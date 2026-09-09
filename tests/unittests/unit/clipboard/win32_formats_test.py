#!/usr/bin/env python3

import struct
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from xpra.clipboard.win32_formats import encode_hdrop, encode_unicode_text, uri_list_to_windows_paths, encode_shell_id_list
from xpra.clipboard.win32_formats import WindowsFileClipboard, encode_filename_formats


class Win32FormatsTest(unittest.TestCase):
    def test_filename_aliases_use_native_encodings(self):
        path = "C:\\Temp\\10.2《独立性》.pptx"
        aliases = dict(encode_filename_formats((path,), "cp936"))
        self.assertEqual(aliases["FileNameW"], (path + "\0").encode("utf-16le"))
        self.assertEqual(aliases["FileName"], path.encode("cp936") + b"\0")
        aliases = dict(encode_filename_formats((path,), "cp1252"))
        self.assertEqual(tuple(aliases), ("FileNameW",))

    def test_multiple_files_do_not_publish_single_file_aliases(self):
        paths = (r"C:\Temp\a.pptx", r"C:\Temp\b.pptx")
        self.assertEqual(encode_filename_formats(paths, "cp1252"), ())
        self.assertEqual(encode_filename_formats((), "cp1252"), ())
        self.assertEqual(encode_hdrop(paths)[20:].decode("utf-16le"), "\0".join(paths) + "\0\0")

    def test_append_registers_filename_aliases_without_replacing_hdrop(self):
        native = WindowsFileClipboard.__new__(WindowsFileClipboard)
        calls = []
        ids = {"FileNameW": 49152, "FileName": 49153}
        native.user = SimpleNamespace(
            GetClipboardOwner=lambda: 42, OpenClipboard=lambda _: 1,
            CloseClipboard=lambda: calls.append("close"),
            RegisterClipboardFormatW=lambda name: ids[name])
        native._set_data = lambda fmt, data: calls.append((fmt, data))
        path = r"C:\Temp\a.pptx"
        hdrop = encode_hdrop((path,))
        aliases = encode_filename_formats((path,), "cp1252")
        self.assertTrue(native.append(42, hdrop, lambda: True, aliases))
        self.assertEqual(calls, [(15, hdrop), (49152, aliases[0][1]), (49153, aliases[1][1]), "close"])

    def test_hdrop_append_preserves_owner_and_other_formats(self):
        native = WindowsFileClipboard.__new__(WindowsFileClipboard)
        buffer = ctypes.create_string_buffer(128)
        calls = []
        native.user = SimpleNamespace(
            GetClipboardOwner=lambda: 42, OpenClipboard=lambda owner: 1,
            CloseClipboard=lambda: calls.append("close"),
            SetClipboardData=lambda fmt, handle: calls.append((fmt, handle)) or handle)
        native.kernel = SimpleNamespace(
            GlobalAlloc=lambda *_: 123, GlobalLock=lambda _: ctypes.addressof(buffer),
            GlobalUnlock=lambda _: None, GlobalFree=lambda _: self.fail("Windows owns successful allocation"))
        data = encode_hdrop([r"C:\Temp\a.pptx"])
        self.assertTrue(native.append(42, data, lambda: True))
        self.assertEqual(buffer.raw[:len(data)], data)
        self.assertEqual(calls, [(15, 123), "close"])
        # No EmptyClipboard API is provided: appending must not erase GTK formats.

    def test_hdrop_retry_and_native_takeover(self):
        native = WindowsFileClipboard.__new__(WindowsFileClipboard)
        owners = iter((42, 99))  # takeover between precheck and acquiring lock
        closed = []
        native.user = SimpleNamespace(GetClipboardOwner=lambda: next(owners),
                                      OpenClipboard=lambda _: 1, CloseClipboard=lambda: closed.append(True))
        native.kernel = SimpleNamespace()
        self.assertTrue(native.append(42, b"data", lambda: True))
        self.assertEqual(closed, [True])
        native.user.GetClipboardOwner = lambda: 42
        native.user.OpenClipboard = lambda _: 0
        self.assertFalse(native.append(42, b"data", lambda: True))
        self.assertTrue(native.append(42, b"data", lambda: False))

    def test_shell_id_list(self):
        pidls = [b"\x04\x00AB\0\0", b"\x04\x00CD\0\0"]
        with patch("xpra.clipboard.win32_formats._windows_path_pidl", side_effect=pidls) as parse:
            data = encode_shell_id_list("file:///C:/Temp/中文.txt\nfile://server/share/a.txt")
        self.assertEqual([c.args[0] for c in parse.call_args_list],
                         ["C:\\Temp\\中文.txt", r"\\server\share\a.txt"])
        self.assertEqual(struct.unpack("<IIII", data[:16]), (2, 16, 18, 24))
        self.assertEqual(data[16:], b"\0\0" + b"".join(pidls))
        self.assertEqual(encode_shell_id_list(b""), b"")

    def test_uri_list(self):
        data = b"# copied files\r\nfile:///C:/Users/test/My%20File.txt\r\nfile://server/share/report.pdf\r\n"
        self.assertEqual(
            uri_list_to_windows_paths(data),
            (r"C:\Users\test\My File.txt", r"\\server\share\report.pdf"),
        )

    def test_hdrop(self):
        paths = (r"C:\Temp\one.txt", r"D:\two.png")
        data = encode_hdrop(paths)
        self.assertEqual(struct.unpack("<IiiII", data[:20]), (20, 0, 0, 0, 1))
        self.assertEqual(data[20:].decode("utf-16le"), "\0".join(paths) + "\0\0")

    def test_unicode_text(self):
        self.assertEqual(encode_unicode_text("one\ntwo").decode("utf-16le"), "one\r\ntwo\0")
        self.assertEqual(encode_unicode_text("one\r\ntwo").decode("utf-16le"), "one\r\ntwo\0")


def main():
    unittest.main()


if __name__ == "__main__":
    main()
