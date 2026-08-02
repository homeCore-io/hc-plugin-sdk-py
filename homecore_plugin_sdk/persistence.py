"""Device persistence — remembering what this plugin registered last time.

When a device disappears from a plugin's authoritative source — a Hue bulb
deleted from the bridge, a Z-Wave node excluded, an entry removed from config —
its homeCore record has to go too. Otherwise it lingers forever, still shown in
the UI and still accepting commands nothing will execute.

Working out what disappeared means knowing what existed *before*, and a plugin
that has just restarted knows nothing. So the SDK mirrors every
register/unregister to a small JSON file, loads it at startup, and gives you
:meth:`~homecore_plugin_sdk.PluginBase.reconcile_devices` to diff the live set
against it.

.. code-block:: python

    def on_connect(self):
        # Once, before registering anything.
        self.enable_device_persistence(config_dir / ".published-device-ids.json")

    def after_a_successful_sync(self, live_ids):
        report = self.reconcile_devices(live_ids)
        # report.stale_unregistered — gone from the bridge, now gone from homeCore
        # report.unknown_in_live    — ids you passed but never registered

**Only reconcile after a sync you trust.** Calling it on a partial fetch will
unregister live devices behind a temporarily unreachable upstream — which looks
exactly like the bug it exists to prevent, but worse, because the devices were
fine. Track an "everything succeeded" flag across your per-source loop and pass
the live set only when it holds.

Plugins whose upstream reports irregularly — battery sensors that go quiet for
hours — should enable persistence but skip auto-reconcile. The false-positive
risk is worse than a zombie device, and an operator can clear those with
``DELETE /api/v1/plugins/{id}/devices``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    """Outcome of a reconcile."""

    #: Registered before this reconcile but absent from the live set, so
    #: unregistered.
    stale_unregistered: list[str] = field(default_factory=list)
    #: In the live set but never registered. Usually empty; non-empty means the
    #: caller passed ids it never registered with the SDK. Reported for
    #: diagnosis, no action taken.
    unknown_in_live: list[str] = field(default_factory=list)


def scoped_snapshot_path(path: str, plugin_id: str) -> str:
    """Insert ``plugin_id`` into a snapshot filename.

    ``.published-device-ids.json`` → ``.published-device-ids.plugin.hue.json``

    Real deployments keep every plugin's config in one directory, and every
    plugin derives this path the same way — so without scoping they share one
    file and unregister each other's devices.

    Idempotent: a path already carrying this plugin's id comes back unchanged,
    so repeated calls cannot keep extending the name.

    Plugin ids contain dots (``plugin.hue``) and so does the base filename, so
    this works on the whole filename rather than splitting on the extension.
    """
    head, name = os.path.split(path)
    if name.endswith(".json"):
        base = name[: -len(".json")]
        scoped = name if base.endswith(plugin_id) else f"{base}.{plugin_id}.json"
    else:
        scoped = name if name.endswith(plugin_id) else f"{name}.{plugin_id}"
    return os.path.join(head, scoped)


class DeviceTracker:
    """The set of devices this plugin has registered, optionally on disk."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._path: str | None = None

    def enable_persistence(self, path: str) -> None:
        """Load any previous snapshot, then mirror every change to *path*.

        A failure to load is logged loudly and never raised. It is not fatal —
        the plugin still works — but it does silently cost the ability to retire
        devices from earlier runs, so it must not pass unnoticed.
        """
        try:
            with open(path, "r") as f:
                ids = json.load(f)
            if isinstance(ids, list):
                with self._lock:
                    self._ids.update(str(i) for i in ids)
                logger.debug("loaded %d ids from device snapshot %s", len(ids), path)
            else:
                logger.warning(
                    "device snapshot %s is not a list — devices registered in "
                    "earlier runs cannot be reconciled and will linger in homeCore",
                    path,
                )
        except FileNotFoundError:
            logger.debug("no device snapshot at %s yet — first run", path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "cannot read device snapshot %s (%s) — devices registered in "
                "earlier runs cannot be reconciled and will linger in homeCore",
                path,
                exc,
            )
        self._path = path

    def add(self, device_id: str) -> None:
        with self._lock:
            changed = device_id not in self._ids
            self._ids.add(device_id)
        if changed:
            self._save()

    def discard(self, device_id: str) -> None:
        with self._lock:
            changed = device_id in self._ids
            self._ids.discard(device_id)
        if changed:
            self._save()

    def snapshot(self) -> set[str]:
        with self._lock:
            return set(self._ids)

    def __contains__(self, device_id: object) -> bool:
        with self._lock:
            return device_id in self._ids

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self._lock:
                ordered = sorted(self._ids)
            # Write via a temp file in the same directory and rename, so a crash
            # mid-write cannot leave a truncated snapshot that reads as "this
            # plugin registered nothing" and retires every device on next
            # reconcile.
            tmp = f"{self._path}.tmp"
            with open(tmp, "w") as f:
                json.dump(ordered, f, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("device snapshot write failed (%s): %s", self._path, exc)
