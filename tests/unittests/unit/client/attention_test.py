"""Window metadata dispatch and attention lifecycle, without a display server."""
import os
import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path
import xpra

from xpra.client.gtk3.window.attention import AttentionWindow
from xpra.client.gtk3.window.window import ClientWindow
from xpra.client.gui.window_base import ClientWindowBase
from xpra.util.objects import typedict


class Window(AttentionWindow):
    def __init__(self, client=None, wid=1, realized=True):
        self.wid = wid
        self._client = client or SimpleNamespace()
        self.active = False
        self.focused = False
        self._iconified = False
        self.realized = realized
        self.calls = []
        self.deferred = None
        self.init_window(self._client, typedict(), typedict())

    def connect(self, *args):
        pass

    def is_active(self):
        return self.active

    def has_toplevel_focus(self):
        return self.focused

    def get_realized(self):
        return self.realized

    def get_mapped(self):
        return self.realized

    def when_realized(self, name, callback):
        if self.realized:
            callback()
        else:
            self.deferred = callback

    def is_OR(self):
        return False

    def is_tray(self):
        return False

    def set_urgency_hint(self, value):
        self.calls.append(value)

    def metadata(self, value):
        ClientWindowBase.set_metadata(self, typedict({"attention-requested": value}))


class AttentionTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"XPRA_ATTENTION_ACK_ON_FOCUS": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.platform = patch.multiple("xpra.client.gtk3.window.attention", OSX=False, WIN32=False)
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def test_default_follows_remote(self):
        with patch.dict(os.environ):
            os.environ.pop("XPRA_ATTENTION_ACK_ON_FOCUS", None)
            w = Window()
        self.assertFalse(w.attention_ack_on_focus)
        w.focused = True
        w.metadata(True)
        w._attention_focus_changed()
        w._attention_focus_in()
        w._sync_attention()
        w.metadata(True)
        self.assertFalse(w.attention_state.acknowledged)
        self.assertEqual(w.calls, [True])
        w.catlink_reinit_destroy = True
        w.cleanup()
        with patch.dict(os.environ, {"XPRA_ATTENTION_ACK_ON_FOCUS": "0"}):
            replacement = Window(w._client)
        replacement.metadata(True)
        self.assertEqual(replacement.calls, [True])
        replacement.metadata(False)
        self.assertEqual(replacement.calls, [True, False])

    def test_dock_remote_mode(self):
        app = Mock()
        name = "xpra.platform.darwin.attention"
        with patch.dict(sys.modules, {"AppKit": SimpleNamespace(NSApp=app)}):
            sys.modules.pop(name, None)
            native = importlib.import_module(name)
        self.addCleanup(sys.modules.pop, name, None)
        with patch.dict(sys.modules, {name: native}), patch.object(native, "GLib") as glib, \
                patch.object(native, "envbool", return_value=False), \
                patch("xpra.client.gtk3.window.attention.OSX", True), \
                patch.dict(os.environ, {"XPRA_ATTENTION_ACK_ON_FOCUS": "0"}):
            w = Window()
            w.focused = True
            w.metadata(True)
            w._attention_focus_changed()
            native._check_focus()
            self.assertFalse(w.attention_state.acknowledged)
            app.dockTile.return_value.setBadgeLabel_.assert_called_once_with("…")
            glib.timeout_add.assert_not_called()
            w.metadata(False)
            app.dockTile.return_value.setBadgeLabel_.assert_called_with("")
            self.assertFalse(native._windows)

    def test_real_window_dispatch(self):
        self.assertIs(ClientWindow.set_attention_requested, AttentionWindow.set_attention_requested)

    def test_focus_and_new_request(self):
        w = Window()
        w.metadata(True)
        w.metadata(True)
        self.assertEqual(w.calls, [True])
        w.focused = True
        w._attention_focus_changed()
        w.focused = False
        w._attention_focus_changed()
        w.metadata(True)
        self.assertEqual(w.calls, [True, False])
        w.metadata(False)
        w.metadata(True)
        self.assertEqual(w.calls, [True, False, True])
        w.metadata(False)
        self.assertEqual(w.calls[-1], False)

    def test_initial_focus_and_deferred_clear(self):
        w = Window(realized=False)
        w.metadata(True)
        w.metadata(False)
        w.realized = True
        w.deferred()
        self.assertEqual(w.calls, [])
        w.focused = True
        w.metadata(True)
        self.assertTrue(w.attention_state.acknowledged)
        self.assertEqual(w.calls, [])

    def test_rebuild_and_destroy(self):
        w = Window()
        w.metadata(True)
        w.acknowledge_attention()
        w.catlink_reinit_destroy = True
        w.cleanup()
        replacement = Window(w._client)
        replacement.metadata(True)
        self.assertEqual(replacement.calls, [])
        replacement.cleanup()
        self.assertEqual(w._client._attention_states, {})
        replacement = Window(w._client)
        replacement.metadata(True)
        replacement.cleanup()
        self.assertEqual(replacement.calls, [True, False])

    def test_dock_badge_multiple_windows_and_focus(self):
        app = Mock()
        app.isActive.return_value = True
        name = "xpra.platform.darwin.attention"
        with patch.dict(sys.modules, {"AppKit": SimpleNamespace(NSApp=app)}):
            sys.modules.pop(name, None)
            native = importlib.import_module(name)
        self.addCleanup(sys.modules.pop, name, None)
        with patch.object(native, "GLib") as glib, patch.object(native, "envbool", return_value=False):
            glib.timeout_add.return_value = 123
            with patch("xpra.client.gtk3.window.attention.OSX", True), patch.dict(sys.modules, {name: native}):
                a, b = Window(wid=1), Window(wid=2)
                for w in (a, b):
                    w._iconified = True
                    w.metadata(True)
                badge = app.dockTile.return_value.setBadgeLabel_
                badge.assert_called_with("…")
                self.assertTrue(native._check_focus())
                self.assertFalse(a.attention_state.acknowledged)
                self.assertFalse(b.attention_state.acknowledged)
                a._iconified = False
                a.focused = True
                a._attention_focus_changed()
                self.assertTrue(a.attention_state.acknowledged)
                self.assertEqual(native._windows, {b})
                badge.assert_called_with("…")
                b.metadata(False)
                badge.assert_called_with("")
                self.assertFalse(native._windows)
                self.assertEqual(native._timer, 0)
                glib.source_remove.assert_called_once_with(123)
                b.metadata(True)
                badge.assert_called_with("…")
                b.cleanup()
                badge.assert_called_with("")
                self.assertFalse(native._windows)
                app.requestUserAttention_.assert_not_called()
                app.isActive.assert_not_called()

    def test_active_but_unfocused_or_minimized(self):
        for focused, iconified in ((False, False), (False, True), (True, True)):
            with self.subTest(focused=focused, iconified=iconified):
                w = Window()
                w.active = True
                w.focused = focused
                w._iconified = iconified
                w.metadata(True)
                w._attention_focus_changed()
                w._attention_focus_in()
                self.assertFalse(w.attention_state.acknowledged)
                self.assertEqual(w.calls, [True])
                w._iconified = False
                w.focused = True
                w._attention_focus_changed()
                self.assertEqual(w.calls, [True, False])

    def test_windows_native_flags(self):
        flash = Mock()
        gui = SimpleNamespace(get_window_handle=lambda window: 123)
        common = SimpleNamespace(user32=SimpleNamespace(FlashWindowEx=flash))
        name = "xpra.platform.win32.attention"
        with patch.dict(sys.modules, {"xpra.platform.win32.gui": gui,
                                      "xpra.platform.win32.common": common}):
            spec = importlib.util.spec_from_file_location(
                name, Path(xpra.__file__).parent / "platform/win32/attention.py")
            native = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(native)
        native.set_window_attention(object(), True)
        info = flash.call_args.args[0]._obj
        self.assertEqual(info.hwnd, 123)
        self.assertEqual(info.dwFlags, 15)
        self.assertEqual(info.dwTimeout, 0)
        native.set_window_attention(object(), False)
        self.assertEqual(flash.call_args.args[0]._obj.dwFlags, 0)


if __name__ == "__main__":
    unittest.main()
