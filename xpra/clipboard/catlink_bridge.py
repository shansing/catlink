import ctypes
import ctypes.util
import json
import os
import posixpath
import re
import socket
import sys
import threading
import time
from html import unescape
from urllib.parse import unquote, urlsplit

from xpra.log import Logger
from xpra.util.str_fn import bytestostr

log = Logger("clipboard")


class _GtkTargetEntry(ctypes.Structure):
    _fields_ = (("target", ctypes.c_char_p), ("flags", ctypes.c_uint), ("info", ctypes.c_uint))


_GtkClipboardGetFunc = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
)
_GtkClipboardClearFunc = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_GTK_MANAGED_TARGETS = frozenset(("TARGETS", "MULTIPLE", "TIMESTAMP", "SAVE_TARGETS"))
_HTML_FILE_ATTRIBUTE = re.compile(r"(\b(?:src|data)\s*=\s*)([\"'])(.*?)(\2)", re.IGNORECASE)
_MACOS_PROVIDER_CLASS = None
_MAX_IPC_HEADER_SIZE = 1 << 20
_MAX_IPC_BINARY_SIZE = 128 << 20


def _is_html_target(target):
    return bytestostr(target).lower().startswith("text/html")


def _is_image_request_target(target):
    target = bytestostr(target).lower()
    return target.startswith("image/") or target == "application/x-qt-image"


def _as_bytes(data):
    if isinstance(data, str):
        return data.encode("utf8")
    return bytes(data)


def _same_source_offer(previous_origin, origin):
    return bool(origin and origin == previous_origin)


def _is_remote_file_uri_text(value):
    text = value.decode("utf8", "replace") if isinstance(value, bytes) else str(value or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and all(line.lower().startswith("file://") for line in lines)


def _local_file_target(target, uris):
    if target == "Shell IDList Array":
        from xpra.clipboard.win32_formats import encode_shell_id_list
        return encode_shell_id_list(uris)
    if target == "x-special/gnome-copied-files":
        # Unlike text/uri-list, GNOME's private format rejects an empty final
        # URI record, so normalize separators and omit the trailing newline.
        return b"copy\n" + b"\n".join(uris.splitlines())
    if target == "text/x-moz-url":
        first = uris.decode("utf-8").splitlines()[0] if uris else ""
        title = os.path.basename(unquote(urlsplit(first).path))
        return (first + "\r\n" + title).encode("utf-16le") if first else b""
    return uris


def _image_data_complete(dtype, data):
    raw = bytes(data or b"")
    kind = bytestostr(dtype).lower()
    if kind == "image/bmp":
        return len(raw) >= 6 and raw[:2] == b"BM" and int.from_bytes(raw[2:6], "little") <= len(raw)
    if kind == "image/png":
        return len(raw) >= 20 and raw.startswith(b"\x89PNG\r\n\x1a\n") and raw[-8:-4] == b"IEND"
    if kind in ("image/jpeg", "image/jpg"):
        return len(raw) >= 4 and raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")
    if kind == "image/webp":
        return (len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
                and int.from_bytes(raw[4:8], "little") + 8 <= len(raw))
    return True


def _html_file_references(data):
    text = data.decode("utf8", "replace") if isinstance(data, bytes) else str(data or "")
    references = []
    seen = set()
    for match in _HTML_FILE_ATTRIBUTE.finditer(text):
        source = match.group(3)
        value = unescape(source)
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "file" or parsed.netloc not in ("", "localhost"):
            continue
        path = unquote(parsed.path)
        # These paths belong to the Linux server, not the client's OS.
        if not posixpath.isabs(path) or source in seen:
            continue
        seen.add(source)
        references.append((source, path))
    return references


def _get_macos_provider_class():
    global _MACOS_PROVIDER_CLASS
    if _MACOS_PROVIDER_CLASS is not None:
        return _MACOS_PROVIDER_CLASS
    import objc
    from AppKit import NSObject
    provider_protocol = objc.protocolNamed("NSPasteboardItemDataProvider")

    class CatlinkRemoteClipboardProvider(NSObject, protocols=[provider_protocol]):
        def pasteboard_item_provideDataForType_(self, _pasteboard, item, pbtype):
            try:
                bridge = self._catlink_bridge
                generation = self._catlink_generation
                if generation != bridge.generation or not bridge._active:
                    return
                target = self._catlink_types[str(pbtype)]
                bridge._request_remote(generation, target)
                bridge._send({"type": "data-request", "generation": generation, "target": target})
                response = bridge._wait_response(generation, target)
                if not response:
                    return
                _dtype, data = response
                if generation != bridge.generation or not bridge._active:
                    return
                if target == "text/uri-list":
                    uris = data.decode("utf8").splitlines()
                    index = self._catlink_file_index
                    if index >= len(uris) or uris[index] != self._catlink_file_uri:
                        return
                    item.setString_forType_(uris[index], pbtype)
                else:
                    from Foundation import NSData
                    item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), pbtype)
            except Exception:
                log.error("macOS clipboard provider failed", exc_info=True)

        @objc.python_method
        def configure(self, bridge, generation, types):
            self._catlink_bridge = bridge
            self._catlink_generation = generation
            self._catlink_types = types
            return self

    _MACOS_PROVIDER_CLASS = CatlinkRemoteClipboardProvider
    return _MACOS_PROVIDER_CLASS


