# This file is part of Xpra, released under the GNU GPL v2 or later.

from ctypes import Structure, POINTER, byref, sizeof
from ctypes.wintypes import UINT, HWND, DWORD, BOOL
from xpra.platform.win32.common import user32
from xpra.platform.win32.gui import get_window_handle


class FLASHWINFO(Structure):
    _fields_ = [("cbSize", UINT), ("hwnd", HWND), ("dwFlags", DWORD),
                ("uCount", UINT), ("dwTimeout", DWORD)]


FlashWindowEx = user32.FlashWindowEx
FlashWindowEx.argtypes = [POINTER(FLASHWINFO)]
FlashWindowEx.restype = BOOL
FLASHW_STOP = 0
FLASHW_ALL = 3
FLASHW_TIMERNOFG = 12


def set_window_attention(window, pending: bool) -> None:
    hwnd = get_window_handle(window)
    if hwnd:
        # Same flags as upstream's native Win32 window alert implementation.
        flags = (FLASHW_ALL | FLASHW_TIMERNOFG) if pending else FLASHW_STOP
        info = FLASHWINFO(sizeof(FLASHWINFO), hwnd, flags, 0, 0)
        FlashWindowEx(byref(info))
