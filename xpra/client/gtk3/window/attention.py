# This file is part of Xpra, released under the GNU GPL v2 or later.

from xpra.client.gui.window.attention import AttentionState
from xpra.client.gtk3.window.stub_window import GtkStubWindow
from xpra.os_util import OSX, WIN32
from xpra.log import Logger
from xpra.util.env import envbool

log = Logger("window", "metadata")


class AttentionWindow(GtkStubWindow):
    def init_window(self, client, metadata, client_props) -> None:
        # Keep acknowledgements across local destroy/recreate, but not reconnects.
        if not hasattr(client, "_attention_states"):
            client._attention_states = {}
        self.attention_state = client._attention_states.setdefault(self.wid, AttentionState())
        # Default to remote state; opt in to acknowledging on local focus.
        self.attention_ack_on_focus = envbool("XPRA_ATTENTION_ACK_ON_FOCUS", False)
        self._attention_closed = False
        self._attention_applied = False
        self._attention_has_mapped = False
        self.connect("notify::has-toplevel-focus", self._attention_focus_changed)
        self.connect("focus-in-event", self._attention_focus_in)
        self.connect("map-event", self._attention_mapped)

    def _attention_mapped(self, *_args) -> bool:
        self._attention_has_mapped = True
        self._sync_attention()
        return False

    def _attention_is_focused(self) -> bool:
        # Quartz can report is_active() after the window has lost focus.
        return not getattr(self, "_iconified", False) and self.has_toplevel_focus()

    def set_attention_requested(self, requested: bool) -> None:
        self.attention_state.update(requested, self.attention_ack_on_focus and self._attention_is_focused())
        self.when_realized("attention", self._sync_attention)

    def _attention_focus_changed(self, *_args) -> None:
        if self.attention_ack_on_focus and self._attention_is_focused():
            self.acknowledge_attention()

    def _attention_focus_in(self, *_args) -> bool:
        self._attention_focus_changed()
        return False

    def acknowledge_attention(self) -> None:
        self.attention_state.acknowledge()
        self._sync_attention()

    def _sync_attention(self) -> None:
        if self._attention_closed or not self.get_realized():
            return
        if not self._attention_has_mapped and not self.get_mapped():
            return
        if self.attention_ack_on_focus and self._attention_is_focused():
            self.attention_state.acknowledge()
        pending = self.attention_state.pending and not self.is_OR() and not self.is_tray()
        if pending != self._attention_applied:
            self._apply_attention(pending)

    def _apply_attention(self, pending: bool) -> None:
        try:
            if OSX:
                from xpra.platform.darwin.attention import set_window_attention
                set_window_attention(self, pending)
            elif WIN32:
                from xpra.platform.win32.attention import set_window_attention
                set_window_attention(self, pending)
            else:
                self.set_urgency_hint(pending)
            self._attention_applied = pending and self.attention_state.pending
            log("window attention wid=%#x requested=%s acknowledged=%s applied=%s",
                self.wid, self.attention_state.requested, self.attention_state.acknowledged, self._attention_applied)
        except Exception:
            log.error("cannot apply window attention for %#x", self.wid, exc_info=True)

    def cleanup(self) -> None:
        self._attention_closed = True
        if self._attention_applied:
            self._apply_attention(False)
        if not getattr(self, "catlink_reinit_destroy", False):
            self._client._attention_states.pop(self.wid, None)

    def get_info(self) -> dict:
        return {"attention": {
            "ack-on-focus": self.attention_ack_on_focus,
            "requested": self.attention_state.requested,
            "acknowledged": self.attention_state.acknowledged,
            "applied": self._attention_applied,
        }}
