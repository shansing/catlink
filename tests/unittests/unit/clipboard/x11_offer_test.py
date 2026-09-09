"""Run with DISPLAY (Xvfb suffices); callbacks use the real X11 proxy methods."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import subprocess
import time
import struct


@unittest.skipUnless(os.environ.get("DISPLAY"), "requires an X11 display")
class X11OfferTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from xpra.x11.gtk.display_source import init_gdk_display_source
        init_gdk_display_source()
        from xpra.x11.selection.proxy import ClipboardProxy
        cls.proxy_class = ClipboardProxy

    def setUp(self):
        self.p = self.proxy_class(0, "CLIPBOARD")
        self.p._enabled = self.p._can_send = self.p._can_receive = True
        self.p._want_targets = self.p._greedy_client = True
        self.pending, self.sent = [], []
        self.p.get_contents = lambda target, cb: self.pending.append((target, cb))
        self.p.emit = lambda signal, data: self.sent.append(data)
        self.p.choose_targets = lambda targets: ("text/plain",) if "text/plain" in targets else (targets[0],)

    def tearDown(self):
        self.p.reset_incr_data()

    def property_reply(self, target, dtype, data, dformat=8):
        atom = f"CLIPBOARD-{self.p._offer_epoch}-{target}"
        with patch("xpra.x11.selection.proxy.X11Window", SimpleNamespace(
                GetWindowPropertyType=lambda *_: (dtype, dformat),
                XGetWindowProperty=lambda *_, **kw: data,
                XDeleteProperty=lambda *_: None)):
            self.p.do_property_notify(SimpleNamespace(atom=atom))
        return atom

    def test_incr_image_interleaved_with_ordinary_uri(self):
        results = []
        self.p.got_local_contents = lambda *args: results.append(args)
        self.property_reply("image/png", "INCR", struct.pack("@L", 6), 32)
        self.property_reply("image/png", "image/png", b"abc")
        self.property_reply("text/uri-list", "text/uri-list", b"file:///A.png\r\n")
        self.property_reply("image/png", "image/png", b"def")
        self.property_reply("image/png", "image/png", b"")
        self.assertEqual(results, [
            ("text/uri-list", "text/uri-list", 8, b"file:///A.png\r\n"),
            ("image/png", "image/png", 8, b"abcdef"),
        ])
        self.assertFalse(self.p.incr_data)

    def test_two_incremental_targets_do_not_share_chunks_or_timers(self):
        results = []
        self.p.got_local_contents = lambda *args: results.append(args)
        image = self.property_reply("image/png", "INCR", struct.pack("@L", 6), 32)
        uri = self.property_reply("text/uri-list", "INCR", struct.pack("@L", 4), 32)
        self.assertNotEqual(self.p.incr_data[image].timer, self.p.incr_data[uri].timer)
        self.property_reply("image/png", "image/png", b"abc")
        self.property_reply("text/uri-list", "text/uri-list", b"file")
        self.property_reply("text/uri-list", "text/uri-list", b"")
        self.assertIn(image, self.p.incr_data)
        self.property_reply("image/png", "image/png", b"def")
        self.property_reply("image/png", "image/png", b"")
        self.assertEqual(results, [
            ("text/uri-list", "text/uri-list", 8, b"file"),
            ("image/png", "image/png", 8, b"abcdef"),
        ])

    def test_invalid_incr_type_only_cancels_its_own_property(self):
        results = []
        self.p.got_local_contents = lambda *args: results.append(args)
        self.property_reply("image/png", "INCR", struct.pack("@L", 6), 32)
        self.property_reply("text/uri-list", "INCR", struct.pack("@L", 4), 32)
        self.property_reply("image/png", "image/png", b"abc")
        self.property_reply("image/png", "STRING", b"bad")
        self.property_reply("text/uri-list", "text/uri-list", b"file")
        self.property_reply("text/uri-list", "text/uri-list", b"")
        self.assertEqual(results, [
            ("image/png", "", 0, b""),
            ("text/uri-list", "text/uri-list", 8, b"file"),
        ])

    def test_owner_change_cancels_all_incremental_timers(self):
        self.property_reply("image/png", "INCR", struct.pack("@L", 6), 32)
        self.property_reply("text/uri-list", "INCR", struct.pack("@L", 4), 32)
        timers = [s.timer for s in self.p.incr_data.values()]
        from xpra.x11.selection.proxy import GLib
        with patch.object(GLib, "source_remove", wraps=GLib.source_remove) as remove:
            self.p._invalidate_local_reads()
            for timer in timers:
                remove.assert_any_call(timer)
        self.assertFalse(self.p.incr_data)

    def test_same_target_waiters_share_one_conversion(self):
        results, conversions = [], []
        with patch("xpra.x11.selection.proxy.get_wininfo", return_value="owner"), \
                patch("xpra.x11.selection.proxy.X11Window", SimpleNamespace(
                    XGetSelectionOwner=lambda *_: 42,
                    ConvertSelection=lambda *args, **kw: conversions.append(args))):
            for _ in range(2):
                self.proxy_class.get_contents(self.p, "text/uri-list", lambda *args: results.append(args))
        self.assertEqual(len(conversions), 1)
        self.p.got_local_contents("text/uri-list", "text/uri-list", 8, b"file:///A")
        self.assertEqual(results, [("text/uri-list", 8, b"file:///A")] * 2)

    def offer(self, timestamp, targets):
        self.p._block_owner_change = 1
        self.p.do_owner_changed()
        self.p._block_owner_change = 0
        self.p.offer_owner_xid = 42
        self.p.offer_selection_timestamp = timestamp
        self.p.targets = targets
        self.p.schedule_emit_token()

    def reply(self, target, data):
        requested, cb = self.pending.pop(0)
        self.assertEqual(requested, target)
        cb(target, 8, data)

    def test_content_identity_and_later_recopy(self):
        uri = ("text/uri-list", "text/plain")
        self.offer(1, uri)
        self.reply("text/uri-list", b"file:///A.txt\r\n")
        self.reply("text/plain", b"file:///A.txt")
        first = self.sent[-1][-1]["offer-id"]
        self.offer(2, uri + ("image/png",))
        self.reply("text/uri-list", b"file:///B.png\r\n")
        self.reply("text/plain", b"file:///B.png")
        self.assertGreater(self.sent[-1][-1]["offer-id"], first)

        self.offer(3, uri)
        self.reply("text/uri-list", b"file:///B.png\r\n")
        self.reply("text/plain", b"file:///B.png")
        first = self.sent[-1][-1]["offer-id"]
        self.offer(4, uri + ("image/png",))
        self.reply("text/uri-list", b"file:///B.png\r\n")
        self.reply("text/plain", b"file:///B.png")
        self.assertEqual(self.sent[-1][-1]["offer-id"], first)
        self.offer(5, uri + ("image/png",))
        self.reply("text/uri-list", b"file:///B.png\r\n")
        self.reply("text/plain", b"file:///B.png")
        self.assertGreater(self.sent[-1][-1]["offer-id"], first)

    def test_stale_payload_after_local_and_remote_transition(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.offer(1, ("text/plain",))
                if reverse:
                    self.p.claim = lambda: None
                    self.p.got_token((), None)
                else:
                    self.p._block_owner_change = 1
                    self.p.do_owner_changed()
                    self.p._block_owner_change = 0
                self.reply("text/plain", b"obsolete")
                self.assertFalse(self.sent)

    def test_stale_targets_does_not_replace_new_cache(self):
        self.offer(1, ())
        _, cb = self.pending.pop()
        self.p._block_owner_change = 1
        self.p.do_owner_changed()
        self.p._block_owner_change = 0
        self.p.targets = ("image/png",)
        cb("ATOM", 32, b"")
        self.assertEqual(self.p.targets, ("image/png",))
        self.assertFalse(self.sent)

    def test_same_event_duplicate_and_consecutive_texts(self):
        self.offer(1, ("text/plain",))
        self.reply("text/plain", b"A")
        first = self.sent[-1][-1]["offer-id"]
        self.p.schedule_emit_token()
        self.reply("text/plain", b"A")
        self.assertEqual(self.sent[-1][-1]["offer-id"], first)
        self.offer(2, ("text/plain",))
        self.reply("text/plain", b"B")
        self.assertGreater(self.sent[-1][-1]["offer-id"], first)

    def test_old_property_cannot_fulfill_current_request(self):
        self.p._offer_epoch = 5
        self.p.targets = ("image/png",)
        deleted = []
        with patch("xpra.x11.selection.proxy.X11Window", SimpleNamespace(
                XDeleteProperty=lambda xid, prop: deleted.append(prop))):
            self.p.do_property_notify(SimpleNamespace(atom="CLIPBOARD-4-TARGETS"))
        self.assertEqual(deleted, ["CLIPBOARD-4-TARGETS"])
        self.assertEqual(self.p.targets, ("image/png",))

    def test_same_timestamp_different_image_bytes(self):
        self.offer(1, ("image/png",))
        self.reply("image/png", b"A")
        first = self.sent[-1][-1]["offer-id"]
        self.offer(1, ("image/png",))
        self.reply("image/png", b"B")
        self.assertGreater(self.sent[-1][-1]["offer-id"], first)

    def test_native_x11_reads_after_reverse_claim(self):
        from xpra.os_util import gi_import
        from xpra.x11.gtk.bindings import init_x11_filter
        from xpra.x11.selection.clipboard import X11Clipboard
        init_x11_filter()
        packets = []
        helper = X11Clipboard(lambda *packet: packets.append(packet),
                              **{"clipboards.local": ("CLIPBOARD",)})
        helper.enable_selections(("CLIPBOARD",))
        helper.set_greedy_client(True)
        p = helper._get_proxy("CLIPBOARD")
        context = gi_import("GLib").MainContext.default()
        children = []
        try:
            for value in (b"A", b"B", b"C"):
                # Claim from the opposite direction between local copies.
                p.got_token(("UTF8_STRING",), {"UTF8_STRING": ("UTF8_STRING", 8, b"remote")})
                packets.clear()
                child = subprocess.Popen(["xclip", "-selection", "clipboard", "-target", "UTF8_STRING", "-quiet"],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                children.append(child)
                child.stdin.write(value)
                child.stdin.close()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    while context.pending():
                        context.iteration(False)
                    if any(len(packet) >= 8 and packet[7] == value for packet in packets):
                        break
                    time.sleep(0.005)
                else:
                    self.fail(f"X11 property response was not delivered: {value!r}, {packets!r}")
                self.assertTrue(all(packet[-1].get("origin") for packet in packets if packet[0] == "clipboard-token"))
        finally:
            helper.enable_selections(())
            helper.cleanup()
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=3)
