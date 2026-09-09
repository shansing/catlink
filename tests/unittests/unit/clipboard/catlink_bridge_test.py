#!/usr/bin/env python3

import io
import json
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from xpra.clipboard.proxy import ClipboardProxyCore

from xpra.clipboard.catlink_bridge import (
    CatlinkClipboardBridge, _image_data_complete, _same_source_offer, _local_file_target,
    _html_file_references,
)
from xpra.gtk.clipboard import GTKClipboardProxy, _has_bridge_managed_target


class MemorySocket:
    def __init__(self, incoming=b""):
        self.incoming = incoming
        self.sent = bytearray()

    def sendall(self, data):
        self.sent.extend(data)

    def makefile(self, _mode):
        return io.BytesIO(self.incoming)


class BridgeProxy:
    _selection = "CLIPBOARD"


def make_stateful_bridge():
    bridge = CatlinkClipboardBridge.__new__(CatlinkClipboardBridge)
    # Initialize the production state machine without opening an IPC socket.
    CatlinkClipboardBridge.__init__(bridge, lambda *_args: None, enabled=True)
    bridge.sock = MemorySocket()
    return bridge, BridgeProxy()


class CatlinkBridgeBinaryFrameTest(unittest.TestCase):
    def test_windows_shell_provider_uses_completed_local_files(self):
        bridge, _ = make_stateful_bridge()
        bridge.generation = 1
        bridge._active = True
        proxy = SimpleNamespace(_selection="CLIPBOARD", clipboard=object())
        writes = []
        gtk = SimpleNamespace(gtk_clipboard_set_with_data=lambda *_: 1,
                              gtk_selection_data_set=lambda *args: writes.append(args))
        gdk = SimpleNamespace(gdk_atom_intern=lambda name, _: name)
        bridge._load_gtk_clipboard_api = lambda: (gtk, gdk)
        bridge._gobject_pointer = lambda _: 1
        bridge._prepare_windows_hdrop = lambda *_: None
        bridge._request_remote = lambda *_: self.fail("Shell alias must not request remote Shell data")
        bridge._wait_response = lambda generation, target: (target, b"file:///C:/Temp/local.txt")
        with patch("xpra.clipboard.catlink_bridge.sys.platform", "win32"), \
                patch("xpra.clipboard.win32_formats._windows_path_pidl", return_value=b"\0\0") as parse:
            self.assertTrue(bridge.claim(proxy, (
                "text/uri-list", "text/x-moz-url", "text/plain", "UTF8_STRING",
                "text/plain;charset=UTF-8", "TEXT", "STRING", "x-special/gnome-copied-files",
            ), {"text/plain": ("text/plain", 8, b"file:///lzcapp/a.pptx")}))
            entries, names, get_data, _ = bridge._gtk_provider
            self.assertEqual(names, (b"Shell IDList Array",))
            get_data(None, None, names.index(b"Shell IDList Array"), None)
        parse.assert_called_once_with(r"C:\Temp\local.txt")
        self.assertEqual(writes[0][1], b"Shell IDList Array")
        self.assertEqual(writes[0][-1], 16)

    def test_windows_file_filter_preserves_real_text_and_images(self):
        bridge, _ = make_stateful_bridge()
        bridge.generation = 1
        bridge._active = True
        proxy = SimpleNamespace(_selection="CLIPBOARD", clipboard=object())
        bridge._load_gtk_clipboard_api = lambda: (
            SimpleNamespace(gtk_clipboard_set_with_data=lambda *_: 1), object())
        bridge._gobject_pointer = lambda _: 1
        bridge._prepare_windows_hdrop = lambda *_: None
        targets = ("text/uri-list", "text/plain", "UTF8_STRING", "image/png", "text/html")
        with patch("xpra.clipboard.catlink_bridge.sys.platform", "win32"):
            self.assertTrue(bridge.claim(proxy, targets,
                                        {"text/plain": ("text/plain", 8, b"Actual caption")}))
            self.assertEqual(bridge._gtk_provider[1],
                             (b"text/plain", b"UTF8_STRING", b"image/png", b"text/html", b"Shell IDList Array"))
        with patch("xpra.clipboard.catlink_bridge.sys.platform", "linux"):
            self.assertTrue(bridge.claim(proxy, targets,
                                        {"text/plain": ("text/plain", 8, b"file:///lzcapp/a")}))
            self.assertIn(b"text/uri-list", bridge._gtk_provider[1])
            self.assertIn(b"x-special/gnome-copied-files", bridge._gtk_provider[1])

    def test_server_paths_do_not_use_client_path_rules(self):
        bridge, _ = make_stateful_bridge()
        bridge.generation = 1
        bridge._active = True
        paths = []
        bridge._offer_file = lambda path: paths.append(path) or {"handle": path}
        # Windows 3.13 rejects slash-rooted paths in isabs(). Remote paths
        # must work regardless of the host Python version running this test.
        with patch("xpra.clipboard.catlink_bridge.os.path", SimpleNamespace(isabs=lambda _: False)):
            bridge._send_uri_files("file:///lzcapp/a%20b.txt\nfile://other/a.txt", 1)
            references = _html_file_references('<img src="file:///lzcapp/a%20b.png">')
        self.assertEqual(paths, ["/lzcapp/a b.txt"])
        self.assertEqual(references, [("file:///lzcapp/a%20b.png", "/lzcapp/a b.png")])

    def test_reentrant_waiters_share_response(self):
        for payload in (b"file:///tmp/a", b""):
            with self.subTest(payload=payload):
                bridge, _ = make_stateful_bridge()
                bridge.generation = 1
                bridge._active = True
                nested = []
                pending = [True]
                def iteration(_block):
                    pending[0] = False
                    bridge._responses[(1, "text/uri-list")] = ("text/uri-list", payload)
                    nested.append(bridge._wait_response(1, "text/uri-list", pump_main=False))
                context = SimpleNamespace(pending=lambda: pending[0], iteration=iteration)
                glib = SimpleNamespace(MainContext=SimpleNamespace(default=lambda: context))
                # Bound the test even if a regression consumes the cached reply.
                def after_iteration(_seconds):
                    if not bridge._responses:
                        bridge._active = False
                with patch("xpra.os_util.gi_import", return_value=glib), \
                        patch("xpra.clipboard.catlink_bridge.time.sleep", side_effect=after_iteration):
                    outer = bridge._wait_response(1, "text/uri-list")
                self.assertEqual(nested, [("text/uri-list", payload)])
                self.assertEqual(outer, ("text/uri-list", payload))

    def test_late_empty_response_does_not_erase_success(self):
        bridge, _ = make_stateful_bridge()
        bridge.generation = 1
        bridge._active = True
        frames = bytearray()
        for payload in (b"", b"image-data", b""):
            frames.extend(json.dumps({"type": "data-response", "generation": 1,
                                      "target": "image/png", "binary_size": len(payload)}).encode() + b"\n")
            frames.extend(payload)
        bridge.sock = MemorySocket(bytes(frames))
        bridge._reader()
        self.assertEqual(bridge._responses[(1, "image/png")], ("image/png", b"image-data"))

    def test_send_binary_frame(self):
        bridge = CatlinkClipboardBridge.__new__(CatlinkClipboardBridge)
        bridge.sock = MemorySocket()
        bridge._send_lock = threading.Lock()
        payload = b"\x00raw\nimage\xff"
        bridge._send({"type": "image", "target": "image/png"}, payload)
        stream = io.BytesIO(bridge.sock.sent)
        header = json.loads(stream.readline())
        self.assertEqual(header["binary_size"], len(payload))
        self.assertNotIn("data", header)
        self.assertEqual(stream.read(len(payload)), payload)

    def test_read_binary_frame(self):
        payload = b"file:///tmp/a\nfile:///tmp/b\x00"
        header = json.dumps({
            "type": "data-response",
            "generation": 7,
            "target": "text/uri-list",
            "binary_size": len(payload),
        }).encode() + b"\n"
        bridge = CatlinkClipboardBridge.__new__(CatlinkClipboardBridge)
        bridge.sock = MemorySocket(header + payload)
        bridge._lock = threading.Condition()
        bridge._responses = {}
        bridge.generation = 7
        bridge._active = True
        bridge._reader()
        key = (7, "text/uri-list")
        with bridge._lock:
            self.assertEqual(bridge._responses[key], ("text/uri-list", payload))

    def test_source_offer_identity_controls_merging(self):
        self.assertTrue(_same_source_offer(42, 42))
        self.assertFalse(_same_source_offer(42, 43))
        self.assertFalse(_same_source_offer(0, 0))

    def test_file_aliases_use_local_paths_and_correct_encoding(self):
        uris = b"file:///tmp/local%20image.tiff\n"
        self.assertEqual(_local_file_target("x-special/gnome-copied-files", uris), b"copy\n" + uris)
        self.assertEqual(_local_file_target("text/x-moz-url", uris).decode("utf-16le"),
                         "file:///tmp/local%20image.tiff\r\nlocal image.tiff")

    def test_incomplete_bmp_is_rejected(self):
        declared = 12_081_654
        truncated = b"BM" + declared.to_bytes(4, "little") + b"\0" * (4 * 1024 * 1024 - 6)
        self.assertFalse(_image_data_complete("image/bmp", truncated))
        complete = b"BM" + (10).to_bytes(4, "little") + b"1234"
        self.assertTrue(_image_data_complete("image/bmp", complete))

    def test_token_and_contents_reject_same_truncated_image(self):
        data = b"\x89PNG\r\n\x1a\ntruncated"
        self.assertFalse(_image_data_complete("image/png", data))
        for eager in (True, False):
            bridge, proxy = make_stateful_bridge()
            sent = []
            bridge._send = lambda message, binary=b"": sent.append((message, binary))
            values = {"image/png": ("image/png", 8, data)} if eager else {}
            bridge.handle("token", proxy, "CLIPBOARD", ("image/png", "text/uri-list"), values, 1, "peer")
            if not eager:
                bridge._request_generations[42] = (bridge.generation, "image/png")
                bridge.handle("contents", proxy, "CLIPBOARD", ("image/png", "image/png", 8, data), 42)
            self.assertFalse(any(m["type"] == "image" for m, _ in sent))
            self.assertTrue(any(m["type"] == "unavailable" and m["target"] == "image/png" for m, _ in sent))
            self.assertIn((bridge.generation, "image/png"), bridge._unavailable)
            self.assertIn("text/uri-list", bridge._targets)

    def test_text_only_token_does_not_reuse_previous_managed_targets(self):
        bridge, proxy = make_stateful_bridge()
        bridge.handle("token", proxy, "CLIPBOARD", ("image/png",), {}, 1, "peer")
        bridge.handle("token", proxy, "CLIPBOARD", ("text/plain",), {}, 2, "peer")
        self.assertFalse(bridge.claim(proxy, ("text/plain", "UTF8_STRING"), {}))
        self.assertFalse(bridge._active)

    def test_claim_targets_follow_current_token_generation(self):
        bridge = CatlinkClipboardBridge.__new__(CatlinkClipboardBridge)
        bridge._active = True
        bridge._proxy = object()
        bridge._targets = ("text/uri-list",)
        proxy = bridge._proxy
        self.assertEqual(bridge._claim_targets(proxy, ("image/png",)), ("image/png",))

    def test_two_phase_uri_then_image_requires_reclaim(self):
        bridge = CatlinkClipboardBridge.__new__(CatlinkClipboardBridge)
        bridge.generation = 1
        bridge._active = True
        proxy = object()
        bridge._claimed_proxy = proxy
        bridge._claimed_targets = ("text/uri-list",)
        bridge._claimed_generation = bridge.generation
        self.assertTrue(bridge.is_claimed(proxy, ("text/uri-list",)))
        self.assertFalse(bridge.is_claimed(proxy, ("text/uri-list", "image/png")))

    def test_same_targets_new_generation_requires_reclaim(self):
        bridge, proxy = make_stateful_bridge()
        bridge._active = True
        bridge._claimed_proxy = proxy
        bridge._claimed_targets = ("text/uri-list",)
        bridge._claimed_generation = bridge.generation
        bridge.generation += 1
        self.assertFalse(bridge.is_claimed(proxy, ("text/uri-list",)))

    def test_mixed_offer_sequence_does_not_resurrect_managed_state(self):
        bridge, proxy = make_stateful_bridge()
        origin = "peer:CLIPBOARD:x11:600004"

        def managed(targets, offer_id, data=None):
            return bridge.handle("token", proxy, "CLIPBOARD", targets,
                                 data or {}, offer_id, origin)

        self.assertTrue(managed(("text/uri-list",), 40))
        file_generation = bridge.generation
        self.assertTrue(managed(("text/uri-list",), 41))
        self.assertGreater(bridge.generation, file_generation)

        image_generation = bridge.generation
        self.assertTrue(managed(("image/png",), 42,
                                {"image/png": ("image/png", 8, b"png")}))
        self.assertGreater(bridge.generation, image_generation)

        # An unsupported rich target may fall back through normal Xpra, but
        # it must retire the bridge state before the next managed offer.
        self.assertFalse(managed(("application/x-custom-object", "text/plain"), 43))
        self.assertFalse(bridge._active)
        unsupported_generation = bridge.generation

        self.assertTrue(managed(("text/uri-list",), 44))
        self.assertTrue(bridge._active)
        self.assertGreater(bridge.generation, unsupported_generation)
        self.assertEqual(bridge._targets, ("text/uri-list",))

    def test_empty_token_invalidates_previous_managed_offer(self):
        bridge, proxy = make_stateful_bridge()
        bridge.handle("token", proxy, "CLIPBOARD", ("image/png",), {}, 50, "peer")
        self.assertTrue(bridge._active)
        bridge.invalidate_native(proxy)
        self.assertFalse(bridge._active)
        self.assertEqual(bridge._targets, ())

    def test_remote_origin_does_not_replace_local_outgoing_origin(self):
        proxy = ClipboardProxyCore("CLIPBOARD")
        local = proxy.set_local_clipboard_origin("local-copy")
        proxy.set_remote_clipboard_origin("peer-copy")
        self.assertEqual(proxy.get_clipboard_token_metadata()["origin"], local)
        self.assertFalse(proxy.is_local_clipboard_origin("peer-copy"))

    def test_realistic_file_then_image_is_new_generation(self):
        bridge, proxy = make_stateful_bridge()
        origin = "peer:CLIPBOARD"
        self.assertTrue(bridge.handle("token", proxy, "CLIPBOARD",
                                      ("text/uri-list", "text/plain"),
                                      {"text/plain": ("text/plain", 8, b"file:///a.txt")},
                                      10, origin))
        first_generation = bridge.generation
        self.assertEqual(bridge._targets, ("text/uri-list", "text/plain"))
        self.assertTrue(bridge.handle("token", proxy, "CLIPBOARD",
                                      ("text/uri-list", "image/png", "text/plain"),
                                      {"image/png": ("image/png", 8, b"png")},
                                      11, origin))
        self.assertGreater(bridge.generation, first_generation)
        self.assertIn("image/png", bridge._targets)

    def test_two_phase_image_offer_merges_only_with_same_offer_id(self):
        bridge, proxy = make_stateful_bridge()
        origin = "peer:CLIPBOARD:x11:600004"
        bridge.handle("token", proxy, "CLIPBOARD",
                      ("text/uri-list", "text/plain"),
                      {"text/plain": ("text/plain", 8, b"file:///first.png")},
                      20, origin)
        generation = bridge.generation
        bridge.handle("token", proxy, "CLIPBOARD",
                      ("text/uri-list", "text/plain", "image/png"),
                      {"image/png": ("image/png", 8, b"png-first")},
                      20, origin)
        self.assertEqual(bridge.generation, generation)
        self.assertIn("image/png", bridge._targets)

        # Copying the same image again later is still a new user operation.
        # The producer must issue a new offer-id even when origin and bytes
        # are identical; the client must not merge it with the old provider.
        bridge.handle("token", proxy, "CLIPBOARD",
                      ("text/uri-list", "text/plain"),
                      {"text/plain": ("text/plain", 8, b"file:///first.png")},
                      21, origin)
        self.assertGreater(bridge.generation, generation)

    def test_late_response_is_bound_to_request_generation(self):
        bridge, proxy = make_stateful_bridge()
        bridge.handle("token", proxy, "CLIPBOARD", ("image/png",), {}, 30, "peer")
        old_generation = bridge.generation
        bridge._request_generations[7] = (old_generation, "image/png")
        bridge.handle("token", proxy, "CLIPBOARD", ("image/png",), {}, 31, "peer")

        self.assertTrue(bridge.handle("contents", proxy, "CLIPBOARD",
                                      ("image/png", "image/png", 8, b"old-image"), 7))
        self.assertNotIn((bridge.generation, "image/png"), bridge._received)

    def test_uri_file_offer_is_idempotent_with_eager_and_delayed_paths(self):
        bridge, _proxy = make_stateful_bridge()
        calls = []
        bridge._offer_file = lambda path: calls.append(path) or {"handle": path}
        bridge._send = lambda value, binary=b"": calls.append(value)
        bridge.generation = 4
        bridge._active = True
        raw = "file:///tmp/same.png\nfile:///tmp/other.png\n"
        bridge._send_uri_files(raw, 4)
        bridge._send_uri_files(raw, 4)
        self.assertEqual(calls.count("/tmp/same.png"), 1)
        self.assertEqual(calls.count("/tmp/other.png"), 1)
        self.assertEqual([x["type"] for x in calls if isinstance(x, dict)], ["prepare-files", "start-files"])

    def test_native_owner_lifecycle_and_selections(self):
        bridge, _ = make_stateful_bridge()
        cleared = []
        bridge._schedule_native_clear = lambda *args: cleared.append(args)
        native = SimpleNamespace(clear=None)
        def install(_ptr, _entries, _count, get_data, clear_data, _user):
            if native.clear:
                native.clear(None, None)
            native.clear = clear_data
            return 1
        bridge._load_gtk_clipboard_api = lambda: (SimpleNamespace(gtk_clipboard_set_with_data=install), object())
        bridge._gobject_pointer = lambda _: 1
        sent = []
        def proxy(selection):
            return SimpleNamespace(
                _selection=selection, _enabled=True, _can_receive=True,
                _have_token=False, _got_token_events=0, _owner_change_embargo=0,
                _catlink_local_owner=False, _catlink_text_owner=None, catlink_bridge=bridge,
                clipboard=object(), cancel_emit_token=lambda: None,
                set_local_clipboard_origin=lambda: None,
                schedule_emit_token=lambda: sent.append(selection))
        p, primary = proxy("CLIPBOARD"), proxy("PRIMARY")
        for offer in (1, 2):
            bridge.handle("token", p, "CLIPBOARD", ("image/png",), {}, offer, "peer")
            GTKClipboardProxy.got_token(p, ("image/png",), None, False)
            GTKClipboardProxy.do_owner_changed(p)
            self.assertTrue(bridge.is_claimed(p))
        self.assertFalse(sent, "our native replacement echoed as a local copy")
        generation = bridge.generation
        GTKClipboardProxy.got_token(primary, (), None)
        self.assertEqual(generation, bridge.generation)
        self.assertTrue(bridge.is_claimed(p))

        native.clear(None, None)  # real local B takes over from remote A
        self.assertFalse(bridge._active)
        GTKClipboardProxy.do_owner_changed(p)  # within the old 5s embargo
        self.assertEqual(sent, ["CLIPBOARD"])
        GTKClipboardProxy.do_owner_changed(p)
        self.assertEqual(sent, ["CLIPBOARD", "CLIPBOARD"])
        self.assertFalse(cleared, "local takeover must not clear B's clipboard")
        # Copy A remotely again, then receive an empty token on CLIPBOARD.
        bridge.handle("token", p, "CLIPBOARD", ("image/png",), {}, 3, "peer")
        GTKClipboardProxy.got_token(p, ("image/png",), None, False)
        GTKClipboardProxy.got_token(p, (), None)
        self.assertFalse(bridge._active)
        self.assertEqual(len(cleared), 1)

    def test_disabled_bridge_does_not_claim(self):
        bridge, proxy = make_stateful_bridge()
        bridge.enabled = False
        self.assertFalse(bridge.handle("token", proxy, "CLIPBOARD", ("image/png",), {}, 1, "peer"))
        self.assertFalse(bridge.claim(proxy, ("image/png",)))

    def test_foreign_response_and_retired_wait(self):
        bridge, proxy = make_stateful_bridge()
        bridge.handle("token", proxy, "CLIPBOARD", ("image/png",), {}, 1, "peer")
        self.assertFalse(bridge.handle("contents", proxy, "CLIPBOARD",
                                      ("image/png", "image/png", 8, b"foreign"), 999))
        generation = bridge.generation
        bridge.invalidate_native(proxy, clear=False)
        self.assertIsNone(bridge._wait_response(generation, "image/png"))
        self.assertIsNone(bridge._wait_inline(generation, "text/plain"))

    def test_duplicate_token_does_not_advance_generation(self):
        bridge, proxy = make_stateful_bridge()
        args = (("text/uri-list", "text/plain"),
                {"text/plain": ("text/plain", 8, b"file:///a.txt")}, 0, "")
        self.assertTrue(bridge.handle("token", proxy, "CLIPBOARD", *args))
        generation = bridge.generation
        self.assertTrue(bridge.handle("token", proxy, "CLIPBOARD", *args))
        self.assertEqual(bridge.generation, generation)
        self.assertTrue(bridge._last_token_duplicate)

    def test_file_then_text_invalidates_managed_offer(self):
        bridge, proxy = make_stateful_bridge()
        bridge.handle("token", proxy, "CLIPBOARD", ("text/uri-list",), {}, 3, "peer")
        self.assertTrue(bridge._active)
        self.assertFalse(bridge.handle("token", proxy, "CLIPBOARD",
                                       ("text/plain", "UTF8_STRING"),
                                       {"UTF8_STRING": ("UTF8_STRING", 8, b"hello")},
                                       4, "peer"))
        self.assertFalse(bridge._active)
        self.assertEqual(bridge._targets, ())

    def test_primary_token_cannot_replace_clipboard_offer(self):
        bridge, proxy = make_stateful_bridge()
        bridge.handle("token", proxy, "CLIPBOARD", ("text/uri-list",), {}, 1, "peer")
        generation = bridge.generation
        self.assertFalse(bridge.handle("token", proxy, "PRIMARY", ("image/png",), {}, 2, "peer"))
        self.assertEqual(bridge.generation, generation)

    def test_managed_token_without_data_is_not_plain_transition(self):
        # Two-phase offers may carry targets first and data later.  This must
        # not trigger native invalidation or advance the bridge generation.
        self.assertTrue(_has_bridge_managed_target(("text/uri-list", "image/png")))
        self.assertTrue(_has_bridge_managed_target(("text/html",)))
        self.assertFalse(_has_bridge_managed_target(("text/plain", "UTF8_STRING")))



def main():
    unittest.main()


if __name__ == "__main__":
    main()
