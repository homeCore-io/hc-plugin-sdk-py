"""homecore_plugin_sdk — Python SDK for HomeCore device plugins.

Provides :class:`PluginBase`, a base class that handles broker connection,
device registration, and state publishing.  Subclass it to implement a plugin:

.. code-block:: python

    from homecore_plugin_sdk import PluginBase

    class MyLightPlugin(PluginBase):
        PLUGIN_ID = "plugin.my_light"

        def on_connect(self):
            caps = {"on": {"type": "boolean"}, "brightness": {"type": "integer", "minimum": 0, "maximum": 255}}
            self.register_device("light.01", "My Light", caps)
            self.publish_availability("light.01", True)
            self.publish_plugin_status("active")

        def on_command(self, device_id: str, payload: dict) -> None:
            print(f"Command for {device_id}: {payload}")
            self.publish_state(device_id, {"on": payload.get("on", False)})

    if __name__ == "__main__":
        MyLightPlugin().run()

Configuration
-------------
Constructor parameters override environment variables, which override defaults:

+---------------------+----------------------+------------+
| Parameter           | Env var              | Default    |
+=====================+======================+============+
| ``broker_host``     | ``HC_BROKER_HOST``   | 127.0.0.1  |
+---------------------+----------------------+------------+
| ``broker_port``     | ``HC_BROKER_PORT``   | 1883       |
+---------------------+----------------------+------------+
| ``password``        | ``HC_PLUGIN_PASSWORD``| (empty)   |
+---------------------+----------------------+------------+
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from .capabilities import Action, Capabilities, Concurrency, ItemOp, RequiresRole
from .notices import NoticeLevel, PluginNotice, PluginNotices
from .persistence import DeviceTracker, ReconcileReport, scoped_snapshot_path
from .streaming import StreamContext, StreamTerminated

__all__ = [
    "PluginBase",
    "MqttLogHandler",
    # Notices
    "PluginNotice",
    "PluginNotices",
    "NoticeLevel",
    # Capability actions
    "Capabilities",
    "Action",
    "Concurrency",
    "ItemOp",
    "RequiresRole",
    # Streaming actions
    "StreamContext",
    "StreamTerminated",
    # Device persistence
    "ReconcileReport",
]

#: This SDK's version, reported in every heartbeat. Informational — it tells an
#: operator which SDK to rebuild against; it is not what core checks
#: compatibility on.
SDK_VERSION = "0.3.0"

#: The wire protocol this SDK speaks, which is core's `hc-types` version. Core
#: compares it against its own to decide whether the two agree on the shape of
#: a device, an event, and a command.
PROTOCOL_VERSION = "0.1.5"

logger = logging.getLogger(__name__)


class PluginBase(ABC):
    """Base class for HomeCore plugins written in Python.

    Subclasses must set :attr:`PLUGIN_ID` and implement :meth:`on_command`.
    Call :meth:`run` to connect and enter the event loop.
    """

    #: Unique plugin identifier — override in subclass.
    PLUGIN_ID: str = "plugin.unnamed"

    def __init__(
        self,
        broker_host: str | None = None,
        broker_port: int | None = None,
        password: str | None = None,
        protocol: int | None = None,
    ) -> None:
        self.broker_host = broker_host or os.getenv("HC_BROKER_HOST", "127.0.0.1")
        self.broker_port = broker_port or int(os.getenv("HC_BROKER_PORT", "1883"))
        self.password = password or os.getenv("HC_PLUGIN_PASSWORD", "")
        #: MQTT protocol level. ``None`` means 3.1.1, which is what homeCore's
        #: broker serves on its main port. Only set this if you are pointing at
        #: the separate v5 port.
        self.protocol = protocol
        self._client: Any = None  # paho.mqtt.client.Client
        self._started_at: float = time.time()
        self._management_enabled: bool = False
        self._config_path: str | None = None
        self._version: str | None = None
        #: Set by :meth:`from_config`, so :meth:`enable_management` can default
        #: to the same file rather than making every plugin pass it twice.
        self._bootstrap_config_path: str | None = None

        #: Conditions this plugin is currently reporting about itself. Raised
        #: and cleared by your code, republished in full on every heartbeat.
        self.notices = PluginNotices(on_change=self._on_notices_changed)

        # Devices this plugin has registered. Drives the heartbeat's
        # device_count, decides which command topics we subscribe to, and — once
        # persistence is enabled — survives a restart so reconcile_devices can
        # tell what has since disappeared.
        self._devices = DeviceTracker()

        self._capabilities: Capabilities | None = None
        self._active_streams: dict[str, StreamContext] = {}
        self._streams_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, path: str | None = None, **kwargs: Any) -> "PluginBase":
        """Build a plugin from the config file homeCore hands it.

        **This is how a plugin starts in a real install.** Every homeCore
        plugin, in every language, receives its config path as ``sys.argv[1]``
        — core owns that file, seeds it with a minted broker credential, and
        the operator edits it in the UI. A plugin runtime passes it the same
        way, as the last argument of its adapter's launch template.

        Without this, every plugin author writes the same twenty lines of
        tomllib and gets to invent their own bug in it::

            if __name__ == "__main__":
                MyPlugin.from_config().run()

        The ``[homecore]`` table is what core writes:

        .. code-block:: toml

            [homecore]
            broker_host = "127.0.0.1"
            broker_port = 1883
            plugin_id   = "plugin.my_light"
            password    = "…"

        Anything else in the file is yours; read it with
        :meth:`read_own_config` rather than parsing the file twice.

        :param path: Config file. Defaults to ``sys.argv[1]``.
        :param kwargs: Passed to the constructor, and they win — an explicit
            argument is someone deliberately overriding the file.
        :raises SystemExit: with a message naming the contract, when no path
            was given and ``argv`` has none. A plugin that cannot find its
            config cannot connect, and the failure it would otherwise produce
            is a broker auth error nowhere near the cause.
        """
        import sys
        import tomllib

        if path is None:
            if len(sys.argv) < 2:
                raise SystemExit(
                    f"{cls.__name__}: no config file. homeCore passes it as argv[1] — "
                    f"run this as `python -m your.module <config.toml>`."
                )
            path = sys.argv[1]

        try:
            with open(path, "rb") as f:
                doc = tomllib.load(f)
        except FileNotFoundError:
            raise SystemExit(f"{cls.__name__}: config file not found: {path}") from None
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"{cls.__name__}: {path} is not valid TOML: {exc}") from None

        hc = doc.get("homecore", {})
        settings: dict[str, Any] = {}
        for key in ("broker_host", "broker_port", "password"):
            if hc.get(key) is not None:
                settings[key] = hc[key]
        settings.update(kwargs)

        plugin = cls(**settings)
        # Instance attribute, deliberately: core is the authority on a plugin's
        # id, and one process should not be able to rename the class for
        # everything else that imports it.
        if hc.get("plugin_id"):
            plugin.PLUGIN_ID = hc["plugin_id"]
        plugin._bootstrap_config_path = path
        return plugin

    def read_own_config(self) -> dict:
        """The whole config file this plugin was started with, parsed.

        For plugin-specific settings that live beside ``[homecore]``. Returns
        an empty dict when the plugin was not built by :meth:`from_config`.
        """
        import tomllib

        if not self._bootstrap_config_path:
            return {}
        with open(self._bootstrap_config_path, "rb") as f:
            return tomllib.load(f)

    def publish_state(
        self,
        device_id: str,
        state: dict,
        *,
        change: dict | None = None,
    ) -> None:
        """Publish a full device state update (retained, QoS 1).

        :param device_id: The canonical HomeCore device identifier.
        :param state: Dict of attribute names → values.
        :param change: Optional ``_hc.change`` provenance payload.
        """
        topic = f"homecore/devices/{device_id}/state"
        self._publish(
            topic,
            json.dumps(self._with_state_change_metadata(state, change)),
            qos=1,
            retain=True,
        )

    def publish_state_partial(
        self,
        device_id: str,
        patch: dict,
        *,
        change: dict | None = None,
    ) -> None:
        """Publish a partial state update (JSON merge-patch, QoS 1, not retained).

        Use this for high-frequency sensors that send diffs rather than full state blobs.

        :param device_id: The canonical HomeCore device identifier.
        :param patch: Dict of attributes to merge into the current state.
        :param change: Optional ``_hc.change`` provenance payload.
        """
        topic = f"homecore/devices/{device_id}/state/partial"
        self._publish(
            topic,
            json.dumps(self._with_state_change_metadata(patch, change)),
            qos=1,
            retain=False,
        )

    def publish_state_for_command(
        self,
        device_id: str,
        state: dict,
        command_payload: dict,
        *,
        fallback_source: str | None = None,
    ) -> None:
        """Publish a full state update caused by a HomeCore command."""
        self.publish_state(
            device_id,
            state,
            change=self.change_from_command(
                command_payload,
                fallback_source=fallback_source,
            ),
        )

    def publish_state_partial_for_command(
        self,
        device_id: str,
        patch: dict,
        command_payload: dict,
        *,
        fallback_source: str | None = None,
    ) -> None:
        """Publish a partial state update caused by a HomeCore command."""
        self.publish_state_partial(
            device_id,
            patch,
            change=self.change_from_command(
                command_payload,
                fallback_source=fallback_source,
            ),
        )

    def register_device(
        self,
        device_id: str,
        name: str,
        capabilities: dict,
        area: str | None = None,
        *,
        manufacturer: str | None = None,
        model: str | None = None,
        sw_version: str | None = None,
        parent_device_id: str | None = None,
    ) -> None:
        """Publish a device registration message.

        :param device_id: Stable unique identifier for the device.
        :param name: Human-readable label.
        :param capabilities: JSON Schema object describing device attributes.
        :param area: Optional room/zone assignment.
        :param manufacturer: Who made it, as its own system reports it.
        :param model: What it is.
        :param sw_version: Firmware, as the device reports it — not any
            homeCore version.
        :param parent_device_id: What this device sits behind — a bulb's
            bridge, a node's controller, one outlet of a strip. Advisory:
            nothing routes through it, and it must be a device this plugin
            also registers. Home Assistant calls this ``via_device``.

        The last three are the same facts a Home Assistant integration puts in
        ``DeviceInfo``, so a port carries them straight across. homeCore acts on
        none of them; they are there for the operator looking at a device that
        has stopped working and needing to know which one it is and what it is
        running.

        Keyword-only and omitted when None: a plugin learns these at different
        times — a bridge names the manufacturer at discovery and the firmware
        only after the first poll — and an absent field leaves whatever core
        already knows alone rather than blanking it.
        """
        topic = f"homecore/plugins/{self.PLUGIN_ID}/register"
        hardware = {
            key: value
            for key, value in (
                ("manufacturer", manufacturer),
                ("model", model),
                ("sw_version", sw_version),
                ("parent_device_id", parent_device_id),
            )
            if value
        }
        payload = json.dumps(
            {
                "device_id": device_id,
                "plugin_id": self.PLUGIN_ID,
                "name": name,
                "area": area,
                "capabilities": capabilities,
                **hardware,
            }
        )
        self._publish(topic, payload, qos=1)
        self._track_device(device_id)

    def register_device_typed(
        self,
        device_id: str,
        name: str,
        device_type: str,
        area: str | None = None,
    ) -> None:
        """Register a device by type name.

        Instead of supplying a full capability schema, provide a ``device_type``
        string that HomeCore resolves against its built-in device-type catalog.
        This is the recommended path for well-known device categories.

        Example device types: ``"light"``, ``"switch"``, ``"motion_sensor"``,
        ``"contact_sensor"``, ``"temperature_sensor"``, ``"power_monitor"``,
        ``"cover"``, ``"lock"``, ``"climate"``, ``"virtual_switch"``, …

        :param device_id: Stable unique identifier for the device.
        :param name: Human-readable label.
        :param device_type: Type name from the device-type catalog.
        :param area: Optional room/zone assignment.
        """
        topic = f"homecore/plugins/{self.PLUGIN_ID}/register"
        payload = json.dumps(
            {
                "device_id": device_id,
                "plugin_id": self.PLUGIN_ID,
                "name": name,
                "area": area,
                "device_type": device_type,
            }
        )
        self._publish(topic, payload, qos=1)
        self._track_device(device_id)

    def unregister_device(self, device_id: str) -> None:
        """Retire a device from HomeCore.

        Clears retained state/availability/schema topics and then publishes a
        plugin-scoped unregister message so HomeCore deletes the stored device.
        """
        self._publish(f"homecore/devices/{device_id}/state", "", qos=1, retain=True)
        self._publish(
            f"homecore/devices/{device_id}/availability", "", qos=1, retain=True
        )
        self._publish(f"homecore/devices/{device_id}/schema", "", qos=1, retain=True)
        topic = f"homecore/plugins/{self.PLUGIN_ID}/unregister"
        payload = json.dumps({"device_id": device_id})
        self._publish(topic, payload, qos=1)
        self.unsubscribe_commands(device_id)
        self._devices.discard(device_id)

    def publish_availability(self, device_id: str, available: bool) -> None:
        """Publish an availability heartbeat (retained, QoS 1).

        :param device_id: The target device.
        :param available: ``True`` for ``"online"``, ``False`` for ``"offline"``.
        """
        topic = f"homecore/devices/{device_id}/availability"
        self._publish(topic, "online" if available else "offline", qos=1, retain=True)

    def publish_plugin_status(self, status: str) -> None:
        """Publish plugin status to ``homecore/plugins/{plugin_id}/status`` (retained).

        :param status: One of ``"active"``, ``"degraded"``, ``"offline"``.
        """
        topic = f"homecore/plugins/{self.PLUGIN_ID}/status"
        self._publish(topic, status, qos=1, retain=True)

    def register_device_full(
        self,
        device_id: str,
        name: str,
        device_type: str | None = None,
        area: str | None = None,
        capabilities: dict | None = None,
    ) -> None:
        """Register a device with all optional fields.

        Combines the functionality of :meth:`register_device` and
        :meth:`register_device_typed` into one call with every field optional.

        :param device_id: Stable unique identifier for the device.
        :param name: Human-readable label.
        :param device_type: Optional type name from the device-type catalog.
        :param area: Optional room/zone assignment.
        :param capabilities: Optional JSON Schema object describing device attributes.
        """
        topic = f"homecore/plugins/{self.PLUGIN_ID}/register"
        msg: dict[str, Any] = {
            "device_id": device_id,
            "plugin_id": self.PLUGIN_ID,
            "name": name,
        }
        if device_type is not None:
            msg["device_type"] = device_type
        if area is not None:
            msg["area"] = area
        if capabilities is not None:
            msg["capabilities"] = capabilities
        self._publish(topic, json.dumps(msg), qos=1)
        self._track_device(device_id)

    def register_device_schema(self, device_id: str, schema: dict) -> None:
        """Publish a device capability schema (retained, QoS 1).

        :param device_id: The target device.
        :param schema: Dict describing the device's capability schema.
        """
        topic = f"homecore/devices/{device_id}/schema"
        self._publish(topic, json.dumps(schema), qos=1, retain=True)

    def subscribe_commands(self, device_id: str) -> None:
        """Receive commands for one device.

        Every ``register_device*`` call does this for you, so you rarely need
        it. Reach for it only when homeCore knows about a device this plugin
        did not register.
        """
        self._track_device(device_id)

    def unsubscribe_commands(self, device_id: str) -> None:
        """Stop receiving commands for one device."""
        if self._client is not None:
            self._client.unsubscribe(f"homecore/devices/{device_id}/cmd")

    def enable_device_persistence(self, path: str) -> None:
        """Remember across restarts which devices this plugin registered.

        Call once at startup, before registering anything. The plugin id is
        inserted into the filename, so plugins sharing a config directory cannot
        share a snapshot and retire each other's devices.

        Without this, :meth:`reconcile_devices` can only see devices registered
        in the *current* process, so anything dropped while the plugin was down
        lingers in homeCore forever.

        :param path: Typically ``<config_dir>/.published-device-ids.json``.
        """
        self._devices.enable_persistence(scoped_snapshot_path(path, self.PLUGIN_ID))

    def reconcile_devices(self, live: set[str]) -> ReconcileReport:
        """Unregister every device this plugin knows about that is not in *live*.

        The "set what is live this cycle, let the SDK clean up the rest"
        workflow. Combined with :meth:`enable_device_persistence` it also
        retires devices registered in earlier runs.

        **Only call this after a sync you trust.** On a partial fetch it will
        unregister live devices behind a temporarily unreachable upstream. Track
        an "everything succeeded" flag across your per-source loop and pass the
        live set only when it holds.

        Ids in *live* that were never registered are reported in
        ``unknown_in_live`` and otherwise ignored — register them first if you
        meant to keep them.
        """
        known = self._devices.snapshot()
        stale = sorted(known - live)
        unknown = sorted(live - known)

        unregistered = []
        for device_id in stale:
            try:
                self.unregister_device(device_id)
                unregistered.append(device_id)
                logger.info("unregistered stale device %s", device_id)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                logger.warning("failed to unregister stale device %s: %s", device_id, exc)

        if unknown:
            logger.debug(
                "reconcile_devices saw %d live ids not registered with the SDK; "
                "register them first if they should be kept",
                len(unknown),
            )
        return ReconcileReport(stale_unregistered=unregistered, unknown_in_live=unknown)

    def subscribe_state(self, device_id: str) -> None:
        """Receive *state* updates for a device this plugin does not own.

        For cross-device consumers — a thermostat that reads sensors belonging
        to other plugins. Updates arrive on :meth:`on_state`.

        The broker ACL has to allow it: such a plugin needs
        ``allow_sub = ["homecore/devices/+/state"]``, which is broader than a
        typical plugin's.
        """
        if self._client is not None:
            self._client.subscribe(f"homecore/devices/{device_id}/state", qos=1)

    def unsubscribe_state(self, device_id: str) -> None:
        if self._client is not None:
            self._client.unsubscribe(f"homecore/devices/{device_id}/state")

    def publish_event(self, event_type: str, payload: dict) -> None:
        """Publish a structured event (QoS 1, not retained).

        :param event_type: The event type key (used as topic suffix).
        :param payload: Event payload dict, serialized as JSON.
        """
        topic = f"homecore/events/{event_type}"
        self._publish(topic, json.dumps(payload), qos=1, retain=False)

    def enable_management(
        self,
        interval_secs: int = 60,
        version: str | None = None,
        config_path: str | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        """Let homeCore supervise this plugin.

        Turns on the heartbeat, the remote management commands (ping, read and
        write config, change log level), and — if you pass *capabilities* — the
        action manifest that becomes buttons in the UI.

        Without this a plugin still runs, but homeCore cannot see it properly:
        no heartbeat means it shows as offline, and none of its actions or
        notices reach the operator.

        Call it from :meth:`on_connect`.

        :param interval_secs: Seconds between heartbeats. 60 is the norm; core
            marks a plugin offline after 90 seconds of silence.
        :param version: Your plugin's version, shown in the UI.
        :param config_path: Config file to serve for get_config/set_config. In
            a normal install this is ``sys.argv[1]`` — homeCore owns the file
            and hands you the path.
        :param capabilities: Your action manifest. See :class:`Capabilities`.
        """
        self._management_enabled = True
        self._version = version
        # Built by from_config? Then the file it read is the file core owns,
        # and making every plugin name it a second time is one more chance to
        # name a different one.
        self._config_path = config_path or self._bootstrap_config_path
        if capabilities is not None:
            capabilities.plugin_id = self.PLUGIN_ID
            self._capabilities = capabilities

        if self._client is not None:
            self._client.subscribe(
                f"homecore/plugins/{self.PLUGIN_ID}/manage/cmd", qos=1
            )
        self._publish_capabilities()

        def _heartbeat_loop():
            while True:
                self._publish_heartbeat()
                time.sleep(interval_secs)

        t = threading.Thread(
            target=_heartbeat_loop, daemon=True, name=f"{self.PLUGIN_ID}-heartbeat"
        )
        t.start()

    def _on_notices_changed(self) -> None:
        """Push a heartbeat as soon as the notice set changes.

        Notices ride on the heartbeat, so without this a condition raised just
        after startup would not reach the UI until the next beat — up to
        `interval_secs` of the operator looking at a plugin that seems fine.
        """
        if self._management_enabled and self._client is not None:
            self._publish_heartbeat()

    def _publish_heartbeat(self) -> None:
        device_count = len(self._devices)
        hb = {
            "timestamp": self._iso_now(),
            "version": self._version,
            "sdk_version": SDK_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "uptime_secs": round(time.time() - self._started_at),
            "device_count": device_count,
            # The full current set every beat. Core replaces rather than
            # merges, so a cleared condition disappears on its own and there is
            # nothing to expire.
            "notices": self.notices.to_wire(),
        }
        self._publish(
            f"homecore/plugins/{self.PLUGIN_ID}/heartbeat",
            json.dumps(hb),
            qos=1,
            retain=False,
        )

    def _publish_capabilities(self) -> None:
        """Publish the action manifest, retained.

        Retained because homeCore may start, or restart, after this plugin —
        without it a late-joining core would never learn the plugin has actions
        until the plugin happened to reconnect.
        """
        if self._capabilities is None:
            return
        self._capabilities.plugin_id = self.PLUGIN_ID
        self._publish(
            f"homecore/plugins/{self.PLUGIN_ID}/capabilities",
            json.dumps(self._capabilities.to_dict()),
            qos=1,
            retain=True,
        )

    def enable_log_forwarding(self, min_level: str = "INFO") -> None:
        """Attach an MQTT log handler to the root logger.

        Log records at or above *min_level* are published to
        ``homecore/plugins/{plugin_id}/logs`` as JSON (QoS 0, not retained).

        :param min_level: Minimum log level name (e.g. ``"DEBUG"``, ``"INFO"``).
        """
        handler = MqttLogHandler(self)
        handler.setLevel(getattr(logging, min_level.upper(), logging.INFO))
        logging.getLogger().addHandler(handler)

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def on_command(self, device_id: str, payload: dict) -> None:
        """Called when a command message arrives for one of this plugin's devices.

        :param device_id: The target device.
        :param payload: Decoded JSON command payload.
        """

    def on_connect(self) -> None:
        """Called once the broker connection is up.

        Register your devices here rather than in ``__init__``, so a reconnect
        re-registers them. Call :meth:`enable_management` here too.
        """

    def on_action(
        self,
        action: str,
        params: dict,
        ctx: StreamContext | None = None,
    ) -> dict | None:
        """Handle a capability action you declared in the manifest.

        Return a dict for an immediate action. For a streaming action, *ctx* is
        a :class:`~homecore_plugin_sdk.streaming.StreamContext` — report
        through it and return ``None``.

        Returning ``None`` from an *immediate* action tells the SDK you do not
        recognise the id, and it answers with ``unknown action``.

        Streaming handlers run on their own thread, so blocking here is fine
        and will not stall the MQTT loop.
        """
        return None

    def on_set_config(self, config) -> bool:
        """Accept a structured ``set_config`` payload.

        homeCore sends config as raw text when the operator edits TOML directly,
        and as an object when your plugin declared a config schema and the UI
        rendered a form. The SDK writes the text form verbatim; it cannot turn
        an object into TOML for you, so override this if you declare a schema.

        :returns: ``True`` if you handled and persisted it.
        """
        return False

    def on_state(self, device_id: str, state: dict) -> None:
        """A device you subscribed to with :meth:`subscribe_state` changed.

        Only for cross-device consumers. Devices this plugin owns arrive
        through :meth:`on_command` instead.
        """

    def extract_command_change(self, command_payload: dict) -> dict | None:
        """Extract ``_hc.command`` metadata from a decoded HomeCore command payload."""
        if not isinstance(command_payload, dict):
            return None
        hc = command_payload.get("_hc")
        if not isinstance(hc, dict):
            return None
        change = hc.get("command")
        if not isinstance(change, dict):
            return None
        return dict(change)

    def change_from_command(
        self,
        command_payload: dict,
        *,
        fallback_source: str | None = None,
    ) -> dict:
        """Resolve a command payload into a concrete HomeCore-originated change."""
        return self.extract_command_change(command_payload) or {
            "changed_at": self._iso_now(),
            "kind": "homecore",
            "source": fallback_source or self.PLUGIN_ID,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Connect to the broker and block until interrupted."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise ImportError("paho-mqtt is required: pip install paho-mqtt") from exc

        # VERSION2 explicitly. paho 2.x still defaults to VERSION1 for
        # backwards compatibility, and its callbacks take different arguments —
        # on_connect gets 4 rather than 5. Without this the connect callback
        # never fires (so nothing is ever registered) and the first disconnect
        # raises TypeError out of the network loop.
        #
        # MQTT 3.1.1, because homeCore's embedded broker serves v3 on its main
        # port and v5 on a separate `v5_port`. Connecting as v5 to the ordinary
        # port is a protocol mismatch: the broker closes the socket and paho
        # reconnects forever, reporting "Unspecified error". Pass
        # protocol=MQTTv5 and the v5 port together if you want v5.
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.PLUGIN_ID,
            protocol=self.protocol or mqtt.MQTTv311,
        )
        self._client = client

        if self.password:
            client.username_pw_set(self.PLUGIN_ID, self.password)

        def _on_connect(c, userdata, flags, reason_code, properties):
            if reason_code == 0:
                logger.info(
                    "Connected to broker at %s:%s", self.broker_host, self.broker_port
                )
                # Re-subscribe to the devices we already knew about. On a
                # reconnect the broker has forgotten our subscriptions, and
                # on_connect may register the same devices again — which is
                # idempotent, but this covers a plugin that registers lazily.
                for device_id in sorted(self._devices.snapshot()):
                    client.subscribe(f"homecore/devices/{device_id}/cmd", qos=1)
                if self._management_enabled:
                    client.subscribe(
                        f"homecore/plugins/{self.PLUGIN_ID}/manage/cmd", qos=1
                    )
                    self._publish_capabilities()
                self.on_connect()
            else:
                logger.error("Broker connection refused: reason_code=%s", reason_code)

        def _on_message(c, userdata, msg):
            self._on_message_handler(msg)

        def _on_disconnect(c, userdata, flags, reason_code, properties):
            if reason_code != 0:
                logger.warning("Disconnected from broker (reason_code=%s); will reconnect", reason_code)

        client.on_connect = _on_connect
        client.on_message = _on_message
        client.on_disconnect = _on_disconnect

        client.connect(self.broker_host, self.broker_port, keepalive=60)
        logger.info("Plugin %s entering event loop", self.PLUGIN_ID)
        client.loop_forever()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_message_handler(self, msg: Any) -> None:
        """Route an incoming MQTT message.  Extracted for unit-testability."""
        parts = msg.topic.split("/")

        # Device commands: homecore/devices/{device_id}/cmd
        if (
            len(parts) == 4
            and parts[0] == "homecore"
            and parts[1] == "devices"
            and parts[3] == "cmd"
        ):
            device_id = parts[2]
            # Belt and braces alongside the per-device subscription: a broker
            # that hands us a topic we did not ask for must not turn into this
            # plugin acting on another plugin's device.
            if device_id not in self._devices:
                logger.debug("ignoring command for unowned device %s", device_id)
                return
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                payload = {"raw": msg.payload.decode(errors="replace")}
            self.on_command(device_id, payload)
            return

        # State of a device owned by someone else, for cross-device consumers:
        # homecore/devices/{device_id}/state
        if (
            len(parts) == 4
            and parts[0] == "homecore"
            and parts[1] == "devices"
            and parts[3] == "state"
        ):
            try:
                state = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                return
            if isinstance(state, dict):
                self.on_state(parts[2], state)
            return

        # Management commands: homecore/plugins/{plugin_id}/manage/cmd
        if (
            self._management_enabled
            and len(parts) == 5
            and parts[0] == "homecore"
            and parts[1] == "plugins"
            and parts[2] == self.PLUGIN_ID
            and parts[3] == "manage"
            and parts[4] == "cmd"
        ):
            self._handle_manage_cmd(msg)

    def _publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        if self._client is None:
            logger.warning("publish called before run(): topic=%s", topic)
            return
        logger.debug("publish topic=%s retain=%s", topic, retain)
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def _with_state_change_metadata(self, payload: dict, change: dict | None) -> dict:
        if not change or not isinstance(payload, dict):
            return payload
        next_payload = dict(payload)
        hc = next_payload.get("_hc")
        next_hc = dict(hc) if isinstance(hc, dict) else {}
        next_hc["change"] = dict(change)
        next_payload["_hc"] = next_hc
        return next_payload

    def _handle_manage_cmd(self, msg: Any) -> None:
        """Process a management command message."""
        resp_topic = f"homecore/plugins/{self.PLUGIN_ID}/manage/response"
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            return
        action = payload.get("action")
        request_id = payload.get("request_id")

        if action == "ping":
            self._publish(
                resp_topic,
                json.dumps({"request_id": request_id, "status": "ok"}),
                qos=1,
            )
        elif action == "get_config":
            if not self._config_path:
                self._respond(request_id, error="no config path configured")
            else:
                try:
                    with open(self._config_path, "r") as f:
                        content = f.read()
                    # The key is `data`. Core reads resp["data"] and falls back
                    # to the whole envelope when it is absent, so getting this
                    # wrong shows the operator {request_id, status, ...} where
                    # the config should be.
                    self._respond(request_id, extra={"data": content})
                except OSError as exc:
                    self._respond(request_id, error=str(exc))

        elif action == "set_config":
            self._handle_set_config(payload, request_id)
        elif action == "cancel":
            target = payload.get("target_request_id")
            with self._streams_lock:
                ctx = self._active_streams.get(target)
            if ctx is None:
                self._respond(
                    request_id, error="no active stream for target_request_id"
                )
            else:
                ctx._cancel()
                self._respond(request_id)

        elif action == "respond":
            target = payload.get("target_request_id")
            with self._streams_lock:
                ctx = self._active_streams.get(target)
            if ctx is None:
                self._respond(
                    request_id,
                    error="no active awaiting_user stream for target_request_id",
                )
            else:
                ctx._deliver_response(payload.get("response") or {})
                self._respond(request_id)

        elif action == "set_log_level":
            level_name = payload.get("level", "INFO").upper()
            level = getattr(logging, level_name, None)
            if level is not None:
                logging.getLogger().setLevel(level)
                self._publish(
                    resp_topic,
                    json.dumps({"request_id": request_id, "status": "ok"}),
                    qos=1,
                )
            else:
                self._publish(
                    resp_topic,
                    json.dumps({"request_id": request_id, "status": "error", "error": f"unknown level: {level_name}"}),
                    qos=1,
                )

        else:
            self._dispatch_action(action, request_id, payload)

    def _handle_set_config(self, payload: dict, request_id: str) -> None:
        """Write a ``set_config`` payload.

        The field is ``config``, not ``content`` — reading the wrong key used to
        mean an absent value defaulting to ``""``, which **truncated the
        plugin's config file** on every save.

        Core sends a string when the operator edited raw TOML and an object when
        the plugin declared a config schema and the UI rendered a form. It also
        forwards the request body verbatim when that body has no top-level
        ``config`` key, so the raw editor arrives as ``{"raw": "<text>"}``.
        Strings are written as-is; anything else is :meth:`on_set_config`'s to
        handle, because turning an object into TOML is not something this SDK
        can do for you.
        """
        if not self._config_path:
            self._respond(request_id, error="no config path configured")
            return

        config = payload.get("config")
        if isinstance(config, dict) and isinstance(config.get("raw"), str):
            config = config["raw"]

        if not isinstance(config, str):
            if self.on_set_config(config):
                self._respond(request_id)
            else:
                self._respond(
                    request_id,
                    error="structured config received; override on_set_config(config) "
                    "to accept it, or edit the raw form instead",
                )
            return

        try:
            with open(self._config_path, "w") as f:
                f.write(config)
            self._respond(request_id)
        except OSError as exc:
            self._respond(request_id, error=str(exc))

    # ------------------------------------------------------------------
    # Capability actions
    # ------------------------------------------------------------------

    def _declared_action(self, action_id: str) -> Action | None:
        if self._capabilities is None:
            return None
        for a in self._capabilities.actions:
            if a.id == action_id:
                return a
        return None

    def _dispatch_action(self, action: str, request_id: str, payload: dict) -> None:
        """Route a management command that is not a built-in to :meth:`on_action`."""
        declared = self._declared_action(action)
        # Params are everything that is not protocol envelope.
        params = {
            k: v
            for k, v in payload.items()
            if k not in ("action", "request_id", "target_request_id")
        }

        if declared is not None and declared.stream:
            self._start_stream(declared, request_id, params)
            return

        try:
            result = self.on_action(action, params, None)
        except Exception as exc:  # noqa: BLE001 - a plugin bug must not kill the loop
            logger.exception("action %s raised", action)
            self._respond(request_id, error=f"action failed: {exc}")
            return

        if result is None:
            self._respond(request_id, error=f"unknown action: {action}")
        else:
            self._respond(request_id, extra=result)

    def _start_stream(self, declared: Action, request_id: str, params: dict) -> None:
        """Run a streaming action on its own thread and answer ``accepted``."""
        if not request_id:
            self._respond("", error="streaming action requires request_id")
            return

        if declared.concurrency is Concurrency.SINGLE:
            with self._streams_lock:
                busy = next(
                    (
                        rid
                        for rid, c in self._active_streams.items()
                        if c.action_id == declared.id
                    ),
                    None,
                )
            if busy is not None:
                self._respond(
                    request_id, status="busy", extra={"active_request_id": busy}
                )
                return

        ctx = StreamContext(self, request_id, declared.id)
        with self._streams_lock:
            self._active_streams[request_id] = ctx

        def _run() -> None:
            error: BaseException | None = None
            try:
                self.on_action(declared.id, params, ctx)
            except Exception as exc:  # noqa: BLE001
                logger.exception("streaming action %s raised", declared.id)
                error = exc
            finally:
                # Guarantees exactly one terminal stage even if the handler
                # returned without emitting one, then clears the retained topic.
                ctx._finalize(error)
                with self._streams_lock:
                    self._active_streams.pop(request_id, None)

        # Distinct prefix so a caller (or a test) can find live action threads
        # without also matching the heartbeat thread, which never exits.
        threading.Thread(
            target=_run, daemon=True, name=f"hc-action-{declared.id}-{request_id}"
        ).start()

        # Answer immediately; the work continues on the thread above.
        self._respond(
            request_id, status="accepted", extra={"stream_topic": ctx.topic}
        )

    def _respond(
        self,
        request_id: str,
        *,
        status: str = "ok",
        error: str | None = None,
        extra: dict | None = None,
    ) -> None:
        body: dict[str, Any] = {"request_id": request_id}
        if error is not None:
            body["status"] = "error"
            body["error"] = error
        else:
            body["status"] = status
            if extra:
                body.update(extra)
        self._publish(
            f"homecore/plugins/{self.PLUGIN_ID}/manage/response",
            json.dumps(body),
            qos=1,
        )

    def _track_device(self, device_id: str) -> None:
        """Record a device as ours and subscribe to its command topic.

        Registration and subscription are one step here on purpose. In the Rust
        SDK they are separate calls, and forgetting the second is the classic
        first-plugin bug: the device appears in homeCore, its state updates,
        and every command silently goes nowhere.
        """
        new = device_id not in self._devices
        self._devices.add(device_id)
        if new and self._client is not None:
            self._client.subscribe(f"homecore/devices/{device_id}/cmd", qos=1)

    def _iso_now(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MqttLogHandler(logging.Handler):
    """A :class:`logging.Handler` that publishes log records to MQTT.

    Each record is serialized as a JSON ``LogLine`` and published to
    ``homecore/plugins/{plugin_id}/logs`` with QoS 0, not retained.
    """

    def __init__(self, plugin: PluginBase) -> None:
        super().__init__()
        self._plugin = plugin

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from datetime import datetime, timezone

            log_line = {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "level": record.levelname,
                "target": record.name,
                "message": self.format(record) if self.formatter else record.getMessage(),
                "fields": None,
            }
            topic = f"homecore/plugins/{self._plugin.PLUGIN_ID}/logs"
            self._plugin._publish(topic, json.dumps(log_line), qos=0, retain=False)
        except Exception:
            self.handleError(record)
