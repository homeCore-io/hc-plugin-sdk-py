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
    ) -> None:
        self.broker_host = broker_host or os.getenv("HC_BROKER_HOST", "127.0.0.1")
        self.broker_port = broker_port or int(os.getenv("HC_BROKER_PORT", "1883"))
        self.password = password or os.getenv("HC_PLUGIN_PASSWORD", "")
        self._client: Any = None  # paho.mqtt.client.Client
        self._started_at: float = time.time()
        self._management_enabled: bool = False
        self._config_path: str | None = None
        self._version: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
    ) -> None:
        """Publish a device registration message.

        :param device_id: Stable unique identifier for the device.
        :param name: Human-readable label.
        :param capabilities: JSON Schema object describing device attributes.
        :param area: Optional room/zone assignment.
        """
        topic = f"homecore/plugins/{self.PLUGIN_ID}/register"
        payload = json.dumps(
            {
                "device_id": device_id,
                "plugin_id": self.PLUGIN_ID,
                "name": name,
                "area": area,
                "capabilities": capabilities,
            }
        )
        self._publish(topic, payload, qos=1)

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

    def register_device_schema(self, device_id: str, schema: dict) -> None:
        """Publish a device capability schema (retained, QoS 1).

        :param device_id: The target device.
        :param schema: Dict describing the device's capability schema.
        """
        topic = f"homecore/devices/{device_id}/schema"
        self._publish(topic, json.dumps(schema), qos=1, retain=True)

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
    ) -> None:
        """Enable the management protocol (heartbeat + remote commands).

        :param interval_secs: Seconds between heartbeat publishes.
        :param version: Plugin version string included in heartbeats.
        :param config_path: Path to a config file for get_config/set_config commands.
        """
        self._management_enabled = True
        self._version = version
        self._config_path = config_path

        # Subscribe to management commands
        cmd_topic = f"homecore/plugins/{self.PLUGIN_ID}/manage/cmd"
        if self._client is not None:
            self._client.subscribe(cmd_topic, qos=1)

        # Start heartbeat daemon thread
        def _heartbeat_loop():
            while True:
                uptime = time.time() - self._started_at
                hb = {
                    "timestamp": self._iso_now(),
                    "version": self._version,
                    "uptime_secs": round(uptime),
                }
                self._publish(
                    f"homecore/plugins/{self.PLUGIN_ID}/heartbeat",
                    json.dumps(hb),
                    qos=1,
                    retain=False,
                )
                time.sleep(interval_secs)

        t = threading.Thread(target=_heartbeat_loop, daemon=True)
        t.start()

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
        """Called after the broker connection is established.  Override to
        register devices and perform startup subscriptions."""

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

        client = mqtt.Client(client_id=self.PLUGIN_ID, protocol=mqtt.MQTTv5)
        self._client = client

        if self.password:
            client.username_pw_set(self.PLUGIN_ID, self.password)

        def _on_connect(c, userdata, flags, reason_code, properties):
            if reason_code == 0:
                logger.info("Connected to broker at %s:%s", self.broker_host, self.broker_port)
                client.subscribe("homecore/devices/+/cmd", qos=1)
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
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, ValueError):
                payload = {"raw": msg.payload.decode(errors="replace")}
            self.on_command(device_id, payload)
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
            if self._config_path:
                try:
                    with open(self._config_path, "r") as f:
                        content = f.read()
                    self._publish(
                        resp_topic,
                        json.dumps({"request_id": request_id, "status": "ok", "content": content}),
                        qos=1,
                    )
                except OSError as exc:
                    self._publish(
                        resp_topic,
                        json.dumps({"request_id": request_id, "status": "error", "error": str(exc)}),
                        qos=1,
                    )
            else:
                self._publish(
                    resp_topic,
                    json.dumps({"request_id": request_id, "status": "error", "error": "no config_path configured"}),
                    qos=1,
                )
        elif action == "set_config":
            content = payload.get("content", "")
            if self._config_path:
                try:
                    with open(self._config_path, "w") as f:
                        f.write(content)
                    self._publish(
                        resp_topic,
                        json.dumps({"request_id": request_id, "status": "ok"}),
                        qos=1,
                    )
                except OSError as exc:
                    self._publish(
                        resp_topic,
                        json.dumps({"request_id": request_id, "status": "error", "error": str(exc)}),
                        qos=1,
                    )
            else:
                self._publish(
                    resp_topic,
                    json.dumps({"request_id": request_id, "status": "error", "error": "no config_path configured"}),
                    qos=1,
                )
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
