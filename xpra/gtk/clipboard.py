# This file is part of Xpra.
# Copyright (C) 2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from time import monotonic

from xpra.util.gobject import n_arg_signal, one_arg_signal
from xpra.clipboard.common import ClipboardCallback
from xpra.clipboard.targets import TEXT_TARGETS
from xpra.clipboard.uri import file_uri_to_clientfs_uri
from xpra.clipboard.proxy import ClipboardProxyCore, filter_data
from xpra.clipboard.timeout import ClipboardTimeoutHelper
from xpra.os_util import gi_import
from xpra.util.str_fn import Ellipsizer
from xpra.util.env import envint
from xpra.log import Logger

Gtk = gi_import("Gtk")
Gdk = gi_import("Gdk")
GObject = gi_import("GObject")

log = Logger("clipboard")

BLOCK_DELAY = envint("XPRA_CLIPBOARD_BLOCK_DELAY", 5)


def _has_bridge_managed_target(targets):
    """Whether a token still belongs to the rich provider state machine."""
    return any(str(x).startswith(("image/", "text/html")) or str(x) == "text/uri-list"
               for x in (targets or ()))


import struct
import codecs
def parse_windows_CF_HDROP(hdrop_data: bytes, prefix: str) -> list[str]:
    """
    解析 CF_HDROP 格式的二进制数据为文件路径列表
    """
    if len(hdrop_data) < 20:
        raise ValueError("无效的 CF_HDROP 数据：长度不足")

    offset = struct.unpack("<I", hdrop_data[:4])[0]  # 小端序解析4字节偏移量

    path_data = hdrop_data[offset:]
    if not path_data:
        return []

    try:
        utf16_str = codecs.decode(path_data, "utf-16le", errors="ignore")
        paths = [f"{prefix}{p}" for p in utf16_str.rstrip("\x00").split("\x00") if p]
        return paths
    except Exception as e:
        raise ValueError(f"解析路径失败：{e}") from e

class GTK_Clipboard(ClipboardTimeoutHelper):

    def __repr__(self):
        return "GTK_Clipboard"

    def make_proxy(self, selection):
        proxy = GTKClipboardProxy(selection)
        proxy.catlink_bridge = self.catlink_bridge if hasattr(self, "catlink_bridge") else None
        proxy.set_want_targets(self._want_targets)
        proxy.set_direction(self.can_send, self.can_receive)
        proxy.connect("send-clipboard-token", self._send_clipboard_token_handler)
        proxy.connect("send-clipboard-request", self._send_clipboard_request_handler)
        return proxy


