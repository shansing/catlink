# This file is part of Xpra.
# Copyright (C) 2012 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from io import BytesIO
from time import monotonic
from collections.abc import Sequence

from AppKit import (
    NSStringPboardType, NSPasteboardTypeFileURL, NSPasteboardTypePNG,
    NSPasteboardTypeTIFF, NSPasteboardTypeURL, NSPasteboard,
)
from CoreFoundation import NSData, CFDataGetBytes, CFDataGetLength

from xpra.clipboard.timeout import ClipboardTimeoutHelper
from xpra.clipboard.common import ClipboardCallback
from xpra.clipboard.targets import _filter_targets, TEXT_TARGETS
from xpra.clipboard.proxy import ClipboardProxyCore, filter_data
from xpra.platform.darwin.rich_clipboard import (
    HTML_TARGETS, get_clientfs_uris, get_pasteboard_file_urls,
    get_rich_text_html, get_rich_text_image, has_rich_text_content, has_rtfd_rtf,
)
from xpra.platform.ui_thread_watcher import get_ui_watcher
from xpra.util.str_fn import csv, Ellipsizer, bytestostr
from xpra.os_util import gi_import
from xpra.log import Logger

log = Logger("clipboard", "osx")

GLib = gi_import("GLib")

TARGET_TRANS = {
    NSStringPboardType: "STRING",
}

IMAGE_FORMATS = ["image/png", "image/jpeg", "image/tiff"]
URI_TARGETS = ("text/uri-list",)


def filter_targets(targets) -> Sequence[str]:
    return _filter_targets(TARGET_TRANS.get(x, x) for x in targets)