class CatlinkClipboardBridge:
    """Small bridge from the Xpra client clipboard to the Catlink Go host."""
    def __init__(self, request_target, enabled=None):
        self.request_target = request_target
        if enabled is None:
            enabled = os.environ.get("CATLINK_REMOTE_CLIPBOARD_DOWNLOAD", "1").lower() not in ("0", "false", "no", "off")
        self.enabled = bool(enabled)
        self.generation = 0
        self.sock = None
        self._lock = threading.Condition()
        self._send_lock = threading.Lock()
        self._responses = {}
        self._inline = {}
        self._targets = ()
        self._active = False
        self._received = {}
        self._requested = set()
        # Xpra replies identify the request, not the offer. Preserve the offer
        # generation at send time so a delayed reply from an older copy can
        # never be written into the currently active clipboard generation.
        self._request_generations = {}
        # Eager text/plain and delayed text/uri-list are two delivery paths
        # for the same file offer. Keep registration idempotent per generation
        # so the broker never receives the same URI twice (which would make a
        # later URI response contain duplicate files).
        self._uri_file_offers = set()
        self._unavailable = set()
        self._proxy = None
        self._selection_name = ""
        self._offer_text = ""
        self._source_origin = ""
        self._source_offer_id = 0
        self._offer_fingerprint = None
        self._last_token_duplicate = False
        self._gtk = None
        self._gdk = None
        self._gtk_provider = None
        self._claimed_proxy = None
        self._claimed_targets = ()
        self._claimed_generation = 0
        # Monotonic cancellation nonce for deferred native-provider cleanup.
        # A text/native replacement invalidates older idle callbacks so they
        # cannot clear the newly installed clipboard owner.
        self._native_clear_nonce = 0
        self._native_installing = False
        self._native_owner = None
        if not self.enabled:
            return  # Disabled means no IPC, downloads or native ownership hooks.
        raw = os.environ.get("CATLINK_CLIPBOARD_IPC", "")
        if not raw or "|" not in raw:
            return
        try:
            addr, secret = raw.rsplit("|", 1)
            host, port = addr.rsplit(":", 1)
            self.sock = socket.create_connection((host, int(port)), timeout=3)
            self._send({"type": "hello", "secret": secret})
            self.sock.settimeout(None)
            threading.Thread(target=self._reader, daemon=True).start()
        except Exception:
            log.warn("cannot connect to Catlink clipboard bridge", exc_info=True)
            self.sock = None

    def _send(self, value, binary=b""):
        if not self.sock:
            return
        payload = bytes(binary or b"")
        if len(payload) > _MAX_IPC_BINARY_SIZE:
            raise ValueError(f"clipboard IPC binary payload is too large: {len(payload)} bytes")
        header = dict(value)
        if payload:
            header["binary_size"] = len(payload)
        encoded = (json.dumps(header, separators=(",", ":")) + "\n").encode()
        if len(encoded) > _MAX_IPC_HEADER_SIZE:
            raise ValueError(f"clipboard IPC header is too large: {len(encoded)} bytes")
        with self._send_lock:
            self.sock.sendall(encoded)
            if payload:
                self.sock.sendall(payload)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _reader(self):
        try:
            stream = self.sock.makefile("rb")
            while line := stream.readline():
                if len(line) > _MAX_IPC_HEADER_SIZE:
                    raise ValueError(f"clipboard IPC header is too large: {len(line)} bytes")
                msg = json.loads(line)
                size = int(msg.get("binary_size", 0))
                if size < 0 or size > _MAX_IPC_BINARY_SIZE:
                    raise ValueError(f"invalid clipboard IPC binary size: {size}")
                data = self._read_exact(stream, size)
                if msg.get("type") == "data-response":
                    log.debug("clipboard IPC data-response: generation=%s target=%s size=%s",
                             msg.get("generation"), msg.get("target"), len(data))
                    with self._lock:
                        if msg.get("generation") != self.generation or not self._active:
                            continue
                        key = (msg.get("generation"), msg.get("target"))
                        previous = self._responses.get(key)
                        # Concurrent requests share immutable offer data. A late
                        # timeout must not replace a successful response with empty data.
                        if data or previous is None:
                            self._responses[key] = (msg.get("target"), data)
                        self._lock.notify_all()
        except Exception:
            log.debug("clipboard IPC reader stopped", exc_info=True)
        finally:
            # Wake/cancel native file waiters if Go disappears mid-download.
            self.sock = None
            with self._lock:
                self._lock.notify_all()

    @staticmethod
    def _read_exact(stream, size):
        chunks = []
        remaining = size
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise EOFError(f"clipboard IPC binary payload ended with {remaining} bytes missing")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def claim(self, proxy, targets, target_data=None):
        """Claim the native clipboard with delayed callbacks."""
        if not self.accepts(proxy):
            return False
        # Do not resurrect a previous image/file offer when Xpra announces a
        # genuinely text-only clipboard token.  GTK calls this method from
        # ``got_token`` even when the core non-text callback was not involved;
        # consulting the cached targets here would otherwise leave the old
        # native selection owner active and applications paste stale data.
        incoming = tuple(bytestostr(x) for x in targets)
        self._selection_name = getattr(proxy, "_selection", getattr(self, "_selection_name", ""))
        incoming_managed = tuple(x for x in incoming
                                 if x.startswith("image/") or x == "text/uri-list" or _is_html_target(x))
        if not incoming_managed:
            return False
        normalized = self._claim_targets(proxy, incoming)
        managed = tuple(x for x in normalized
                        if x.startswith("image/") or x == "text/uri-list" or _is_html_target(x))
        if not managed:
            return False
        self._claimed_proxy = None
        # GTK implements these selection protocol targets itself. Registering
        # them as payload targets routes TARGETS discovery into our data callback.
        suppress_uri_text = "text/uri-list" in normalized
        if "text/uri-list" in normalized and target_data:
            for key, value in target_data.items():
                if bytestostr(key) in ("UTF8_STRING", "text/plain", "text/plain;charset=utf-8", "TEXT", "STRING"):
                    suppress_uri_text = _is_remote_file_uri_text(value[2] if len(value) > 2 else "")
                    break
        offered = tuple(x for x in normalized if x not in _GTK_MANAGED_TARGETS)
        if "text/uri-list" in normalized and "x-special/gnome-copied-files" not in offered:
            offered += ("x-special/gnome-copied-files",)
        if sys.platform == "win32" and "text/uri-list" in normalized:
            # These are transport/file-manager representations, not document
            # content. Export native files to Windows rather than letting an
            # editor select a URI/text alias instead of its file insertion path.
            # Keep normalized untouched: Xpra must still fetch text/uri-list.
            offered = tuple(x for x in offered
                            if x not in ("text/uri-list", "text/x-moz-url")
                            and not x.startswith("x-special/gnome-")
                            and not (suppress_uri_text and (
                                x in ("UTF8_STRING", "TEXT", "STRING", "text/plain")
                                or x.lower().startswith("text/plain;charset="))))
            # GTK 3 does not translate URI lists to Windows Shell file formats.
            # Advertise a delayed Shell promise, but resolve only downloaded paths.
            offered += ("Shell IDList Array",)
        try:
            gtk, gdk = self._load_gtk_clipboard_api()
            clipboard_ptr = self._gobject_pointer(proxy.clipboard)
        except Exception:
            log.error("cannot load native GTK clipboard provider API", exc_info=True)
            return False
        target_names = tuple(x.encode("utf8") for x in offered)
        entries = (_GtkTargetEntry * len(target_names))(*(
            _GtkTargetEntry(name, 0, index) for index, name in enumerate(target_names)
        ))
        generation = self.generation

        @_GtkClipboardGetFunc
        def get_data(_clipboard, selection_data, info, _user_data):
            if generation != self.generation or not self._active:
                log.debug("ignoring stale native clipboard request: generation=%s current=%s active=%s",
                          generation, self.generation, self._active)
                return
            if info >= len(offered):
                return
            target = offered[info]
            log.debug("local GTK clipboard data requested: generation=%s target=%s", generation, target)
            # All file-list aliases must describe the downloaded local files,
            # including tokens whose eager data is PNG rather than URI text.
            local_uri_text = "text/uri-list" in normalized and (
                target in ("x-special/gnome-copied-files", "text/x-moz-url", "Shell IDList Array")
                or (suppress_uri_text and target in (
                    "UTF8_STRING", "text/plain", "text/plain;charset=utf-8", "TEXT", "STRING")))
            if not local_uri_text:
                self._request_remote(generation, target)
            if target == "text/uri-list":
                # This is always a remote file offer.  It must be fetched via
                # the IPC response path; treating it as inline data causes
                # repeated 30-second inline timeouts when a later token lacks
                # eager target_data.
                self._send({"type": "data-request", "generation": generation, "target": target})
                response = self._wait_response(generation, target)
            elif local_uri_text:
                self._send({"type": "data-request", "generation": generation, "target": "text/uri-list"})
                response = self._wait_response(generation, "text/uri-list")
            elif _is_html_target(target):
                self._send({"type": "data-request", "generation": generation, "target": target})
                response = self._wait_response(generation, target)
            elif _is_image_request_target(target):
                # Qt applications commonly request this private target even
                # when the remote owner advertised image/png.  Always source
                # the canonical PNG bytes instead of waiting for an inline
                # value that Xpra does not provide.
                source_target = "image/png" if target == "application/x-qt-image" else target
                self._send({"type": "data-request", "generation": generation, "target": source_target})
                response = self._wait_response(generation, source_target)
                if response:
                    dtype, data = response
                    dtype = target
            else:
                response = self._wait_inline(generation, target)
            if not response:
                log.warn("local GTK clipboard data unavailable: generation=%s target=%s", generation, target)
                return
            # Waiting pumps the main loop, which can replace the offer. Never
            # finish an obsolete provider callback with a newer owner's data.
            if generation != self.generation or not self._active:
                return
            dtype, data = response
            if local_uri_text:
                dtype = target
                try:
                    data = _local_file_target(target, data)
                except Exception:
                    log.error("cannot encode local clipboard file list: target=%s", target, exc_info=True)
                    return
            elif target == "application/x-qt-image":
                dtype = target
            atom = gdk.gdk_atom_intern((dtype or target).encode("utf8"), False)
            buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            gtk.gtk_selection_data_set(selection_data, atom, 8, ctypes.cast(buf, ctypes.c_void_p), len(data))
            log.debug("local GTK clipboard data provided: generation=%s requested=%s dtype=%s size=%s",
                     generation, target, dtype or target, len(data))

        @_GtkClipboardClearFunc
        def clear_data(_clipboard, _user_data):
            # GTK invokes this for both our own replacement and a genuine
            # local copy. Only the latter changes the direction of the offer.
            # Never clear GTK here: the clipboard now belongs to that app.
            if not self._native_installing:
                self._native_owner = None
            if (not self._native_installing and self._claimed_proxy is proxy
                    and self._claimed_generation == generation):
                self.invalidate_native(proxy, clear=False)
                proxy._catlink_local_owner = True

        proxy._owner_change_embargo = time.monotonic()
        proxy._catlink_local_owner = False
        proxy._catlink_text_owner = None
        self._native_clear_nonce = getattr(self, "_native_clear_nonce", 0) + 1
        self._native_installing = True
        try:
            claimed = gtk.gtk_clipboard_set_with_data(
                clipboard_ptr, entries, len(entries), get_data, clear_data, None,
            )
        finally:
            self._native_installing = False
        # GTK retains the C callbacks until the selection is replaced.
        self._gtk_provider = (entries, target_names, get_data, clear_data)
        self._claimed_proxy = proxy if claimed else None
        self._claimed_targets = tuple(normalized)
        self._claimed_generation = generation
        get_owner = getattr(proxy, "_get_local_selection_owner", None)
        self._native_owner = get_owner() if claimed and get_owner else None
        log.debug("native clipboard provider installed: generation=%s selection=%s targets=%s success=%s",
                 self.generation, self._selection_name, offered, claimed)
        if claimed and sys.platform == "win32" and "text/uri-list" in normalized:
            self._prepare_windows_hdrop(proxy, generation)
        return bool(claimed)

    def _prepare_windows_hdrop(self, proxy, generation):
        # Keep GTK's promised Shell format. Add the predefined CF_HDROP once
        # files exist, without EmptyClipboard or a second clipboard backend.
        try:
            from xpra.clipboard.win32_formats import (
                WindowsFileClipboard, encode_hdrop, uri_list_to_windows_paths, encode_filename_formats,
            )
            from xpra.os_util import gi_import
            glib = gi_import("GLib")
            native = WindowsFileClipboard()
            owner = native.owned_window()
            if not owner:
                log.warn("cannot append Windows file format: GTK is not the native clipboard owner")
                return
        except Exception:
            log.error("cannot initialize Windows file format publisher", exc_info=True)
            return

        def current():
            return (self.sock and self._active and self.generation == generation
                    and self._claimed_proxy is proxy and self._claimed_generation == generation)

        def prepare():
            self._send({"type": "data-request", "generation": generation, "target": "text/uri-list"})
            response = self._wait_response(generation, "text/uri-list", pump_main=False)
            if not response or not response[1] or not current():
                return
            paths = uri_list_to_windows_paths(response[1])
            data = encode_hdrop(paths)
            extra_formats = encode_filename_formats(paths)
            deadline = time.monotonic() + 30
            def publish():
                if not current():
                    return False
                try:
                    if native.append(owner, data, current, extra_formats):
                        log.info("Windows file format publication finished: generation=%s current=%s formats=%s",
                                 generation, bool(current() and native.owned_window() == owner),
                                 ("CF_HDROP",) + tuple(name for name, _ in extra_formats))
                    elif time.monotonic() < deadline:
                        glib.timeout_add(50, publish)
                    else:
                        log.warn("Windows CF_HDROP publication timed out: generation=%s", generation)
                except Exception:
                    log.error("cannot publish Windows CF_HDROP", exc_info=True)
                return False
            glib.idle_add(publish)
        threading.Thread(target=prepare, daemon=True).start()

    @staticmethod
    def _gobject_pointer(obj):
        get_pointer = ctypes.pythonapi.PyCapsule_GetPointer
        get_pointer.argtypes = (ctypes.py_object, ctypes.c_char_p)
        get_pointer.restype = ctypes.c_void_p
        pointer = get_pointer(obj.__gpointer__, None)
        if not pointer:
            raise RuntimeError("PyGObject returned a null native pointer")
        return pointer

    def _wait_inline(self, generation, target):
        deadline = time.monotonic() + 30
        key = (generation, target)
        while time.monotonic() < deadline:
            if generation != self.generation or not self._active:
                return None
            with self._lock:
                response = self._inline.get(key)
                unavailable = key in self._unavailable
            if response:
                dtype, dformat, data = response
                if isinstance(data, str):
                    data = data.encode("utf8")
                return dtype, bytes(data)
            if unavailable:
                return None
            try:
                from xpra.os_util import gi_import
                context = gi_import("GLib").MainContext.default()
                while context.pending():
                    context.iteration(False)
            except Exception:
                pass
            time.sleep(0.01)
        log.warn("inline clipboard data timed out: generation=%s target=%s", generation, target)
        return None

    def _request_remote(self, generation, target):
        if target == "application/x-qt-image":
            target = "image/png"
        key = (generation, target)
        with self._lock:
            if not self._active or generation != self.generation or key in self._requested:
                return
            self._requested.add(key)
            proxy = self._proxy
        if proxy:
            log.debug("requesting Xpra clipboard target: generation=%s target=%s", generation, target)
            request_id = self.request_target(proxy, target)
            if request_id is not None:
                self._request_generations[request_id] = (generation, target)

    def request_remote(self, proxy, target):
        self._proxy = proxy
        self._request_remote(self.generation, target)

    def _load_gtk_clipboard_api(self):
        if self._gtk and self._gdk:
            return self._gtk, self._gdk
        gtk_name = ctypes.util.find_library("gtk-3") or ("libgtk-3-0.dll" if os.name == "nt" else "libgtk-3.so.0")
        gdk_name = ctypes.util.find_library("gdk-3") or ("libgdk-3-0.dll" if os.name == "nt" else "libgdk-3.so.0")
        gtk = ctypes.CDLL(gtk_name)
        gdk = ctypes.CDLL(gdk_name)
        gtk.gtk_clipboard_set_with_data.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(_GtkTargetEntry), ctypes.c_uint,
            _GtkClipboardGetFunc, _GtkClipboardClearFunc, ctypes.c_void_p,
        )
        gtk.gtk_clipboard_set_with_data.restype = ctypes.c_int
        gtk.gtk_clipboard_clear.argtypes = (ctypes.c_void_p,)
        gtk.gtk_clipboard_clear.restype = None
        gtk.gtk_selection_data_set.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
        )
        gtk.gtk_selection_data_set.restype = None
        gdk.gdk_atom_intern.argtypes = (ctypes.c_char_p, ctypes.c_int)
        gdk.gdk_atom_intern.restype = ctypes.c_void_p
        self._gtk, self._gdk = gtk, gdk
        return gtk, gdk

    def _schedule_native_clear(self, proxy, generation):
        """Release an obsolete GTK provider after token dispatch completes."""
        clear_nonce = getattr(self, "_native_clear_nonce", 0)
        try:
            from xpra.os_util import gi_import
            glib = gi_import("GLib")
            def clear_if_stale():
                if (self.generation != generation + 1 or self._active
                        or getattr(self, "_native_clear_nonce", 0) != clear_nonce):
                    return False
                # GTK may not have delivered B's owner-change event yet.
                # Generation alone cannot authorize clearing the clipboard:
                # verify that the X/GDK owner is still our retired provider.
                if not self.owns_native(proxy):
                    return False
                try:
                    gtk, _gdk = self._load_gtk_clipboard_api()
                    gtk.gtk_clipboard_clear(self._gobject_pointer(proxy.clipboard))
                    self._gtk_provider = None
                except Exception:
                    log.debug("cannot clear stale native clipboard provider", exc_info=True)
                return False
            glib.idle_add(clear_if_stale)
        except Exception:
            log.debug("cannot schedule native clipboard cleanup", exc_info=True)

    def mark_native_replaced(self):
        """Cancel deferred cleanup because another owner is being installed."""
        self._native_clear_nonce = getattr(self, "_native_clear_nonce", 0) + 1

    def owns_native(self, proxy):
        owner = self._native_owner
        return owner is not None and proxy._get_local_selection_owner() == owner

    def accepts(self, proxy):
        # PRIMARY is a separate selection, not another view of CLIPBOARD.
        # This broker owns one file offer; other selections retain normal Xpra.
        return self.enabled and bool(self.sock) and getattr(proxy, "_selection", "") == "CLIPBOARD"

    def invalidate_native(self, proxy, clear=True):
        """Invalidate and asynchronously release an obsolete native offer.

        This is used for empty/text-only tokens which do not go through the
        rich provider path.  The caller must invoke it only when it is not
        about to install a replacement via ``GtkClipboard.set_text``.
        """
        if not self.accepts(proxy) or not (self._active or self._claimed_proxy is proxy):
            return
        stale_generation = self.generation
        self.generation += 1
        self._active = False
        self._targets = ()
        self._requested = set()
        self._responses.clear()
        self._inline.clear()
        self._source_offer_id = 0
        self._source_origin = ""
        self._offer_fingerprint = None
        self._claimed_proxy = None
        self._claimed_targets = ()
        self._claimed_generation = 0
        # Keep request-id tombstones until replies/timeouts arrive, so an old
        # response cannot be mistaken for data belonging to the next offer.
        self._send({"type": "reset", "generation": self.generation})
        # There is no remote provider to embargo now. An empty token followed
        # immediately by a local copy must not inherit the retired offer's
        # five-second block. Any replacement claim/set_text resets this mode.
        proxy._catlink_local_owner = True
        if clear:
            self._schedule_native_clear(proxy, stale_generation)

    def _wait_response(self, generation, target, pump_main=True, warn_timeout=True):
        deadline = time.monotonic() + 30
        key = (generation, target)
        # File providers must not reveal a reserved path at 30s while the
        # broker's 60s download is still pending. Failure is an explicit empty
        # response; generation/connection retirement cancels the wait.
        while (target == "text/uri-list" and self.sock) or time.monotonic() < deadline:
            if not self.sock:
                return None
            if generation != self.generation or not self._active:
                return None
            with self._lock:
                # GTK main-loop pumping can nest another waiter for this key.
                # Keep the response for all waiters until the offer is retired.
                response = self._responses.get(key)
            if response:
                return response
            # Clipboard packets are dispatched on the GTK main thread. Keep it
            # moving while the synchronous X11 selection request is pending.
            if pump_main:
                try:
                    from xpra.os_util import gi_import
                    context = gi_import("GLib").MainContext.default()
                    while context.pending():
                        context.iteration(False)
                except Exception:
                    pass
            time.sleep(0.01)
        if warn_timeout:
            log.warn("clipboard data timed out: generation=%s target=%s", generation, target)
        return None

    def claim_macos(self, proxy, targets, target_data=None):
        """Use NSPasteboardItem's provider callback on the native macOS backend."""
        if not self.accepts(proxy):
            return False
        try:
            from AppKit import NSPasteboardItem, NSPasteboardTypePNG, NSPasteboardTypeTIFF, NSPasteboardTypeFileURL
            provider_class = _get_macos_provider_class()
        except ImportError:
            return False
        incoming = tuple(bytestostr(x) for x in targets)
        if not any(x.startswith("image/") or x == "text/uri-list" for x in incoming):
            self._active = False
            self._proxy = proxy
            self._claimed_proxy = None
            self._claimed_targets = ()
            self._claimed_generation = 0
            return False
        normalized = self._claim_targets(proxy, incoming)
        offered = tuple(x for x in normalized if x.startswith("image/") or x == "text/uri-list")
        if not offered:
            return False
        # A native type is a promise of those exact bytes, not a request to
        # transcode. In particular, JPEG/TIFF-only offers must not claim PNG.
        types = {}
        for target, native in (("image/png", NSPasteboardTypePNG),
                               ("image/jpeg", "public.jpeg"), ("image/jpg", "public.jpeg"),
                               ("image/tiff", NSPasteboardTypeTIFF), ("image/tif", NSPasteboardTypeTIFF)):
            if target in offered:
                types.setdefault(str(native), target)
        has_files = "text/uri-list" in offered
        if not types and not has_files:
            return False
        generation = self.generation
        change_count = proxy.pasteboard.changeCount()
        nonce = getattr(self, "_macos_claim_nonce", 0) + 1
        self._macos_claim_nonce = nonce

        def publish(uris=()):
            nonlocal change_count
            # File downloads may finish after another copy, including a local
            # copy not yet seen by the polling timer. Never overwrite it.
            if (generation != self.generation or not self._active
                    or nonce != self._macos_claim_nonce
                    or proxy.pasteboard.changeCount() != change_count):
                return False
            items = [NSPasteboardItem.alloc().init() for _ in (uris or (None,))]
            providers = []
            for index, item in enumerate(items):
                item_types = dict(types) if index == 0 else {}
                if uris:
                    item_types[str(NSPasteboardTypeFileURL)] = "text/uri-list"
                provider = provider_class.alloc().init().configure(self, generation, item_types)
                provider._catlink_file_index = index
                provider._catlink_file_uri = uris[index] if uris else ""
                if item_types:
                    item.setDataProvider_forTypes_(provider, list(item_types))
                providers.append(provider)
            for target, value in (target_data or {}).items():
                if target in ("UTF8_STRING", "TEXT", "text/plain", "text/plain;charset=utf-8"):
                    raw = value[2]
                    text = raw.decode("utf8", "replace") if isinstance(raw, bytes) else str(raw)
                    if has_files and _is_remote_file_uri_text(text):
                        continue
                    from AppKit import NSStringPboardType
                    items[0].setString_forType_(text, NSStringPboardType)
                    break
            proxy.pasteboard.clearContents()
            try:
                success = bool(proxy.pasteboard.writeObjects_(items))
            finally:
                # clearContents also changes the count on a failed write.
                # Our own writes must never become new local-origin offers.
                proxy.update_change_count()
                change_count = proxy.pasteboard.changeCount()
            if not success:
                log.warn("macOS clipboard write failed: generation=%s", generation)
                return False
            proxy._catlink_pasteboard_provider = providers
            return True

        if not has_files:
            return publish()
        # Image bytes and the file representation are independent. Publish
        # image promises now; a failed file registration must not hide them.
        if types:
            publish()
        # Reserve paths before downloading. The layout is internal only:
        # native file providers reveal URLs only once the whole batch exists.
        def prepare_files():
            self._send({"type": "data-request", "generation": generation, "target": "catlink/file-layout"})
            response = None
            # One broker request stays pending until the batch finishes. A
            # native 30s wait expiry is not a failed 60s HTTP download. Keep
            # waiting without sending duplicate requests or timeout spam.
            while self.sock and self._active and generation == self.generation:
                response = self._wait_response(generation, "catlink/file-layout", pump_main=False, warn_timeout=False)
                if response is not None:
                    break
            if not response or not response[1]:
                return
            uris = tuple(x for x in response[1].decode("utf8").splitlines() if x)
            from xpra.os_util import gi_import
            def publish_once():
                if publish(uris):
                    self._send({"type": "start-files", "generation": generation})
                return False
            gi_import("GLib").idle_add(publish_once)
        threading.Thread(target=prepare_files, daemon=True).start()
        return True

    def _claim_targets(self, proxy, targets):
        # Claim is performed for the token currently entering got_token().
        # Never substitute the bridge's previous offer here: a proxy is reused
        # for CLIPBOARD across generations, and doing so would make a newly
        # copied image inherit the prior file-only target set.
        return tuple(bytestostr(x) for x in targets)

    def is_claimed(self, proxy, targets=None):
        """Whether provider ownership matches the current token targets.

        ``got_token`` may receive a two-phase offer: an initial URI-only token
        followed by image targets/data.  Existing ownership is reusable only
        when the target set is unchanged; target expansion requires replacing
        the native provider for the new representation set.
        """
        if not (self._active and getattr(self, "_claimed_proxy", None) is proxy):
            return False
        # Targets can be identical for two different offers. The native
        # callback closes over generation, so reusing it would serve stale
        # data even though the advertised target list matches.
        if getattr(self, "_claimed_generation", 0) != getattr(self, "generation", 0):
            return False
        if targets is None:
            return True
        return tuple(bytestostr(x) for x in targets) == tuple(getattr(self, "_claimed_targets", ()))

    def wait_for_target(self, generation, target):
        """Wait for one delayed native clipboard representation."""
        self._request_remote(generation, target)
        if target.startswith("image/") or target == "text/uri-list":
            self._send({"type": "data-request", "generation": generation, "target": target})
            return self._wait_response(generation, target)
        return self._wait_inline(generation, target)

    def _receive_image(self, target, dtype, data):
        # Eager token data is subject to the same size policy as requested
        # contents. Never cache truncated bytes ahead of a valid file fallback.
        content_key = (self.generation, target)
        with self._lock:
            self._requested.add(content_key)
            complete = _image_data_complete(dtype, data)
            if not complete:
                self._unavailable.add(content_key)
            else:
                self._unavailable.discard(content_key)
            self._lock.notify_all()
        if not complete:
            log.warn("discarding incomplete remote clipboard image: type=%s size=%s", dtype, len(data))
            self._send({"type": "unavailable", "generation": self.generation, "target": target})
            return
        log.debug("remote clipboard image received: generation=%s type=%s size=%s",
                  self.generation, dtype, len(data))
        self._send({"type": "image", "generation": self.generation, "target": dtype}, bytes(data))

    def handle(self, kind, proxy, selection, *args):
        if not self.accepts(proxy) or selection != "CLIPBOARD":
            return False
        if kind == "token":
            self._last_token_duplicate = False
            targets, target_data = args[:2]
            source_offer_id = args[2] if len(args) >= 3 else 0
            source_origin = args[3] if len(args) >= 4 else ""
            normalized = [bytestostr(x) for x in targets]
            # Selection is part of the bridge state-machine key.  A single
            # bridge instance may be shared by CLIPBOARD and PRIMARY proxies;
            # never let a token from one selection mutate the other's offer.
            proxy_selection = getattr(proxy, "_selection", "")
            if proxy_selection and selection != proxy_selection:
                log.debug("ignoring token for selection=%s on proxy=%s", selection, proxy_selection)
                return False
            if self._selection_name and selection != self._selection_name:
                log.debug("ignoring bridge token for inactive selection %s (active=%s)",
                          selection, self._selection_name)
                return False
            nontext = [x for x in normalized if x.startswith("image/") or x == "text/uri-list"]
            html_targets = [x for x in normalized if _is_html_target(x)]
            # The native GTK provider below is Linux-specific. Other platform
            # backends retain their existing rich-text behavior.
            managed = nontext + (html_targets if hasattr(proxy, "clipboard") else [])
            if not managed:
                self.invalidate_native(proxy)
                return False
            self._active = True
            # Mark the local GTK ownership caused by this remote offer. The
            # corresponding owner-change must not be sent back as a new token.
            text = ""
            if target_data:
                for target, value in target_data.items():
                    if bytestostr(target) in ("UTF8_STRING", "TEXT", "STRING", "text/plain", "text/plain;charset=utf-8"):
                        text = value[2].decode("utf8", "replace") if isinstance(value[2], bytes) else str(value[2])
                        break
            fingerprint_data = []
            for target, value in (target_data or {}).items():
                try:
                    fingerprint_data.append((bytestostr(target), bytes(value[2])))
                except Exception:
                    continue
            fingerprint = (tuple(normalized), tuple(fingerprint_data))
            # ``origin`` identifies the clipboard endpoint and is stable
            # across successive user copies; it is therefore not an offer
            # identity.  Use the producer's offer-id when available.  Legacy
            # peers without either id or origin fall back to an exact
            # fingerprint duplicate check below.
            merge = bool(source_offer_id and source_offer_id == self._source_offer_id
                         and _same_source_offer(self._source_origin, source_origin))
            if not source_origin and not source_offer_id and fingerprint == self._offer_fingerprint:
                log.debug("ignoring duplicate legacy clipboard offer: generation=%s", self.generation)
                self._last_token_duplicate = True
                return True
            if merge:
                self._targets = tuple(dict.fromkeys((*self._targets, *normalized)))
                if text:
                    self._offer_text = text
                self._proxy = proxy
                log.debug("updated remote clipboard offer: generation=%s targets=%s", self.generation, managed)
            else:
                self.generation += 1
                self._targets = tuple(normalized)
                self._inline = {}
                self._responses.clear()
                self._received = {}
                self._requested = set()
                self._unavailable = set()
                self._uri_file_offers = set()
                self._proxy = proxy
                self._offer_text = text
                self._source_origin = source_origin
                self._source_offer_id = source_offer_id
                self._offer_fingerprint = fingerprint
                log.debug("remote clipboard offer: generation=%s source-offer=%s origin=%s targets=%s",
                         self.generation, source_offer_id, source_origin, managed)
                self._send({"type": "offer", "generation": self.generation,
                            "targets": normalized, "text": text, "files": []})
                # Fetch the authoritative URI list as ONE batch. Eager text
                # is only a fallback if URI retrieval fails, not a competing
                # download producer which can publish a partial file list.
            if target_data:
                for target, value in target_data.items():
                    normalized_target = bytestostr(target)
                    if not normalized_target.startswith("image/") and normalized_target != "text/uri-list":
                        self._inline[(self.generation, normalized_target)] = value
                    if _is_html_target(normalized_target) and value[2]:
                        threading.Thread(target=self._send_html,
                                         args=(_as_bytes(value[2]), self.generation, normalized_target), daemon=True).start()
                    if normalized_target == "text/uri-list":
                        raw = _as_bytes(value[2]).decode("utf-8", "replace")
                        self._requested.add((self.generation, normalized_target))
                        threading.Thread(target=self._send_uri_files,
                                         args=(raw, self.generation), daemon=True).start()
                    if bytestostr(target).startswith("image/") and value[2]:
                        self._receive_image(normalized_target, bytestostr(value[0]), value[2])
            return True
        if kind == "contents":
            target, dtype, dformat, data = args[0]
            request_id = args[1] if len(args) >= 2 else None
            target = bytestostr(target)
            dtype = bytestostr(dtype)
            request_offer = self._request_generations.pop(request_id, None)
            if not request_offer:
                # The broker must not consume replies requested by another
                # proxy or Xpra's ordinary (non-bridge) clipboard path.
                return False
            if request_offer:
                request_generation, request_target = request_offer
                if request_generation != self.generation or bytestostr(request_target) != target:
                    log.debug("ignoring stale remote clipboard response: request=%s generation=%s current=%s target=%s",
                             request_id, request_generation, self.generation, target)
                    return True
            if not self._active or target not in self._targets:
                return False
            content_key = (self.generation, target)
            if not dtype or data is None:
                if target == "text/uri-list" and _is_remote_file_uri_text(self._offer_text):
                    threading.Thread(target=self._send_uri_files,
                                     args=(self._offer_text, self.generation), daemon=True).start()
                    return True
                with self._lock:
                    self._requested.add(content_key)
                    self._unavailable.add(content_key)
                    self._lock.notify_all()
                self._send({"type": "unavailable", "generation": self.generation, "target": target})
                return True
            with self._lock:
                self._requested.add(content_key)
            content_value = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) else str(data or "")
            if self._received.get(content_key) == content_value:
                log.debug("ignoring duplicate remote clipboard contents: generation=%s target=%s",
                         self.generation, target)
                return True
            self._received[content_key] = content_value
            if target == "text/uri-list":
                log.debug("remote clipboard URI contents received: generation=%s", self.generation)
                raw = data.decode("utf8", "replace") if isinstance(data, bytes) else str(data or "")
                threading.Thread(target=self._send_uri_files,
                                 args=(raw, self.generation), daemon=True).start()
            elif _is_html_target(target):
                threading.Thread(target=self._send_html,
                                 args=(_as_bytes(data), self.generation, target), daemon=True).start()
            elif dtype.startswith("image/") and data:
                self._receive_image(target, dtype, data)
            else:
                with self._lock:
                    self._inline[(self.generation, target)] = (dtype, dformat, data)
                    self._lock.notify_all()
            return True
        return False

    def _send_uri_files(self, raw, generation):
        paths = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("file://"):
                continue
            parsed = urlsplit(line)
            if parsed.netloc not in ("", "localhost"):
                continue
            path = unquote(parsed.path)
            if posixpath.isabs(path) and path not in paths:
                paths.append(path)
        with self._lock:
            if generation != self.generation or not self._active:
                return
            key = (generation, "uri-batch")
            if key in self._uri_file_offers:
                log.debug("ignoring duplicate remote clipboard URI offer: generation=%s files=%s",
                         generation, len(paths))
                return
            self._uri_file_offers.add(key)
        files = [self._offer_file(path) for path in paths]
        if any(f is None for f in files):
            self._send({"type": "unavailable", "generation": generation, "target": "text/uri-list"})
            return
        self._send({"type": "prepare-files", "generation": generation, "files": files})
        if sys.platform != "darwin":
            # GTK has already installed its delayed provider at token time.
            self._send({"type": "start-files", "generation": generation})

    def _send_html(self, data, generation, target):
        files = []
        for source, path in _html_file_references(data):
            offered = self._offer_file(path)
            if offered is None:
                continue
            offered["source"] = source
            files.append(offered)
        self._send({"type": "html", "generation": generation, "target": target,
                    "files": files}, data)

    @staticmethod
    def _offer_file(path):
        name = os.path.basename(path.rstrip("/")) or "file"
        handle = path
        try:
            import requests
            response = requests.post(os.environ.get("RIM_SERVER", "") + "/clipboard/file-offer",
                                     json={"path": path}, verify=False, timeout=3)
            if response.ok:
                offered = response.json()
                handle = offered.get("handle", path)
                name = offered.get("name", name)
                size = offered.get("size", 0)
            else:
                log.warn("remote clipboard file offer failed for %r: HTTP %s", path, response.status_code)
                return None
        except Exception:
            log.warn("failed to register remote clipboard file %r", path, exc_info=True)
            return None
        return {"handle": handle, "name": name, "size": size, "mime": "",
                "url": os.environ.get("RIM_SERVER", "") + "/clipboard/file/" + handle}
