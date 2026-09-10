# This file is part of Xpra, released under the GNU GPL v2 or later.

from AppKit import NSApp
from xpra.os_util import gi_import
from xpra.util.env import envbool
from xpra.log import Logger

GLib = gi_import("GLib")
log = Logger("window", "metadata")
_windows: set = set()
_timer = 0

# Dock badges accept arbitrary short text (supported on macOS 13).
# An ellipsis means outstanding attention, not an unread-message count.
# Keep these alternatives for applications that may prefer bouncing later:
# from AppKit import NSInformationalRequest, NSCriticalRequest
# request_id = NSApp.requestUserAttention_(NSInformationalRequest)  # bounce for ~1 second
# request_id = NSApp.requestUserAttention_(NSCriticalRequest)  # bounce until activation/cancel
# NSApp.cancelUserAttentionRequest_(request_id)
# Both bounce modes have no effect while NSApp.isActive() is True, and activation
# cancels them. A badge works even when the app remains active with windows minimized.
# Do not acknowledge a window merely because the application becomes active.


def _check_focus() -> bool:
    for window in tuple(_windows):
        if window.attention_ack_on_focus and window._attention_is_focused():
            window.acknowledge_attention()
    return bool(_windows)


def set_window_attention(window, pending: bool) -> None:
    global _timer
    if pending:
        if envbool("CATLINK_HIDE_DOCK", False):
            return
        # Set the badge before registering the window so a failed native call
        # can be retried without retaining a partially applied state.
        NSApp.dockTile().setBadgeLabel_("…")
        _windows.add(window)
        if window.attention_ack_on_focus and not _timer:
            _timer = GLib.timeout_add(250, _check_focus)
    else:
        if not (_windows - {window}):
            NSApp.dockTile().setBadgeLabel_("")
        _windows.discard(window)
        if not _windows and _timer:
            GLib.source_remove(_timer)
            _timer = 0
    log("dock attention badge=%r, windows=%s", "…" if _windows else "", [w.wid for w in _windows])