class OSXClipboardProxy(ClipboardProxyCore):

    def __init__(self, selection, pasteboard, send_clipboard_request_handler, send_clipboard_token_handler):
        self.pasteboard = pasteboard
        self.send_clipboard_request_handler = send_clipboard_request_handler
        self.send_clipboard_token_handler = send_clipboard_token_handler
        super().__init__(selection)
        self.update_change_count()
        # setup clipboard counter watcher:
        w = get_ui_watcher()
        w.add_alive_callback(self.timer_clipboard_check)

    def cleanup(self) -> None:
        super().cleanup()
        w = get_ui_watcher()
        if w:
            try:
                w.remove_alive_callback(self.timer_clipboard_check)
            except (KeyError, ValueError):
                pass

    def timer_clipboard_check(self) -> None:
        c = self.change_count
        self.update_change_count()
        log("timer_clipboard_check() was %s, now %s (have token: %s)", c, self.change_count, self._have_token)
        if c != self.change_count:
            self.local_clipboard_changed()

    def update_change_count(self) -> None:
        p = self.pasteboard
        if p:
            self.change_count = p.changeCount()

    def clear(self) -> None:
        self.pasteboard.clearContents()

    def do_emit_token(self) -> None:
        packet_data: list[str | int | list[str]] = []
        targets = self.get_targets()
        log("do_emit_token() targets=%s", targets)
        packet_data.append(targets)
        if self._greedy_client and "TEXT" in targets:
            text = self.get_clipboard_text()
            if text:
                text_target = next((t for t in TEXT_TARGETS if t in targets), "UTF8_STRING")
                packet_data.append((text_target, "UTF8_STRING", 8, text))
        self.send_clipboard_token_handler(self, tuple(packet_data))

    def get_clipboard_text(self) -> str | None:
        text = self.pasteboard.stringForType_(NSStringPboardType)
        log("get_clipboard_text() NSStringPboardType='%s' (%s)", text, type(text))
        if text is None:
            return None
        return str(text)

    def get_file_urls(self) -> list:
        urls = get_pasteboard_file_urls(self.pasteboard)
        log("get_file_urls()=%s", urls)
        return urls

    def get_uri_list(self) -> str:
        urls = self.get_file_urls()
        uris = get_clientfs_uris(self.pasteboard, urls, materialize=has_rtfd_rtf(self.pasteboard.types() or ()))
        data = "\n".join(uris)
        if data:
            data += "\n"
        log("get_uri_list()=%s", data)
        return data

    def get_targets(self) -> list[str]:
        types = self.pasteboard.types()
        targets = []
        if NSPasteboardTypeFileURL in types:
            targets += URI_TARGETS
        if any(t in (NSStringPboardType, NSPasteboardTypeURL, "public.utf8-plain-text", "public.html", "TEXT") for t in
               types):
            targets += ["TEXT", "STRING", "text/plain", "text/plain;charset=utf-8", "UTF8_STRING"]
        if has_rtfd_rtf(types):
            targets += list(HTML_TARGETS)
        if any(t in (NSPasteboardTypePNG, NSPasteboardTypeTIFF) for t in types):
            targets.append("image/png")
        if NSPasteboardTypeTIFF in types:
            targets.append("image/tiff")
        log("get_targets() targets(%s)=%s", types, targets)
        return list(dict.fromkeys(targets))

    def get_contents(self, target: str, got_contents: ClipboardCallback) -> None:
        request_started = monotonic()
        log("get_contents%s started", (target, got_contents))

        def finish(dtype: str, dformat: int, data) -> None:
            elapsed = int((monotonic() - request_started) * 1000)
            log("get_contents(%s) finish dtype=%s, format=%s, size=%s, elapsed=%ims",
                target, dtype, dformat, len(data or ""), elapsed)
            got_contents(dtype, dformat, data)

        if target == "TARGETS":
            finish("ATOM", 32, self.get_targets())
            return
        if target in URI_TARGETS:
            if has_rtfd_rtf(self.pasteboard.types() or ()) and has_rich_text_content(self.pasteboard):
                log("get_contents(%s) ignoring rich text file URLs because non-whitespace text is present", target)
                got_contents("", 0, None)
                return
            uri_list = self.get_uri_list()
            if uri_list:
                finish(target, 8, uri_list)
            else:
                got_contents("", 0, None)
            return
        if target in HTML_TARGETS:
            if not has_rtfd_rtf(self.pasteboard.types() or ()):
                got_contents("", 0, None)
                return
            try:
                html = get_rich_text_html(self.pasteboard)
                if html:
                    finish(target, 8, html)
                    return
            except Exception:
                elapsed = int((monotonic() - request_started) * 1000)
                log.error("Error: failed to copy rich text as HTML after %ims", elapsed, exc_info=True)
            got_contents("", 0, None)
            return
        if target in IMAGE_FORMATS:
            try:
                data = self.get_image_contents(target)
                if data:
                    finish(target, 8, data)
                    return
            except Exception:
                elapsed = int((monotonic() - request_started) * 1000)
                log.error("Error: failed to copy image from clipboard after %ims", elapsed, exc_info=True)
            got_contents("", 0, None)
            return
        if target in ("TEXT", "STRING", "text/plain", "text/plain;charset=utf-8", "UTF8_STRING"):
            text = self.get_clipboard_text()
            if text is None:
                got_contents("", 0, None)
            else:
                finish(target, 8, text)
            return
        # we don't know how to handle this target:
        got_contents("", 0, None)

    def get_image_contents(self, target):
        started = monotonic()
        types = filter_targets(self.pasteboard.types())
        log("get_image_contents(%s) pasteboard types=%s", target, types)
        if target == "image/png" and NSPasteboardTypePNG in types:
            src_dtype = target
            img_data = self.pasteboard.dataForType_(NSPasteboardTypePNG)
        elif target == "image/tiff" and NSPasteboardTypeTIFF in types:
            src_dtype = target
            img_data = self.pasteboard.dataForType_(NSPasteboardTypeTIFF)
        elif NSPasteboardTypePNG in types:
            src_dtype = "image/png"
            img_data = self.pasteboard.dataForType_(NSPasteboardTypePNG)
        elif NSPasteboardTypeTIFF in types:
            src_dtype = "image/tiff"
            img_data = self.pasteboard.dataForType_(NSPasteboardTypeTIFF)
        else:
            img_data = get_rich_text_image(self.pasteboard, target)
            if img_data:
                src_dtype = target
            else:
                log("image target '%s' not found in %s", target, types)
                return None
        if isinstance(img_data, bytes):
            img_data = filter_data(dtype=src_dtype, dformat=8, data=img_data, trusted=False, output_dtype=target)
            log("get_image_contents(%s)=%i %s from rich text, total elapsed=%ims",
                target, len(img_data or ()), type(img_data), int((monotonic() - started) * 1000))
            return img_data
        if not img_data:
            elapsed = int((monotonic() - started) * 1000)
            log("get_image_contents(%s) no data for source=%s after %ims", target, src_dtype, elapsed)
            return None
        data_ready = monotonic()
        length = CFDataGetLength(img_data)
        log("get_image_contents(%s) source=%s cfdata length=%s, fetch elapsed=%ims",
            target, src_dtype, length, int((data_ready - started) * 1000))
        img_data = CFDataGetBytes(img_data, (0, length), None)
        bytes_ready = monotonic()
        log("get_image_contents(%s) copied %s bytes from pasteboard in %ims",
            target, len(img_data or b""), int((bytes_ready - data_ready) * 1000))
        img_data = filter_data(dtype=src_dtype, dformat=8, data=img_data, trusted=False, output_dtype=target)
        log("get_image_contents(%s)=%i %s, convert elapsed=%ims, total elapsed=%ims",
            target, len(img_data or ()), type(img_data),
            int((monotonic() - bytes_ready) * 1000), int((monotonic() - started) * 1000))
        return img_data

    def got_token(self, targets, target_data=None, claim=True, _synchronous_client=False) -> None:
        # the remote end now owns the clipboard
        self.cancel_emit_token()
        if not self._enabled:
            return
        self._got_token_events += 1
        log("got token, selection=%s, targets=%s, target data=%s, claim=%s, can-receive=%s",
            self._selection, targets, Ellipsizer(target_data), claim, self._can_receive)
        if self._can_receive:
            self.targets = _filter_targets(targets or ())
            self.target_data = target_data or {}
            if targets:
                self.got_contents("TARGETS", "ATOM", 32, targets)
            if target_data:
                for target, td_def in target_data.items():
                    dtype, dformat, data = td_def
                    dtype = bytestostr(dtype)
                    self.got_contents(target, dtype, dformat, data)
            # since we claim to be greedy
            # the peer should have sent us the target and target_data,
            # if not then request it:
            if not targets:
                self.send_clipboard_request_handler(self, self._selection, "TARGETS")
        if not claim:
            log("token packet without claim, not setting the token flag")
            return
        if claim:
            self._have_token = True

    def got_contents(self, target, dtype="", dformat: int = 8, data=b"") -> None:
        # if this is the special target 'TARGETS', cache the result:
        if target == "TARGETS" and dtype == "ATOM" and dformat == 32:
            self.targets = _filter_targets(data)
            log("got_contents: tell OS we have %s", csv(self.targets))
            image_types = tuple(t for t in IMAGE_FORMATS if t in self.targets)
            log("image_types=%s, dtype=%s (is text=%s)",
                image_types, dtype, dtype in TEXT_TARGETS)
            if image_types and dtype not in TEXT_TARGETS:
                # request image:
                self.send_clipboard_request_handler(self, self._selection, image_types[0])
            return
        if dformat == 8 and dtype in TEXT_TARGETS:
            log("we got a byte string: %s", Ellipsizer(data))
            try:
                text = data.decode("utf8")
            except UnicodeDecodeError:
                text = bytestostr(data)
            self.set_clipboard_text(text)
        if dformat == 8 and dtype in IMAGE_FORMATS:
            log("we got a %s image", dtype)
            self.set_image_data(dtype, data)

    def set_image_data(self, dtype, data):
        img_type = dtype.split("/")[1]  # ie: "png"
        from xpra.codecs.pillow.decoder import open_only
        img = open_only(data, (img_type,))
        for img_type, macos_types in {
            "png": [NSPasteboardTypePNG, "image/png"],
            "tiff": [NSPasteboardTypeTIFF, "image/tiff"],
            "jpeg": ["public.jpeg", "image/jpeg"],
        }.items():
            try:
                save_img = img
                if img_type == "jpeg" and img.mode == "RGBA":
                    save_img = img.convert("RGB")
                buf = BytesIO()
                save_img.save(buf, img_type)
                data = buf.getvalue()
                buf.close()
                self.pasteboard.clearContents()
                nsdata = NSData.dataWithData_(data)
                for t in macos_types:
                    r = self.pasteboard.setData_forType_(nsdata, t)
                    log("set '%s' data type: %s", t, r)
            except Exception as e:
                log("set_image_data(%s, ..)", dtype, exc_info=True)
                log.error("Error: failed to copy %s image to clipboard", img_type)
                log.estr(e)

    def set_clipboard_text(self, text) -> None:
        self.pasteboard.clearContents()
        r = self.pasteboard.setString_forType_(text, NSStringPboardType)
        log("set_clipboard_text(%s) success=%s", text, r)
        self.update_change_count()

    def local_clipboard_changed(self) -> None:
        log("local_clipboard_changed()")
        if not self._enabled or not self._can_send or self._block_owner_change:
            return
        self.schedule_emit_token()


