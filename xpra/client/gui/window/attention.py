# This file is part of Xpra, released under the GNU GPL v2 or later.

from dataclasses import dataclass


@dataclass
class AttentionState:
    requested: bool = False
    acknowledged: bool = False

    def update(self, requested: bool, focused: bool = False) -> None:
        if requested != self.requested:
            self.requested = requested
            self.acknowledged = False
        if focused and requested:
            self.acknowledged = True

    def acknowledge(self) -> None:
        self.acknowledged = self.requested

    @property
    def pending(self) -> bool:
        return self.requested and not self.acknowledged
