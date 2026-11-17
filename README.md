# hc-plugin-sdk-py

[![CI](https://github.com/homeCore-io/hc-plugin-sdk-py/actions/workflows/ci.yml/badge.svg)](https://github.com/homeCore-io/hc-plugin-sdk-py/actions/workflows/ci.yml)

Write a [homeCore](https://github.com/homeCore-io/homeCore) plugin in Python.

Subclass `PluginBase`, say what your devices are, handle commands. The SDK
covers the MQTT connection, registration, the management protocol, notices,
alerts, and capability actions.

## Installing

Requires Python 3.11+.

### Working inside the homeCore workspace

This is the normal case today. If you cloned the whole workspace — `core/`,
`plugins/`, `sdks/` side by side — install from the checkout. Your plugin then
picks up SDK edits with no commit, tag, or reinstall in between, which is what
you want while developing the two together.

```bash
# from your plugin's directory, e.g. plugins/hc-mything/
pip install -e ../../sdks/hc-plugin-sdk-py
```

`-e` is the point: it installs a link to the checkout rather than a copy, so an
edit in `sdks/hc-plugin-sdk-py` is live in your plugin immediately.

### From a release

Nothing is published to PyPI yet. Install from the GitHub tag, which pins you
to a known release the same way the Rust SDK's git dependency does:

```bash
pip install git+https://github.com/homeCore-io/hc-plugin-sdk-py@v0.5.5
```

## Your first plugin

```python
from homecore_plugin_sdk import PluginBase

class MyLight(PluginBase):
    PLUGIN_ID = "plugin.mylight"

    def on_connect(self):
        # Register here, not in __init__ — this runs again after a reconnect.
        self.register_device_full("light.01", "Desk Lamp", device_type="light")
        self.publish_availability("light.01", True)
        self.publish_state("light.01", {"on": False, "brightness_pct": 0})

    def on_command(self, device_id, payload):
        # Do whatever the real device needs, then publish what actually
        # happened — homeCore never writes device state itself.
        state = {"on": bool(payload.get("on"))}
        self.publish_state_for_command(device_id, state, payload,
                                       fallback_source="mylight")

if __name__ == "__main__":
    MyLight.from_config().run()
```

homeCore starts a plugin with one argument, the path of its config file, and
writes a `[homecore]` table into that file:

```toml
[homecore]
broker_host = "127.0.0.1"
broker_port = 1883
plugin_id   = "plugin.mylight"
password    = "..."
```

`from_config()` reads `sys.argv[1]`, connects with those settings, takes
`plugin_id` as the plugin's id, and remembers the path so
`enable_management()` can serve the config editor from the same file. Keep
your own settings in other tables of the same file and read them with
`read_own_config()`. `run()` blocks.

Constructor arguments and the `HC_BROKER_HOST` / `HC_BROKER_PORT` /
`HC_PLUGIN_PASSWORD` environment variables are for running a plugin by hand
without a config file. They don't read `plugin_id` or the minted password, so
a plugin built that way can't log in to homeCore's broker.

## How a plugin fits into homeCore

Everything travels over MQTT. Your plugin owns its devices' state; homeCore
owns the rules and the UI.

```
your device  ←→  your plugin  ──state──▶  homeCore  ──▶  rules, UI, history
                              ◀──cmd────
```

Four consequences worth internalising:

1. **Publish what happened, not what was asked.** A command is a request. If
   the bulb refuses, publish the state it is actually in. That is why the UI
   can show a light as off after a failed command instead of lying.
2. **Register in `on_connect`.** It fires on every connect, so a reconnect
   re-registers and re-subscribes.
3. **You only see your own devices.** Registering a device subscribes to that
   device's command topic and nothing else.
4. **Use homeCore's names and units.** A shared idea has one name, one unit:
   `on`, `brightness_pct` (0–100), `color_temp` (kelvin), `transition_ms`,
   `open` (`true` = open), `occupied`, `battery_pct`, `humidity`,
   `illuminance` (lux), temperatures in **°C**, and the rest of the table in
   `core/crates/hc-types/src/device_vocabulary.rs`. Convert from the device's
   own scale inside the plugin, both ways; showing °F is the app's job. A
   command arrives as one of two shapes — an attribute write, `{"on": true}`,
   or a declared action, `{"action": "toggle"}` — never `{"command": …}`.
   `{"on": true}` restores the last level. See "Names and units" in
   `core/docs/plugin-development.md`.

## Installing it into homeCore

homeCore owns your plugin's config at `config/plugins/<plugin_id>.toml` and
starts the plugin as `<binary> <config path>`. `from_config()` picks the path
up from `sys.argv[1]`.

Because the config path is the only argument, `binary` has to be something
that runs your plugin by itself. `/usr/bin/python3` won't work: it would try
to run the config file as a Python script. Use a console script installed
into a virtual environment:

```toml
# your plugin's pyproject.toml
[project.scripts]
hc-mylight = "hc_mylight:main"   # main() calls MyLight.from_config().run()
```

```bash
python3 -m venv /opt/hc-mylight
/opt/hc-mylight/bin/pip install .
```

A single file with a `#!/usr/bin/env python3` first line, marked executable
with `chmod +x`, works too.

Declare the plugin in homeCore's `homecore.toml` so it gets supervised:

```toml
[[plugins]]
id      = "plugin.mylight"
binary  = "/opt/hc-mylight/bin/hc-mylight"
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
    )
```

A plugin built with `from_config()` needs no `config_path`: management serves
the file it was started with. Pass `config_path=` only to serve a different
one.

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

## Alerts — what needs attention about a device

Notices are about the plugin; **alerts** are about its devices, and live in
the house's alert list (homeCore 0.1.85). Declare the kinds you can raise on
the manifest, then say which are true of a device now — the whole list each
time; an empty one clears them:

```python
from homecore_plugin_sdk import (
    AlertAction, AlertSeverity, Capabilities, PluginAlert, PluginAlertKind,
)

self.enable_management(capabilities=Capabilities(alert_kinds=[
    PluginAlertKind(
        "firmware_update",
        "Firmware update available",
        severity=AlertSeverity.INFO,
        default_on=False,  # news, not a problem: off until someone turns it on
    ),
]))

self.set_alerts("wled_1", [
    PluginAlert(
        "firmware_update",
        "Firmware 17.0.0 is out",
        key="17.0.0",  # "skip this one" skips this version, not the kind
        action=AlertAction(
            "Update…",
            "install_firmware",  # one of the device's actions
            params={"version": "17.0.0"},
            confirm=True,
        ),
    ),
])

# ...and once it is installed:
self.set_alerts("wled_1", [])
```

**An alert is current state, not an event.** homeCore replaces what it held
with each list, so one you stop sending has cleared. Set it when something
changes, not on a timer.

homeCore decides whether each is shown and where it is sent, never shows a
plugin's alert as critical (a plugin asking for `critical` gets `error`), and
lets automations start on, check and act on them. A kind's `default_on`
defaults to `True`; set it `False` for news that isn't a problem, so it stays
off until someone turns it on. `AlertAction` runs one of the device's actions
by default; `target=AlertActionTarget.PLUGIN` runs one of the plugin's own.
`set_plugin_alerts([...])` does the same for alerts about the plugin itself — a
login that expired, a bridge that needs its button pressed. Alerts are
retained and said again on every reconnect.

## Capability actions — buttons in the UI

Declare an action and it appears as a button on your plugin's page, and becomes
callable from hc-mcp. Neither needs code written for your plugin specifically.

```python
from homecore_plugin_sdk import Action, Capabilities

def on_connect(self):
    self.enable_management(
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

`progress` and `awaiting_user` also take `data=`, a dict a plugin's own screen
reads from the stream (text as it is written, a preview to confirm). The
generic UI ignores it.

### Who called

homeCore passes the verified caller with every action: `{"uid", "username",
"role", "scopes", "kind"}` (`kind` is `user`, `api_key`, `local_admin` or
`whitelist`). It is never mixed into `params`. A streaming handler reads
`ctx.caller`; an immediate one adds a `caller` keyword and the SDK passes it.
Handlers written without it keep working.

```python
def on_action(self, action, params, ctx=None, caller=None):
    if action == "whoami":
        return {"name": (caller or {}).get("username")}
```

Most actions still need an admin (`plugins:write`), whatever role they declare.
An action declared with `requires_role=RequiresRole.SIGNED_IN` can be called by
anyone signed in, household members and read-only accounts included, so use
`caller["scopes"]` to stay within what that person may do.

## REST access from homeCore

A plugin that needs homeCore's REST API asks for scopes in its manifest, and
homeCore mints it a key on every start. Nothing to paste, nothing stored in the
config file.

```python
Capabilities(actions=[...], api_scopes=["devices:read", "devices:command"])
```

The key arrives with the `api_access` management action. The SDK keeps it in
`self.api_access` (`url`, `key`, `scopes`; the key never shows in `repr`) and
calls your hook:

```python
def on_api_access(self, url, key, scopes):
    self.rest = MyClient(url, key)      # url already ends in /api/v1

# or, from a worker thread (never from on_connect):
access = self.wait_for_api_access(timeout=30)
```

homeCore leaves out scopes no plugin may hold (`users:write`,
`api_keys:admin`), so check `scopes` rather than assuming you got everything.

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

## Your own screens

A plugin can carry web components — a page for itself, a panel for its devices
— and serve them itself. Build them into a folder with one ES module and a
`manifest.json` saying where each goes, ship the folder inside your package as
`ui/` (package data), and:

```python
from homecore_plugin_sdk import ui

def on_connect(self):
    d = ui.find_dir(__file__)
    if d:
        self.with_ui_dir(d, __version__)
    self.enable_management(version=__version__, capabilities=caps)
```

`ui.find_dir(__file__)` looks, in order, at `HC_PLUGIN_UI_DIR`; `ui/` inside
the package `__file__` belongs to (how a wheel installs it); `ui/` beside the
program being run (`sys.argv[0]`); and, run from a source checkout,
`<project>/ui/dist`, where `<project>` is the nearest folder above the package
with a `pyproject.toml`. Apart from the environment variable, a folder counts
only if it holds `manifest.json`.

The SDK declares them on the manifest (`ui`, with a hash of the files as their
`build`) and answers homeCore's requests for the files; homeCore passes them to
the web app and keeps none. A plugin with screens and no actions still
publishes its manifest. A folder that isn't there, or declares nowhere the app
can put it, is said in the log and skipped — the app shows its generic screens.
Every slot goes on as declared, extra fields (`label`, `forms`, `settings`, …)
included: which slots exist is homeCore's to decide, so the SDK never keeps a
plugin from offering one newer than itself.
Each element is handed the app's `host` — the plugin's devices, commands,
actions, settings and the app's look; its types are `core/sdk/ui`
(`@homecore/plugin-ui`). `plugins/hc-ecowitt/ui` in core is a complete one.

The `build` is the same in every homeCore SDK for the same folder: the first 8
bytes of SHA-256 over every file, sorted by path, each fed as its path, a 0
byte and its contents.

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
| `with_ui_dir(dir, version)` | Carry and serve the plugin's own screens |
| `enable_log_forwarding(min_level=)` | Send your logs to homeCore's live log stream |
| `publish_plugin_status(status)` | active / degraded / offline |
| `publish_event(type, payload)` | A structured event on the bus |
| `notices` | `.raise_()`, `.clear()`, `.set()`, `.snapshot()` |
| `set_alerts(device_id, alerts)` | The alerts true of a device now; `[]` clears |
| `set_plugin_alerts(alerts)` | The same, about the plugin itself |
| `enable_device_persistence(path)` | Remember registered devices across restarts |
| `reconcile_devices(live)` | Unregister everything not in the live set |
| `stop()` | `on_stop()`, every device offline, the plugin offline, disconnect |
| `api_access` / `wait_for_api_access(timeout)` | REST access homeCore granted, or `None` |

### Hooks to override

| Hook | When |
|---|---|
| `on_connect()` | Connected. Register devices, enable management |
| `on_command(device_id, payload)` | A command for one of your devices |
| `on_action(action, params, ctx=None, caller=None)` | A capability action; `caller` is optional |
| `on_state(device_id, state)` | A device you subscribed to changed |
| `on_set_config(config)` | A structured config write |
| `on_api_access(url, key, scopes)` | homeCore sent REST access (every start) |
| `on_stop()` | Stopping. Leave what you control safe |

**Stopping.** homeCore and a plugin runtime stop a plugin with SIGTERM, and
kill it 2 seconds later. When `run()` is called from the main thread, SIGTERM
and Ctrl-C call `stop()`: your `on_stop()`, then every registered device
offline, the plugin offline, and a disconnect, which ends `run()`. A plugin
run from another thread calls `stop()` itself.

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

Almost everything the Rust SDK does is here: registration, state, availability, the
management protocol, log forwarding, notices, alerts, capability actions including
streaming, cross-device state subscription, and the plugin's own screens.

One thing is Rust-only so far: **files sent to an action** (`POST
/plugins/{id}/upload/{action}`, which the Rust SDK receives as `upload_chunk`
pieces and hands the action as `file_path`).

The whole process of building a plugin — screens and cards included — is in
the homeCore repository's `docs/plugin-development.md`.

## Development

```bash
pip install -e '.[dev]'
pytest -q tests/
```

`examples/virtual_light.py` is a complete plugin. `examples/discovery_plugin.py`
demonstrates notices and both kinds of capability action.

## License

Dual-licensed under **MIT** or **Apache-2.0**, at your option.