class OSXClipboardProtocolHelper(ClipboardTimeoutHelper):

    def __init__(self, *args, **kwargs):
        self.pasteboard = NSPasteboard.generalPasteboard()
        if self.pasteboard is None:
            raise RuntimeError("cannot load Pasteboard, maybe not running from a GUI session?")
        kwargs["clipboard.local"] = "CLIPBOARD"
        kwargs["clipboards.local"] = ["CLIPBOARD"]
        super().__init__(*args, **kwargs)

    def __repr__(self):
        return "OSXClipboardProtocolHelper"

    def cleanup(self) -> None:
        super().cleanup()
        self.pasteboard = None

    def make_proxy(self, selection) -> OSXClipboardProxy:
        proxy = OSXClipboardProxy(selection, self.pasteboard,
                                  self._send_clipboard_request_handler, self._send_clipboard_token_handler)
        proxy.set_direction(self.can_send, self.can_receive)
        return proxy

    ############################################################################
    # just pass ATOM targets through
    # (we use them internally as strings)
    ############################################################################
    def _munge_wire_selection_to_raw(self, encoding, dtype, dformat, data) -> bytes | str:
        if encoding == "atoms":
            data = _filter_targets(data)
        return super()._munge_wire_selection_to_raw(encoding, dtype, dformat, data)


def main() -> None:
    import time
    from xpra.platform import program_context
    with program_context("OSX Clipboard Change Test"):
        log.enable_debug()

        # init UI watcher with gobject (required by pasteboard monitoring code)
        get_ui_watcher()

        log.info("testing pasteboard")

        gtk = gi_import("Gtk")
        pasteboard = NSPasteboard.generalPasteboard()

        def nosend(*args):
            log("nosend%s", args)

        proxy = OSXClipboardProxy("CLIPBOARD", pasteboard, nosend, nosend)
        log.info("current change count=%s", proxy.change_count)
        clipboard = gtk.Clipboard(selection="CLIPBOARD")
        log.info("changing clipboard %s contents", clipboard)
        clipboard.set_text("HELLO WORLD %s" % time.time())
        proxy.update_change_count()
        log.info("new change count=%s", proxy.change_count)
        log.info("any update to your clipboard should get logged (^C to exit)")
        cc = proxy.change_count
        while True:
            v = proxy.change_count
            if v != cc:
                log.info("success! the clipboard change has been detected, new change count=%s", v)
            else:
                log.info(".")
            time.sleep(1)


if __name__ == "__main__":
    main()
