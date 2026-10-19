"""Alerts — what a plugin says needs attention about its devices.

Notices are about the plugin; **alerts** are about its devices, and live in the
house's alert list (homeCore 0.1.85). A plugin declares the kinds it can raise
(:class:`PluginAlertKind`) on its capability manifest, and publishes, per
device, the ones that are true now (:class:`PluginAlert`). homeCore decides
whether each is shown and where it is sent.

Alerts are **current state, not events**: publish the whole list for a device
each time, and homeCore replaces what it held — an alert you stop sending has
cleared, and an empty list clears them all.

.. code-block:: python

    from homecore_plugin_sdk import (
        AlertAction, AlertSeverity, Capabilities, PluginAlert, PluginAlertKind,
    )

    self.enable_management(capabilities=Capabilities(alert_kinds=[
        PluginAlertKind(
            "firmware_update",
            "Firmware update available",
            severity=AlertSeverity.INFO,
            default_on=False,  # news, not a problem
        ),
    ]))

    self.set_alerts("wled_1", [
        PluginAlert(
            "firmware_update",
            "Firmware 17.0.0 is out",
            key="17.0.0",
            action=AlertAction(
                "Update…", "install_firmware",
                params={"version": "17.0.0"}, confirm=True,
            ),
        ),
    ])
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AlertSeverity(str, Enum):
    """How serious an alert is.

    ``CRITICAL`` exists because it is on the wire, but it is homeCore's own
    (smoke, water): a plugin asking for it gets ``error``.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertActionTarget(str, Enum):
    """Where an :class:`AlertAction` runs."""

    #: On the alert's device, as one of its device actions. The default.
    DEVICE = "device"
    #: As one of the plugin's own manifest actions.
    PLUGIN = "plugin"


@dataclass(frozen=True)
class AlertAction:
    """The button on a plugin's alert: one of the device's own actions, or one
    of the plugin's, run with these params when someone (or an automation)
    presses it.

    :param label: What the button says: ``"Update…"``.
    :param id: The action to run: a device action id (``install_firmware``)
        when the alert is about a device, else one of the plugin's manifest
        actions.
    :param target: :attr:`AlertActionTarget.DEVICE` (the default) runs it on
        the alert's device; :attr:`AlertActionTarget.PLUGIN` runs it as a
        plugin action.
    :param params: Passed to the action.
    :param confirm: Ask before running it: it interrupts something.
    """

    label: str
    id: str
    target: AlertActionTarget = AlertActionTarget.DEVICE
    params: Any = None
    confirm: bool = False

    def to_dict(self) -> dict:
        """The wire form. ``target`` is omitted when it is ``device``,
        ``params`` when unset and ``confirm`` when false, matching Rust's
        ``skip_serializing_if``."""
        out: dict = {"label": self.label, "id": self.id}
        target = AlertActionTarget(self.target)
        if target is not AlertActionTarget.DEVICE:
            out["target"] = target.value
        if self.params is not None:
            out["params"] = self.params
        if self.confirm:
            out["confirm"] = True
        return out


@dataclass(frozen=True)
class PluginAlertKind:
    """A kind of alert a plugin can raise, declared on its capability manifest
    — which is how the settings screen can list it, and let someone turn it on
    or off, before it has ever happened.

    :param topic: ``snake_case``, stable: what :attr:`PluginAlert.topic` names.
    :param label: For a person: ``"Firmware update available"``.
    :param description: Optional longer explanation.
    :param severity: How serious one is. Never ``critical``: that is homeCore's
        own; a plugin asking for it gets ``error``.
    :param default_on: Shown unless someone turns it off (``True``), or only
        once someone turns it on (``False``) — right for news that isn't a
        problem.
    """

    topic: str
    label: str
    description: str | None = None
    severity: AlertSeverity = AlertSeverity.WARNING
    default_on: bool = True

    def to_dict(self) -> dict:
        out: dict = {"topic": self.topic, "label": self.label}
        if self.description is not None:
            out["description"] = self.description
        out["severity"] = AlertSeverity(self.severity).value
        out["default_on"] = self.default_on
        return out


@dataclass(frozen=True)
class PluginAlert:
    """One alert a plugin says is true now, for one device (or for itself).

    :param topic: One of the plugin's declared kinds.
    :param reason: Without the device's name, for its card: ``"Firmware
        17.0.0 is out"``.
    :param key: What this one is about within its kind (``17.0.0``); a
        different key is a different alert, so "skip this one" skips this
        version, not the kind.
    :param message: With the device's name, when the plugin wants other words
        than ``"{name}: {reason}"``.
    :param severity: Overrides the kind's declared severity for this one;
        capped at ``error``.
    :param action: The button on it. See :class:`AlertAction`.
    :param since: When the condition began, if the plugin knows; else homeCore
        uses when it first heard of it. A ``datetime`` (naive means UTC) or an
        RFC 3339 string.
    """

    topic: str
    reason: str
    key: str | None = None
    message: str | None = None
    severity: AlertSeverity | None = None
    action: AlertAction | None = None
    since: datetime | str | None = None

    def to_dict(self) -> dict:
        """The wire form. Unset optional fields are omitted, not sent as null."""
        out: dict = {"topic": self.topic}
        if self.key is not None:
            out["key"] = self.key
        out["reason"] = self.reason
        if self.message is not None:
            out["message"] = self.message
        if self.severity is not None:
            out["severity"] = AlertSeverity(self.severity).value
        if self.action is not None:
            out["action"] = self.action.to_dict()
        if self.since is not None:
            out["since"] = _rfc3339(self.since)
        return out


def _rfc3339(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
