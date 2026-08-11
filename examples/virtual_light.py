#!/usr/bin/env python3
"""virtual_light.py — A software-only dimmable light plugin for HomeCore.

Simulates a dimmable light bulb.  On startup it registers with the broker,
publishes its initial state, then toggles on/off and steps brightness every
5 seconds.  It also responds to commands received via MQTT.

Started the way homeCore starts every plugin — with its config file as
``sys.argv[1]``::

    python virtual_light.py /path/to/config.toml

That is the contract in every language, and it is what a plugin runtime's
adapter passes as the last argument of its launch template. This example used
to take ``--broker/--port/--id`` flags instead, which made it a poor thing to
copy: a plugin built that way cannot be started by core or by a runtime at all.

For local experimentation without core, write the file core would have::

    [homecore]
    broker_host = "127.0.0.1"
    broker_port = 1883
    plugin_id   = "plugin.virtual_py"
"""

import logging
import threading
import time

from homecore_plugin_sdk import PluginBase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger("virtual_light")

DEVICE_ID = "light.virtual_py_01"
CAPABILITIES = {
    "on":         {"type": "boolean"},
    "brightness": {"type": "integer", "minimum": 0, "maximum": 255},
}


class VirtualLightPlugin(PluginBase):
    """Simulated dimmable light bulb plugin."""

    PLUGIN_ID = "plugin.virtual_py"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._state = {"on": False, "brightness": 128}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # PluginBase hooks
    # ------------------------------------------------------------------

    def on_connect(self) -> None:
        # Without this core has no heartbeat from the plugin and shows it as
        # offline 90 seconds in, however well it is actually working. The
        # config path comes from from_config(), so get_config/set_config in the
        # UI edit the same file core seeded.
        self.enable_management(version="1.0.0")

        self.register_device(DEVICE_ID, "Virtual Light (Python)", CAPABILITIES, area="living_room")
        self.publish_availability(DEVICE_ID, True)

        with self._lock:
            state = dict(self._state)
        self.publish_state(DEVICE_ID, state)
        self.publish_plugin_status("active")

        logger.info("Device registered and initial state published: %s", state)

        # Periodic toggle in a background daemon thread so run() can block.
        t = threading.Thread(target=self._tick_loop, daemon=True, name="virtual-light-tick")
        t.start()

    def on_command(self, device_id: str, payload: dict) -> None:
        logger.info("Command received for %s: %s", device_id, payload)
        with self._lock:
            self._state.update({k: v for k, v in payload.items() if k in CAPABILITIES})
            state = dict(self._state)
        self.publish_state_for_command(
            device_id,
            state,
            payload,
            fallback_source="virtual_light",
        )
        logger.info("State after command: %s", state)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _tick_loop(self) -> None:
        """Toggle on/off and step brightness every 5 seconds."""
        while True:
            time.sleep(5)
            with self._lock:
                self._state["on"] = not self._state["on"]
                self._state["brightness"] = (self._state["brightness"] + 16) % 256
                state = dict(self._state)
            logger.info("Periodic tick — publishing state: %s", state)
            self.publish_state(DEVICE_ID, state)


def main() -> None:
    # Reads [homecore] from argv[1] — broker, port, plugin id and the minted
    # password core seeded — and remembers the path so enable_management can
    # serve get_config/set_config from the same file.
    plugin = VirtualLightPlugin.from_config()

    logger.info("Virtual light plugin starting")
    logger.info("  Device ID : %s", DEVICE_ID)
    logger.info("  Broker    : %s:%s", plugin.broker_host, plugin.broker_port)
    logger.info("Press Ctrl-C to stop")

    try:
        plugin.run()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        plugin.publish_availability(DEVICE_ID, False)
        plugin.publish_plugin_status("offline")


if __name__ == "__main__":
    main()
