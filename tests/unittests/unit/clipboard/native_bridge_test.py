"""Exercise native backend contracts without requiring AppKit or user32."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from xpra.clipboard.catlink_bridge import CatlinkClipboardBridge


class Item:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        self.values = {}
        return self

    def setString_forType_(self, value, kind):
        self.values[kind] = value

    def setDataProvider_forTypes_(self, provider, types):
        self.types = types
        self.provider = provider

    def configure(self, bridge, generation, types):
        self.types = types
        return self


class Pasteboard:
    def __init__(self):
        self.count = 1
        self.items = []
        self.success = True

    def changeCount(self):
        return self.count

    def clearContents(self):
        self.count += 1
        self.items = []

    def writeObjects_(self, items):
        self.count += 1
        if self.success:
            self.items = items
        return self.success


class MacNativeTest(unittest.TestCase):
    def setUp(self):
        self.bridge = CatlinkClipboardBridge.__new__(CatlinkClipboardBridge)
        self.bridge.generation = 1
        self.bridge._active = True
        self.bridge.sock = object()
        self.bridge.accepts = lambda proxy: True
        self.bridge._claim_targets = lambda proxy, targets: targets
        self.bridge._send = Mock()
        self.bridge._wait_response = Mock(return_value=("text/uri-list", b"file:///tmp/a\r\nfile:///tmp/b\r\n"))
        self.pb = Pasteboard()
        self.proxy = SimpleNamespace(pasteboard=self.pb, change_count=1, _have_token=True,
                                     local_clipboard_changed=Mock())
        self.proxy.update_change_count = Mock(
            side_effect=lambda: setattr(self.proxy, "change_count", self.pb.changeCount()))
        self.workers = []
        self.idles = []
        appkit = SimpleNamespace(NSPasteboardItem=Item, NSPasteboardTypePNG="public.png",
                                 NSPasteboardTypeTIFF="public.tiff", NSPasteboardTypeFileURL="public.file-url")
        patches = (
            patch.dict("sys.modules", {"AppKit": appkit}),
            patch("xpra.clipboard.catlink_bridge._get_macos_provider_class", return_value=Item),
            patch("xpra.clipboard.catlink_bridge.threading.Thread",
                  side_effect=lambda target, **kw: SimpleNamespace(start=lambda: self.workers.append(target))),
            patch("xpra.os_util.gi_import", return_value=SimpleNamespace(idle_add=self.idles.append)),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def finish_files(self):
        self.workers.pop(0)()
        self.assertFalse(self.idles.pop(0)())

    def test_original_image_types_and_count(self):
        for mime, native in (("image/png", "public.png"), ("image/jpeg", "public.jpeg"),
                             ("image/tiff", "public.tiff")):
            self.assertTrue(self.bridge.claim_macos(self.proxy, (mime,)))
            self.assertEqual(self.pb.items[0].types, [native])
            self.assertEqual(self.pb.items[0].provider.types, {native: mime})
        self.assertEqual(self.proxy.update_change_count.call_count, 3)

    def test_multiple_files_are_separate_items(self):
        self.assertTrue(self.bridge.claim_macos(self.proxy, ("text/uri-list",)))
        self.assertEqual(self.pb.count, 1)  # no placeholder overwrites existing clipboard
        self.finish_files()
        self.assertEqual([i.provider._catlink_file_uri for i in self.pb.items],
                         ["file:///tmp/a", "file:///tmp/b"])
        self.assertTrue(all(not i.values for i in self.pb.items))  # no premature URL
        self.assertEqual(self.bridge._send.call_args.args[0]["type"], "start-files")
        self.proxy.update_change_count.assert_called_once()

    def test_file_wait_expiry_is_not_download_failure(self):
        self.bridge._wait_response.side_effect = [None, ("text/uri-list", b"file:///tmp/late\r\n")]
        self.bridge.claim_macos(self.proxy, ("text/uri-list",))
        self.finish_files()
        self.assertEqual(self.bridge._wait_response.call_count, 2)
        self.assertEqual(self.bridge._send.call_count, 2)  # layout request + start
        self.assertEqual(self.pb.items[0].provider._catlink_file_uri, "file:///tmp/late")

    def test_file_failure_does_not_hide_image(self):
        self.bridge._wait_response.return_value = ("catlink/file-layout", b"")
        self.bridge.claim_macos(self.proxy, ("image/png", "text/uri-list"))
        self.assertEqual(self.pb.items[0].types, ["public.png"])
        self.workers.pop(0)()
        self.assertEqual(self.idles, [])
        self.assertEqual(self.pb.items[0].types, ["public.png"])

    def test_local_copy_during_download_is_preserved(self):
        self.bridge.claim_macos(self.proxy, ("text/uri-list",))
        self.pb.count += 1
        self.finish_files()
        self.assertEqual(self.pb.count, 2)
        self.proxy.update_change_count.assert_not_called()

    def test_old_generation_cannot_publish(self):
        self.bridge.claim_macos(self.proxy, ("text/uri-list",))
        self.bridge.generation += 1
        self.workers.pop(0)()
        self.assertEqual(self.idles, [])
        self.assertEqual(self.pb.count, 1)

    def test_failed_write_still_updates_count(self):
        self.pb.success = False
        self.assertFalse(self.bridge.claim_macos(self.proxy, ("image/jpeg",)))
        self.proxy.update_change_count.assert_called_once()

    def test_native_poll_does_not_echo_own_write(self):
        import xpra
        source = Path(xpra.__file__).parent / "platform/darwin/ctypes_clipboard.py"
        tree = ast.parse(source.read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == "timer_clipboard_check")
        env = {"log": Mock()}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), env)
        self.bridge.claim_macos(self.proxy, ("image/tiff",))
        env["timer_clipboard_check"](self.proxy)
        self.proxy.local_clipboard_changed.assert_not_called()
        self.pb.count += 1
        env["timer_clipboard_check"](self.proxy)
        self.proxy.local_clipboard_changed.assert_called_once()

    def test_provider_requests_original_format(self):
        import xpra
        source = Path(xpra.__file__).parent / "clipboard/catlink_bridge.py"
        tree = ast.parse(source.read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == "pasteboard_item_provideDataForType_")
        env = {"log": Mock()}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), env)
        self.bridge._request_remote = Mock()
        item = SimpleNamespace(setData_forType_=Mock())
        provider = SimpleNamespace(_catlink_bridge=self.bridge, _catlink_generation=1,
                                   _catlink_types={"public.jpeg": "image/jpeg"})
        foundation = SimpleNamespace(NSData=SimpleNamespace(dataWithBytes_length_=lambda data, size: data))
        with patch.dict("sys.modules", {"Foundation": foundation}):
            self.bridge._wait_response.return_value = ("image/jpeg", b"jpeg-data")
            env[method.name](provider, self.pb, item, "public.jpeg")
            self.bridge._request_remote.assert_called_once_with(1, "image/jpeg")
            item.setData_forType_.assert_called_once_with(b"jpeg-data", "public.jpeg")
            self.bridge.generation = 2
            env[method.name](provider, self.pb, item, "public.jpeg")
            self.assertEqual(item.setData_forType_.call_count, 1)




class DisabledBridgeTest(unittest.TestCase):
    def test_gtk_sender_reads_files_and_images_without_bridge(self):
        from xpra.gtk.clipboard import GTKClipboardProxy
        from xpra.clipboard.win32_formats import encode_hdrop
        atom = SimpleNamespace(name=lambda: "image/png")
        clipboard = SimpleNamespace(wait_for_uris=lambda: [], wait_for_text=lambda: "local text",
                                     wait_for_targets=lambda: (True, [atom]))
        proxy = SimpleNamespace(clipboard=clipboard, _have_token=False, catlink_bridge=None)
        reply = Mock()
        clipboard.wait_for_contents = lambda _: SimpleNamespace(get_data=lambda: encode_hdrop(["C:\\files\\A.txt"]))
        with patch.dict("os.environ", {"LZC_CDE_UID": "uid", "LZC_CLIENT_ID": "client"}):
            GTKClipboardProxy.get_contents(proxy, "text/uri-list", reply)
        self.assertIn("/lzcapp/clientfs/uid/.byid/client/C%3A/files/A.txt", reply.call_args.args[2])
        GTKClipboardProxy.get_contents(proxy, "UTF8_STRING", reply)
        self.assertEqual(reply.call_args.args[2], "local text")
        clipboard.wait_for_contents = lambda _: SimpleNamespace(get_data=lambda: b"gtk converted bitmap")
        with patch("xpra.gtk.clipboard.filter_data", side_effect=lambda **kw: kw["data"]):
            GTKClipboardProxy.get_contents(proxy, "image/png", reply)
        self.assertEqual(reply.call_args.args[2], b"gtk converted bitmap")

    def test_disabled_never_connects(self):
        with patch.dict("os.environ", {"CATLINK_CLIPBOARD_IPC": "127.0.0.1:9999|secret"}), \
                patch("xpra.clipboard.catlink_bridge.socket.create_connection") as connect:
            bridge = CatlinkClipboardBridge(Mock(), enabled=False)
            self.assertIsNone(bridge.sock)
            connect.assert_not_called()
            self.assertFalse(bridge.handle("token", SimpleNamespace(_selection="CLIPBOARD"),
                                           "CLIPBOARD", ("text/uri-list",), {}, 1, "peer"))
            self.assertEqual(bridge.generation, 0)

    def test_windows_keeps_production_backend(self):
        import runpy
        import xpra
        module = runpy.run_path(str(Path(xpra.__file__).parent / "platform/win32/clipboard.py"))
        self.assertEqual(module["get_backend_module"](), "xpra.gtk.clipboard.GTK_Clipboard")


if __name__ == "__main__":
    unittest.main()
