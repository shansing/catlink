"""Native GTK ownership regression; run under Xvfb with xclip installed."""
import os
from io import BytesIO
import shutil
import subprocess
import time
import unittest

from xpra.clipboard.catlink_bridge import CatlinkClipboardBridge
from xpra.gtk.clipboard import GTKClipboardProxy
from xpra.os_util import gi_import


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("xclip"), "requires X11 and xclip")
class GtkOwnerTest(unittest.TestCase):
    @staticmethod
    def png(color):
        from PIL import Image
        output = BytesIO()
        Image.new("RGB", (2, 2), color).save(output, "PNG")
        return output.getvalue()

    def setUp(self):
        self.context = gi_import("GLib").MainContext.default()
        self.p = GTKClipboardProxy("CLIPBOARD")
        self.p._enabled = self.p._can_receive = self.p._can_send = True
        self.bridge = CatlinkClipboardBridge(lambda *_: None)
        self.bridge.sock = object()
        self.bridge._send = lambda *_: None
        self.p.catlink_bridge = self.bridge
        self.sent = []
        self.p.emit = lambda _signal, data: self.sent.append(data)
        self.children = []

    def tearDown(self):
        self.p._enabled = False
        self.p.clipboard.clear()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=3)
        self.p.cleanup()

    def pump_until(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            while self.context.pending():
                self.context.iteration(False)
            if predicate():
                return
            time.sleep(0.005)
        self.fail("clipboard ownership transition did not complete within 2s")

    def local_copy(self, target, data):
        self.sent.clear()
        child = subprocess.Popen(["xclip", "-selection", "clipboard", "-target", target, "-quiet"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        child.stdin.write(data)
        child.stdin.close()
        self.pump_until(lambda: any(packet and target in packet[0] for packet in self.sent))
        received = []
        self.p.get_contents(target, lambda dtype, fmt, value: received.append(value))
        actual = received[0]
        if isinstance(actual, str):
            actual = actual.encode("utf-8")
        if target == "image/png":
            from PIL import Image
            self.assertEqual(Image.open(BytesIO(actual)).tobytes(), Image.open(BytesIO(data)).tobytes())
        else:
            self.assertEqual(bytes(actual), data)

    def test_remote_image_local_b_local_c_remote_image(self):
        for offer in (1, 2):
            self.bridge.handle("token", self.p, "CLIPBOARD", ("image/png",), {}, offer, "peer")
            self.p.got_token(("image/png",), None, False)
            self.pump_until(lambda: self.bridge.is_claimed(self.p))
            data = self.png("red")
            self.bridge._responses[(self.bridge.generation, "image/png")] = ("image/png", data)
            atom = gi_import("Gdk").Atom.intern("image/png", False)
            self.assertEqual(self.p.clipboard.wait_for_contents(atom).get_data(), data)
            self.local_copy("image/png", self.png("green"))
            self.local_copy("image/png", self.png("blue"))
            self.assertFalse(self.bridge._active)

    def test_remote_text_local_text_local_image(self):
        self.p.got_token(("UTF8_STRING",), {"UTF8_STRING": ("UTF8_STRING", 8, "远端".encode())})
        self.assertIsNotNone(self.p._catlink_text_owner)
        self.local_copy("UTF8_STRING", "本地".encode())
        self.local_copy("image/png", self.png("yellow"))

    def test_empty_token_then_immediate_local_copy(self):
        self.bridge.handle("token", self.p, "CLIPBOARD", ("image/png",), {}, 1, "peer")
        self.p.got_token(("image/png",), None, False)
        self.p.got_token((), None)
        self.local_copy("UTF8_STRING", b"after-empty")
