"""Notices — a plugin's own account of what is wrong with it.

``PluginRecord.status`` in homeCore answers "is the process alive": *active*,
*offline*, *stopped*. It cannot answer "alive, but structurally unable to do
its job", and that is the state operators actually get stuck in. A plugin whose
receiver is bound to loopback starts cleanly, heartbeats, reports *active*, and
silently drops every message it was meant to receive. On the dashboard it reads
as healthy.

A notice carries the diagnosis to the UI, where it appears on the plugin's card
next to its status rather than only in a log stream nobody is reading.

Notices are **current state, not an event log.** The plugin publishes the full
set it currently believes on every heartbeat, and homeCore replaces what it
held. A condition that clears simply stops being sent and disappears on the
next beat — there is nothing to acknowledge and nothing to expire.

That has one consequence worth internalising: a notice must be cheap to
re-derive. Compute it from current config and state, do not accumulate it. The
classic bug is raising ``no_devices_configured`` once at startup and never
looking again, so it is still on screen after the operator has added devices.

.. code-block:: python

    from homecore_plugin_sdk import PluginNotice

    if not self.reachable:
        self.notices.raise_(
            PluginNotice.error(
                "bridge_unreachable",
                "The bridge stopped answering, so no device state is updating.",
                remedy="Check that the bridge is powered on and on this network.",
            )
        )
    else:
        self.notices.clear("bridge_unreachable")
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum


class NoticeLevel(str, Enum):
    """How much the operator should care.

    A ``str`` enum so it serialises to the ``snake_case`` string homeCore
    expects without a custom encoder.
    """

    #: Worth knowing, nothing is wrong — a deliberate non-default mode, say.
    INFO = "info"
    #: The plugin runs, but something it needs is missing or misconfigured and
    #: some or all of its function is unavailable. The common case.
    WARNING = "warning"
    #: The plugin cannot do its job at all and operator action is required.
    ERROR = "error"


@dataclass(frozen=True)
class PluginNotice:
    """One condition a plugin is reporting about itself."""

    level: NoticeLevel
    code: str
    message: str
    remedy: str | None = None

    @classmethod
    def info(cls, code: str, message: str, *, remedy: str | None = None) -> PluginNotice:
        return cls(NoticeLevel.INFO, code, message, remedy)

    @classmethod
    def warning(cls, code: str, message: str, *, remedy: str | None = None) -> PluginNotice:
        return cls(NoticeLevel.WARNING, code, message, remedy)

    @classmethod
    def error(cls, code: str, message: str, *, remedy: str | None = None) -> PluginNotice:
        return cls(NoticeLevel.ERROR, code, message, remedy)

    def to_dict(self) -> dict:
        """The wire form. ``remedy`` is omitted when unset, matching Rust's
        ``skip_serializing_if``."""
        out: dict = {
            "level": self.level.value,
            "code": self.code,
            "message": self.message,
        }
        if self.remedy is not None:
            out["remedy"] = self.remedy
        return out


class PluginNotices:
    """The set of notices a plugin is currently reporting.

    Thread-safe: plugins typically raise from a polling thread while the
    heartbeat thread reads. Obtain one as ``self.notices`` on
    :class:`~homecore_plugin_sdk.PluginBase`.
    """

    def __init__(self, on_change=None) -> None:
        self._lock = threading.Lock()
        self._notices: dict[str, PluginNotice] = {}
        # Called when the set actually changes, so the plugin can push a
        # heartbeat right away instead of leaving the operator staring at a
        # stale card until the next beat — which can be a minute.
        self._on_change = on_change

    def _notify(self, changed: bool) -> None:
        """Fire the change callback.

        **Must be called with the lock released.** The callback publishes a
        heartbeat, which reads the set back through :meth:`snapshot` and so
        re-acquires this lock — and ``threading.Lock`` is not reentrant, so
        calling it while held deadlocks the caller permanently.
        """
        if changed and self._on_change is not None:
            self._on_change()

    def raise_(self, notice: PluginNotice) -> None:
        """Add or replace the notice with this ``code``.

        Named ``raise_`` because ``raise`` is a Python keyword. Re-raising an
        existing code overwrites it, so re-deriving conditions on a poll loop is
        the intended usage rather than something to guard against.
        """
        with self._lock:
            changed = self._notices.get(notice.code) != notice
            self._notices[notice.code] = notice
        self._notify(changed)

    def clear(self, code: str) -> None:
        """Drop the notice with this ``code``. A no-op if it is not raised, so
        callers never need to check first."""
        with self._lock:
            changed = self._notices.pop(code, None) is not None
        self._notify(changed)

    def set(self, notices: list[PluginNotice]) -> None:
        """Replace the whole set at once.

        The right call when a sync cycle re-derives every condition together —
        it cannot leave a stale notice behind the way individual raise/clear
        pairs can.
        """
        with self._lock:
            replacement = {n.code: n for n in notices}
            # Re-deriving the same conditions is the intended usage, so an
            # unchanged set must not cost a publish.
            changed = replacement != self._notices
            self._notices = replacement
        self._notify(changed)

    def clear_all(self) -> None:
        with self._lock:
            changed = bool(self._notices)
            self._notices.clear()
        self._notify(changed)

    def snapshot(self) -> list[PluginNotice]:
        """What the next heartbeat will carry."""
        with self._lock:
            return list(self._notices.values())

    def to_wire(self) -> list[dict]:
        return [n.to_dict() for n in self.snapshot()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._notices)

    def __contains__(self, code: object) -> bool:
        with self._lock:
            return code in self._notices