class GTKClipboardProxy(ClipboardProxyCore, GObject.GObject):
    __gsignals__ = {
        "send-clipboard-token": one_arg_signal,
        "send-clipboard-request": n_arg_signal(2),
    }

    def __init__(self, selection="CLIPBOARD"):
        ClipboardProxyCore.__init__(self, selection)
        GObject.GObject.__init__(self)
        self._owner_change_embargo = 0.0
        self._catlink_local_owner = False
        self._catlink_text_owner = None
        self._want_targets = False
        self.catlink_bridge = None
        display = Gdk.Display.get_default()
        if not display:
            log.warn(f"Warning: no display, cannot access the {selection} clipboard")
            return
        self.clipboard = Gtk.Clipboard.get(Gdk.Atom.intern(selection, False))
        self.clipboard.connect("owner-change", self.owner_change)

    def __repr__(self):
        return "GTKClipboardProxy(%s)" % self._selection

    def got_token(self, targets, target_data=None, claim=True, synchronous_client=False) -> None:
        # the remote end now owns the clipboard
        self.cancel_emit_token()
        if not self._enabled:
            return
        self._got_token_events += 1
        log("got token, selection=%s, targets=%s, target data=%s, claim=%s, synchronous_client=%s, can-receive=%s",
            self._selection, targets, Ellipsizer(target_data), claim, synchronous_client, self._can_receive)
        if claim:
            self._have_token = True
        if not self._can_receive:
            return
        bridge = self.catlink_bridge
        if bridge and bridge.accepts(self):
            # A token without rich targets retires only this selection's
            # provider. In particular PRIMARY empty tokens must never reset
            # CLIPBOARD, and a later text set_text must supersede idle cleanup.
            if not _has_bridge_managed_target(targets):
                bridge.invalidate_native(self)
            elif bridge._active:
                if bridge.is_claimed(self, targets) or bridge.claim(self, targets, target_data):
                    self._have_token = True
                    return
        if target_data and claim:
            targets = target_data.keys()
            text_targets = tuple(x for x in targets if x in TEXT_TARGETS)
            for text_target in text_targets:
                dtype, dformat, data = target_data.get(text_target)
                if dformat != 8:
                    continue
                text = str(data)
                if isinstance(data, bytes):
                    # Xpra's wire representation carries text/plain as UTF-8
                    # bytes too.  Do not select Latin-1 solely from the
                    # target atom name: that turns valid CJK UTF-8 into
                    # mojibake (for example e5 93 88 -> "å\x93\x88").
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        text = data.decode("latin1")
                log("setting text data %s / %s of size %i: %s", dtype, dformat, len(text), Ellipsizer(text))
                # The bridge may have queued an idle callback to clear the
                # previous file/image provider.  Text replacement supersedes
                # that cleanup; cancel it before changing GTK ownership.
                cancel_clear = getattr(self.catlink_bridge, "mark_native_replaced", None)
                if cancel_clear:
                    cancel_clear()
                self._owner_change_embargo = monotonic()
                self.clipboard.set_text(text, -1)
                if bridge and bridge.accepts(self):
                    self._catlink_local_owner = False
                    self._catlink_text_owner = self._get_local_selection_owner()
                return
            # we should handle more datatypes here..

    ############################################################################
    # forward local requests to the remote clipboard:
    ############################################################################
    def schedule_emit_token(self, min_delay=0) -> None:
        def send_token(*token_data):
            self._have_token = False
            self.emit("send-clipboard-token", token_data)

        if not (self._want_targets or self._greedy_client or self.catlink_bridge):
            send_token()
            return
        # we need the targets:
        raw_targets = self.clipboard.wait_for_targets()
        if raw_targets and len(raw_targets) == 2 and isinstance(raw_targets[0], bool):
            raw_targets = raw_targets[1] if raw_targets[0] else ()
        targets = tuple(x.name() if hasattr(x, "name") else str(x) for x in (raw_targets or ()))
        log.debug("clipboard targets snapshot selection=%s targets=%s", self._selection, targets)
        if not targets:
            send_token()
            return
        if not self._greedy_client:
            send_token(targets)
            return
        # for now, we only handle text targets:
        text_targets = tuple(x for x in targets if x in TEXT_TARGETS)
        if text_targets:
            text = self.clipboard.wait_for_text()
            log.debug("clipboard text snapshot selection=%s text-present=%s text-size=%s targets=%s",
                     self._selection, bool(text), len(text or ""), targets)
            if text:
                # should verify the target is actually utf8...
                text_target = text_targets[0]
                send_token(targets, (text_target, "UTF8_STRING", 8, text))
                return
        # Text data may be temporarily unavailable while the owner is still
        # preparing the selection. Preserve the complete offer in that case.
        log.debug("clipboard sending targets without text data selection=%s targets=%s", self._selection, targets)
        send_token(targets if self.catlink_bridge else text_targets)

    def owner_change(self, clipboard, event) -> None:
        log("owner_change(%s, %s) window=%s, selection=%s",
            clipboard, event, event.window, event.selection)
        # A proxy is bound to one X11 selection.  Some GTK/display setups
        # deliver owner-change notifications for another selection as well;
        # forwarding those would let PRIMARY activity invalidate CLIPBOARD.
        event_selection = getattr(event, "selection", None)
        if event_selection is not None:
            try:
                event_selection = event_selection.name()
            except Exception:
                event_selection = str(event_selection)
            if event_selection and event_selection != self._selection:
                log.debug("ignoring owner-change for selection=%s on proxy=%s",
                          event_selection, self._selection)
                return
        self.do_owner_changed(event)

    def _get_local_selection_owner(self):
        try:
            return Gdk.selection_owner_get_for_display(
                self.clipboard.get_display(), Gdk.Atom.intern(self._selection, False))
        except (AttributeError, TypeError):
            return None

    def do_owner_changed(self, event=None) -> None:
        now = monotonic()
        elapsed = now - self._owner_change_embargo
        log("do_owner_changed() enabled=%s, elapsed=%s", self._enabled, elapsed)
        if not self._enabled:
            return
        bridge = self.catlink_bridge
        if bridge and (bridge.is_claimed(self) or (bridge.accepts(self) and bridge.owns_native(self))):
            # GTK has not released our provider: this is our own claim event.
            return
        text_owner = self._catlink_text_owner
        if text_owner is not None:
            if self._get_local_selection_owner() == text_owner:
                return
            self._catlink_text_owner = None
            self._catlink_local_owner = True
        local_takeover = self._catlink_local_owner
        # Keep the existing text/backend embargo. A native clear callback is
        # positive evidence of a new local owner, not an elapsed-time guess.
        # Remain in local mode until the next remote claim: consuming a one-
        # shot flag would drop B -> C copies inside A's old embargo period.
        if not local_takeover and elapsed < BLOCK_DELAY:
            return
        self.set_local_clipboard_origin()
        self.schedule_emit_token()

    def get_contents(self, target: str, got_contents: ClipboardCallback, time=0) -> None:
        log("get_contents(%s, %s, %i) have-token=%s", target, got_contents, time, self._have_token)

        def get_targets() -> list:
            r = self.clipboard.wait_for_targets()
            if r and len(r) == 2 and r[0]:
                return r[1]
            return []

        if target == "TARGETS":
            atoms = tuple(x.name() for x in get_targets())
            got_contents("ATOM", 32, atoms)
            return
        if target == "text/uri-list":
            processed_uris = []
            uris = self.clipboard.wait_for_uris()
            if len(uris) == 0:
                sel = self.clipboard.wait_for_contents(Gdk.Atom.intern("CF_HDROP", False))
                if sel != None:
                    uris = parse_windows_CF_HDROP(sel.get_data(), "file://")
            for uri in uris:
                if uri.startswith("file://"):
                    processed_uris.append(file_uri_to_clientfs_uri(uri))
                else:
                    processed_uris.append(uri)

            data = "\n".join(processed_uris) +"\n"
            print(f"GOT URI LIST {uris} -> {processed_uris}")
            got_contents(target, 8, data or "")
            return
        if target in TEXT_TARGETS:
            text = self.clipboard.wait_for_text()
            got_contents(target, 8, text or "")
            return
        atom = next((x for x in get_targets() if x.name() == target), None)
        if atom:
            sel = self.clipboard.wait_for_contents(atom)
            if sel:
                data = sel.get_data()
                data = filter_data(dtype=target, dformat=8, data=data)
                got_contents(target, 8, data)
                return
        log.warn(f"Warning: can't find request target atom {target}")
        got_contents(target, 0, b"")


GObject.type_register(GTKClipboardProxy)
