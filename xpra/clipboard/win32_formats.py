import re
import struct
import ctypes
import os
from urllib.parse import unquote, urlsplit


_DRIVE_PATH = re.compile(r"^/[A-Za-z]:/")


class WindowsFileClipboard:
    """Append Windows file formats without replacing GTK ownership."""
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = (
            (self.user, "GetClipboardOwner", (), ctypes.c_void_p),
            (self.user, "GetWindowThreadProcessId", (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)), ctypes.c_uint32),
            (self.user, "OpenClipboard", (ctypes.c_void_p,), ctypes.c_int),
            (self.user, "CloseClipboard", (), ctypes.c_int),
            (self.user, "SetClipboardData", (ctypes.c_uint, ctypes.c_void_p), ctypes.c_void_p),
            (self.user, "RegisterClipboardFormatW", (ctypes.c_wchar_p,), ctypes.c_uint),
            (self.kernel, "GlobalAlloc", (ctypes.c_uint, ctypes.c_size_t), ctypes.c_void_p),
            (self.kernel, "GlobalLock", (ctypes.c_void_p,), ctypes.c_void_p),
            (self.kernel, "GlobalUnlock", (ctypes.c_void_p,), ctypes.c_int),
            (self.kernel, "GlobalFree", (ctypes.c_void_p,), ctypes.c_void_p),
        )
        for library, name, args, result in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    def owned_window(self):
        owner = self.user.GetClipboardOwner()
        pid = ctypes.c_uint32()
        if owner:
            self.user.GetWindowThreadProcessId(owner, ctypes.byref(pid))
        return owner if pid.value == os.getpid() else None

    def append(self, owner, data, current, extra_formats=()):
        # Check again under the native lock: GTK's queued clear callback may
        # not yet have invalidated the generation after another app copied.
        if not current() or not owner or self.user.GetClipboardOwner() != owner:
            return True  # retired, not a lock-contention retry
        if not self.user.OpenClipboard(owner):
            return False
        try:
            if not current() or self.user.GetClipboardOwner() != owner:
                return True
            self._set_data(15, data)  # actual CF_HDROP, not a registered name
            for name, payload in extra_formats:
                fmt = self.user.RegisterClipboardFormatW(name)
                if not fmt:
                    raise ctypes.WinError(ctypes.get_last_error())
                self._set_data(fmt, payload)
            return True
        finally:
            self.user.CloseClipboard()

    def _set_data(self, fmt, data):
        handle = None
        try:
            handle = self.kernel.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                ctypes.memmove(pointer, data, len(data))
            finally:
                self.kernel.GlobalUnlock(handle)
            if not self.user.SetClipboardData(fmt, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            handle = None  # ownership transferred to Windows
        finally:
            if handle:
                self.kernel.GlobalFree(handle)


def encode_filename_formats(paths, ansi_encoding="mbcs"):
    # These legacy aliases describe ONE complete path, not a DROPFILES header
    # or a list. Do not silently reduce a multi-file offer to its first file.
    if len(paths) != 1:
        return ()
    path = paths[0]
    formats = [("FileNameW", (path + "\0").encode("utf-16le"))]
    try:
        ansi = path.encode(ansi_encoding, "strict")
        # Some Windows code pages allow best-fit substitutions even in strict
        # mode. A substituted filename may name another file: never publish it.
        if ansi.decode(ansi_encoding, "strict") == path:
            formats.append(("FileName", ansi + b"\0"))
    except UnicodeError:
        pass
    return tuple(formats)


def _windows_path_pidl(path):
    shell = ctypes.WinDLL("shell32")
    ole = ctypes.WinDLL("ole32")
    shell.SHParseDisplayName.argtypes = (ctypes.c_wchar_p, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_void_p)
    shell.SHParseDisplayName.restype = ctypes.c_long
    shell.ILGetSize.argtypes = (ctypes.c_void_p,)
    shell.ILGetSize.restype = ctypes.c_uint
    ole.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    ole.CoTaskMemFree.restype = None
    pidl = ctypes.c_void_p()
    result = shell.SHParseDisplayName(path, None, ctypes.byref(pidl), 0, None)
    if result < 0 or not pidl.value:
        raise OSError(f"cannot resolve clipboard file in Windows Shell: HRESULT={result:#x}")
    try:
        return ctypes.string_at(pidl, shell.ILGetSize(pidl))
    finally:
        ole.CoTaskMemFree(pidl)


def encode_shell_id_list(uris):
    """CIDA: desktop parent plus absolute PIDLs of completed local files.

    GTK registers named targets verbatim. Shell IDList Array is a registered
    Shell format, unlike CF_HDROP (a predefined numeric format), so it works
    without replacing GTK's clipboard owner or its existing sending backend.
    """
    paths = uri_list_to_windows_paths(uris)
    if not paths:
        return b""
    pidls = [b"\0\0"] + [_windows_path_pidl(path) for path in paths]
    offset = 4 * (len(paths) + 2)
    offsets = []
    for pidl in pidls:
        offsets.append(offset)
        offset += len(pidl)
    return struct.pack("<" + "I" * (len(paths) + 2), len(paths), *offsets) + b"".join(pidls)


def file_uri_to_windows_path(uri: str) -> str:
    parsed = urlsplit(uri.strip())
    if parsed.scheme.lower() != "file":
        return ""
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        return "\\\\" + parsed.netloc + path.replace("/", "\\")
    if _DRIVE_PATH.match(path):
        path = path[1:]
    return path.replace("/", "\\")


def uri_list_to_windows_paths(data) -> tuple[str, ...]:
    text = data.decode("utf8", "replace") if isinstance(data, bytes) else str(data or "")
    paths = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path = file_uri_to_windows_path(line)
        if path:
            paths.append(path)
    return tuple(paths)


def encode_hdrop(paths) -> bytes:
    values = tuple(str(path) for path in paths if path)
    if not values:
        return b""
    # DROPFILES followed by a double-NUL-terminated UTF-16 path list.
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    return header + ("\0".join(values) + "\0\0").encode("utf-16le")


def encode_unicode_text(text: str) -> bytes:
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    return (normalized + "\0").encode("utf-16le")
