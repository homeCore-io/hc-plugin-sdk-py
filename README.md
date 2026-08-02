# hc-plugin-sdk-py

Python plugin SDK for HomeCore. Subclass `PluginBase`, implement `on_command`, call `run()`.

## Quick start

```bash
pip install -e path/to/hc-plugin-sdk-py
```

```python
from homecore_plugin_sdk import PluginBase

class MyPlugin(PluginBase):
    PLUGIN_ID = "plugin.example"

    def on_command(self, device_id: str, payload: dict):
        print(f"Command for {device_id}: {payload}")

plugin = MyPlugin(broker_host="127.0.0.1", broker_port=1883)
plugin.register_device_full("example_sensor", "Example Sensor", device_type="sensor")
plugin.publish_state("example_sensor", {"temperature": 21.5})
plugin.run()
```

## Features

- **PluginBase** — abstract base class handling MQTT connection and command dispatch
- **Device registration** — full schema or by type name from catalog
- **State publishing** — full (retained) and partial (merge-patch)
- **Management protocol** — heartbeat, remote config, dynamic log level
- **Log forwarding** — configurable min level forwarded to core via MQTT
- **Configuration** — constructor params, env vars (`HC_BROKER_HOST`, `HC_BROKER_PORT`, `HC_PLUGIN_PASSWORD`), or defaults

## What this SDK does not have

The Rust SDK is the reference implementation and is ahead of this one in two
places that are worth knowing about before you choose a language:

- **Notices** — the structured, self-clearing problem reports the web UI shows
  on a plugin's card ("bridge unreachable", "no devices found yet"). A plugin
  written with this SDK can log a problem, but cannot surface it there.
- **Capability actions** — the plugin-level command manifest that makes the UI
  render buttons ("Pair bridge", "Rescan") and lets MCP call them, with no UI
  code. Device *capability schemas* work fine here; it is the plugin's own
  action manifest that is Rust-only.

Everything else — registration, state publishing, availability, the
management protocol, log forwarding — is the same across all four SDKs.

Requires Python 3.11+.
