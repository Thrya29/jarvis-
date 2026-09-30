"""Per-agent desktop state: consent, protected windows, and the controllers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from jarvis.core.config import DesktopConfig, ScreenControl
from jarvis.desktop.computer import ComputerController
from jarvis.desktop.uia import UIAService
from jarvis.safety.policy import ApprovalRequest, Approver, Risk

log = logging.getLogger(__name__)

CONSENT_SUMMARY = (
    "Let JARVIS see your screen and control the mouse and keyboard for this task? "
    "Screenshots are sent to the AI model. Press {hotkey} at any time to stop."
)


@dataclass
class DesktopSession:
    cfg: DesktopConfig
    uia: UIAService
    computer: ComputerController | None
    kill_hotkey: str = "ctrl+alt+j"
    _granted: set[str] = field(default_factory=set)
    _denied: set[str] = field(default_factory=set)

    async def consent(self, task_id: str, approver: Approver) -> str | None:
        """None if screen control is allowed for this task, else the reason it isn't."""
        mode = self.cfg.screen_control
        if mode is ScreenControl.DENY:
            return "Screen control is disabled in settings (desktop.screen_control = deny)."
        if mode is ScreenControl.ALLOW or task_id in self._granted:
            return None
        if task_id in self._denied:
            return "The user declined screen control for this task."
        decision = await approver.request(
            ApprovalRequest(
                "screen_control",
                CONSENT_SUMMARY.format(hotkey=self.kill_hotkey.upper()),
                Risk.EXECUTE,
            )
        )
        if decision.approved:
            self._granted.add(task_id)
            return None
        self._denied.add(task_id)
        return "The user declined screen control for this task." + (
            f" Their note: {decision.note}" if decision.note else ""
        )

    async def protected_foreground(self) -> str | None:
        title = await self.uia.foreground_title()
        return title if self.uia.is_blocked(title) else None

    def end_task(self, task_id: str) -> None:
        self._granted.discard(task_id)
        self._denied.discard(task_id)
        if self.computer is not None:
            try:
                self.computer.release_all()
            except OSError:
                log.warning("could not release held keys/buttons", exc_info=True)

    def close(self) -> None:
        self.uia.close()
