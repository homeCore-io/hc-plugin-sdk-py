# hc-plugin-sdk-py

[![CI](https://github.com/homeCore-io/hc-plugin-sdk-py/actions/workflows/ci.yml/badge.svg)](https://github.com/homeCore-io/hc-plugin-sdk-py/actions/workflows/ci.yml)

Write a [homeCore](https://github.com/homeCore-io/homeCore) plugin in Python.

Subclass `PluginBase`, say what your devices are, handle commands. The SDK
covers the MQTT connection, registration, the management protocol, notices, and
capability actions.

```bash
pip install git+https://github.com/homeCore-io/hc-plugin-sdk-py@v0.2.0
```

Requires Python 3.11+.

Not on PyPI yet — install from the tag, which pins you to a known release the
same way the Rust SDK's git dependency does.

## Your first plugin

```python
from homecore_plugin_sdk import PluginBase

class MyLight(PluginBase):
    PLUGIN_ID = "plugin.mylight"

    def on_connect(self):
        # Register here, not in __init__ — this runs again after a reconnect.
        self.register_device_full("light.01", "Desk Lamp", device_type="light")
        self.publish_availability("light.01", True)
        self.publish_state("light.01", {"on": False, "brightness": 0})

    def on_command(self, device_id, payload):
        # Do whatever the real device needs, then publish what actually
        # happened — homeCore never writes device state itself.
        state = {"on": bool(payload.get("on"))}
        self.publish_state_for_command(device_id, state, payload,
                                       fallback_source="mylight")

MyLight().run()
```

`run()` blocks. Point it at a broker with constructor arguments, or the
`HC_BROKER_HOST` / `HC_BROKER_PORT` / `HC_PLUGIN_PASSWORD` environment
variables.

## How a plugin fits into homeCore

Everything travels over MQTT. Your plugin owns its devices' state; homeCore
owns the rules and the UI.

```
your device  ←→  your plugin  ──state──▶  homeCore  ──▶  rules, UI, history
                              ◀──cmd────
```

Three consequences worth internalising:

1. **Publish what happened, not what was asked.** A command is a request. If
   the bulb refuses, publish the state it is actually in. That is why the UI
   can show a light as off after a failed command instead of lying.
2. **Register in `on_connect`.** It fires on every connect, so a reconnect
   re-registers and re-subscribes.
3. **You only see your own devices.** Registering a device subscribes to that
   device's command topic and nothing else.

## Installing it into homeCore

In a normal install you do not copy config files around. homeCore owns your
plugin's config at `config/plugins/<plugin_id>.toml` and passes the path as
`sys.argv[1]`:

```python
import sys
config_path = sys.argv[1] if len(sys.argv) > 1 else "config/config.toml"
```

Declare the plugin in homeCore's `homecore.toml` so it gets supervised:

```toml
[[plugins]]
id      = "plugin.mylight"
binary  = "/usr/bin/python3"
config  = "config/plugins/plugin.mylight.toml"
enabled = true
```

## Management: heartbeat, config, actions

Call `enable_management()` from `on_connect` and homeCore can supervise the
plugin — heartbeat it, restart it, read and write its config, change its log
level. Without it the plugin runs but shows as offline.

```python
def on_connect(self):
    self.enable_management(
        interval_secs=60,          # core marks a plugin offline after 90s
        version="1.2.0",
        config_path=sys.argv[1],
    )
```

## Notices — telling the operator what is wrong

A status of *active* answers "is the process alive". It cannot say "alive, but
unable to do its job", and that is the state operators actually get stuck in.

A notice puts your diagnosis on the plugin's card in the UI:

```python
from homecore_plugin_sdk import PluginNotice

if not self.bridge_reachable():
    self.notices.raise_(PluginNotice.error(
        "bridge_unreachable",
        "The bridge stopped answering, so no device state is updating.",
        remedy="Check that the bridge is powered on and on this network.",
    ))
else:
    self.notices.clear("bridge_unreachable")
```

**A notice is state, not a log line.** The full set rides on every heartbeat
and homeCore replaces what it held, so a cleared notice disappears on its own —
nothing to acknowledge, nothing to expire.

The trap is raising once at startup and never looking again. A plugin that
reports `no_devices_configured` at boot is still showing it after the operator
has added devices. Re-derive conditions where you already loop: after a poll,
after a reconnect, after a config change. `notices.set([...])` replaces the
whole set at once, which is the safest shape when a sync cycle recomputes
everything.

Levels are `PluginNotice.info`, `.warning`, and `.error`.

## Capability actions — buttons in the UI

Declare an action and it appears as a button on your plugin's page, and becomes
callable from hc-mcp. Neither needs code written for your plugin specifically.

```python
from homecore_plugin_sdk import Action, Capabilities

def on_connect(self):
    self.enable_management(
        config_path=sys.argv[1],
        capabilities=Capabilities(actions=[
            Action(id="rescan", label="Rescan devices",
                   description="Ask the bridge for its current device list."),
        ]),
    )

def on_action(self, action, params, ctx=None):
    if action == "rescan":
        found = self.rescan()
        return {"found": len(found)}       # a dict is the result
    return None                            # None means "not mine"
```

### Actions that take a while

Set `stream=True` and your handler receives a `StreamContext` to report through
as it works. That is what drives a live progress bar and a list of devices
appearing one at a time, instead of a spinner that says nothing.

```python
Action(id="discover", label="Discover devices", stream=True,
       cancelable=True, item_key="serial", timeout_ms=30_000)
```

```python
def on_action(self, action, params, ctx=None):
    if action == "discover":
        hosts = self.candidates()
        for i, host in enumerate(hosts):
            if ctx.is_canceled():          # cooperative — nothing interrupts you
                ctx.canceled()
                return
            ctx.progress(percent=100 * i // len(hosts), message=f"Probing {host}")
            if (dev := probe(host)):
                ctx.item_add({"serial": dev.serial, "name": dev.name})
        ctx.complete({"found": len(hosts)})
```

Streaming handlers run on their own thread, so blocking is fine.

| Stage | Meaning |
|---|---|
| `ctx.progress(...)` | percent / label / message, as often as useful |
| `ctx.item_add/update/remove(...)` | one thing found or changed — include the `item_key` field so the UI updates a row rather than appending |
| `ctx.warning(...)` | recoverable; **the stream continues** |
| `ctx.awaiting_user(prompt)` | ask for something, then `ctx.await_respond()` |
| `ctx.complete(data)` | terminal, success |
| `ctx.error(message)` | terminal, failure |
| `ctx.canceled()` | terminal, after you notice `is_canceled()` |

Terminal stages are latched — the first wins. If your handler returns without
emitting one, the SDK sends an `error`, so the UI is never left waiting on a
stream that quietly stopped.

### Asking the operator something

```python
ctx.awaiting_user("Press the pairing button on the device now.")
answer = ctx.await_respond(timeout=60)
```

## Cross-device plugins

To read devices you do **not** own — a thermostat consuming sensors from other
plugins — subscribe explicitly and handle `on_state`:

```python
def on_connect(self):
    self.subscribe_state("sensor.hallway_temp")

def on_state(self, device_id, state):
    self.recompute(device_id, state)
```

This needs a broader broker ACL than a normal plugin:
`allow_sub = ["homecore/devices/+/state"]`.

## API reference

### Devices

| Method | Purpose |
|---|---|
| `register_device_full(id, name, device_type=, area=, capabilities=)` | Register. Everything optional but id and name |
| `register_device_typed(id, name, device_type, area=)` | Register against a built-in type |
| `register_device(id, name, capabilities, area=)` | Register with an explicit JSON Schema |
| `register_device_schema(id, schema)` | Publish a schema separately |
| `unregister_device(id)` | Retire it and clear its retained topics |
| `publish_availability(id, bool)` | online / offline |

Registering also subscribes to that device's commands. In the Rust SDK those
are two separate calls and forgetting the second is the classic first-plugin
bug; here it is one.

### State

| Method | Purpose |
|---|---|
| `publish_state(id, state)` | Full state, retained |
| `publish_state_partial(id, patch)` | Merge-patch — only the keys given |
| `publish_state_for_command(id, state, cmd, fallback_source=)` | Full state, with provenance from the command |
| `publish_state_partial_for_command(...)` | The partial equivalent |

Use the `_for_command` forms when responding to a command: they carry who
caused the change, so the UI and the audit log can say so.

### Plugin

| Method | Purpose |
|---|---|
| `enable_management(interval_secs=, version=, config_path=, capabilities=)` | Heartbeat, remote management, action manifest |
| `enable_log_forwarding(min_level=)` | Send your logs to homeCore's live log stream |
| `publish_plugin_status(status)` | active / degraded / offline |
| `publish_event(type, payload)` | A structured event on the bus |
| `notices` | `.raise_()`, `.clear()`, `.set()`, `.snapshot()` |
| `enable_device_persistence(path)` | Remember registered devices across restarts |
| `reconcile_devices(live)` | Unregister everything not in the live set |

### Hooks to override

| Hook | When |
|---|---|
| `on_connect()` | Connected. Register devices, enable management |
| `on_command(device_id, payload)` | A command for one of your devices |
| `on_action(action, params, ctx=None)` | A capability action |
| `on_state(device_id, state)` | A device you subscribed to changed |
| `on_set_config(config)` | A structured config write |

## Secrets in logs

`enable_log_forwarding()` publishes to a topic anything can subscribe to. Do
not interpolate credentials into log messages — the text is forwarded verbatim.

## Remote config

With `config_path` set, homeCore can read and write your config file. The raw
TOML editor sends text, which the SDK writes verbatim.

If you declare a config schema, the UI renders a form and sends a structured
object instead. The SDK will not guess at TOML serialisation, so override
`on_set_config` to take it:

```python
def on_set_config(self, config):
    with open(self._config_path, "w") as f:
        f.write(to_toml(config))
    return True          # False → the SDK answers with an error
```

## Device persistence

When a device disappears from your upstream — a bulb deleted from the bridge, a
Z-Wave node excluded, an entry removed from config — its homeCore record has to
go too. Otherwise it lingers forever, still shown in the UI and still accepting
commands nothing will execute.

Knowing what disappeared means knowing what existed *before*, and a plugin that
has just restarted knows nothing. So the SDK can mirror every
register/unregister to a small JSON file:

```python
def on_connect(self):
    # Once, before registering anything.
    self.enable_device_persistence(f"{config_dir}/.published-device-ids.json")
```

Then, after a sync where you know the full live set:

```python
report = self.reconcile_devices({d.id for d in bridge.devices()})
# report.stale_unregistered — gone upstream, now gone from homeCore
# report.unknown_in_live    — ids you passed but never registered
```

Devices registered in *earlier runs* are retired too, which is the point: a
fresh process that has registered nothing can still clean up what the previous
one left behind.

The plugin id is inserted into the filename
(`.published-device-ids.plugin.hue.json`), because real deployments keep every
plugin's config in one directory and every plugin derives this path the same
way — unscoped, they would share one file and retire each other's devices.

**Only reconcile after a sync you trust.** On a partial fetch this unregisters
live devices behind a temporarily unreachable upstream — which looks exactly
like the bug it exists to prevent, except the devices were fine. Track an
"everything succeeded" flag across your per-source loop and pass the live set
only when it holds.

Plugins whose upstream reports irregularly — battery sensors that go quiet for
hours — should enable persistence but skip auto-reconcile. An operator can clear
zombies with `DELETE /api/v1/plugins/{id}/devices`.

## Parity with the Rust SDK

Everything the Rust SDK does is here: registration, state, availability, the
management protocol, log forwarding, notices, capability actions including
streaming, and cross-device state subscription.

Everything the Rust SDK does is here. There is no gap left.

## Development

```bash
pip install -e '.[dev]'
pytest -q tests/
```

`examples/virtual_light.py` is a complete plugin. `examples/discovery_plugin.py`
demonstrates notices and both kinds of capability action.

## License

Dual-licensed under **MIT** or **Apache-2.0**, at your option.
