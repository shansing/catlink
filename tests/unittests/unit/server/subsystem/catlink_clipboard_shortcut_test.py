import unittest
from types import SimpleNamespace
from unittest.mock import patch

from xpra.net.common import Packet
from xpra.server.subsystem.keyboard import KeyboardServer, catlink_clipboard_shortcut_key


class CatlinkClipboardShortcutTest(unittest.TestCase):
    def setUp(self):
        self.server = KeyboardServer()
        self.server.readonly = False
        self.server._id_to_window = {1: object()}
        self.events = []
        self.server.fake_key = lambda keycode, pressed: self.events.append((keycode, pressed))
        self.source = SimpleNamespace(
            uuid="client", catlink_clipboard_shortcut_tap=True,
            catlink_clipboard_shortcut_keys={},
        )

    def handle(self, name, pressed=True, modifiers=("control",), keycode=42):
        return self.server.catlink_handle_clipboard_shortcut(
            self.source, 1, keycode, name, ord(name[0]), 55,
            list(modifiers), False, pressed, name,
        )

    def test_matching(self):
        for key in ("c", "v", "x", "C", "V", "X"):
            self.assertEqual(catlink_clipboard_shortcut_key(key, ["control", "lock"]), key.lower())
        for modifiers in ((), ("mod1",), ("control", "shift"), ("control", "mod1"),
                          ("control", "mod3")):
            self.assertEqual(catlink_clipboard_shortcut_key("v", list(modifiers)), "")
        self.assertEqual(catlink_clipboard_shortcut_key("a", ["control"]), "")

    def test_press_release_and_repeat(self):
        self.assertTrue(self.handle("v"))
        self.assertEqual(self.events, [(55, True), (55, False)])
        self.assertTrue(self.handle("v"))
        self.assertEqual(len(self.events), 2)
        self.assertTrue(self.handle("V", False, ()))
        self.assertTrue(self.handle("v"))
        self.assertEqual(len(self.events), 4)

    def test_stale_press_and_changed_modifiers(self):
        with patch("xpra.server.subsystem.keyboard.monotonic", side_effect=(1, 60, 61)):
            self.assertTrue(self.handle("c"))
            self.assertTrue(self.handle("c"))
            self.assertTrue(self.handle("c"))
        self.assertEqual(len(self.events), 4)
        self.assertFalse(self.handle("c", modifiers=()))
        self.assertEqual(self.source.catlink_clipboard_shortcut_keys, {})

    def test_disabled_and_other_keys(self):
        self.source.catlink_clipboard_shortcut_tap = False
        self.assertFalse(self.handle("v"))
        self.source.catlink_clipboard_shortcut_tap = True
        self.assertFalse(self.handle("z"))
        self.assertFalse(self.handle("v", modifiers=("mod1",)))
        self.assertEqual(self.events, [])

    def test_packet_path_consumes_release_after_control_is_released(self):
        self.source.keyboard_config = SimpleNamespace(sync=True)
        self.source.is_modifier = lambda *args: False
        self.source.make_keymask_match = lambda *args, **kwargs: None
        self.source.emit = lambda *args: None
        self.server.get_server_source = lambda proto: self.source
        self.server.set_ui_driver = lambda source: None
        self.server.get_keycode = lambda *args: (55, -1)
        self.server._focus = lambda *args: None

        def packet(pressed, modifiers):
            return Packet("key-action", 1, "v", pressed, modifiers, ord("v"), "v", 42, 0)

        self.server._process_key_action(None, packet(True, ["control"]))
        self.server._process_key_action(None, packet(False, []))
        self.assertEqual(self.events, [(55, True), (55, False)])
        self.assertEqual(self.source.catlink_clipboard_shortcut_keys, {})


if __name__ == "__main__":
    unittest.main()
