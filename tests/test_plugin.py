"""Tests for homecore_plugin_sdk.PluginBase.

All tests use unittest.mock to avoid a real MQTT broker.
"""
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, call, patch

from homecore_plugin_sdk import (
    Action,
    Capabilities,
    NoticeLevel,
    PluginBase,
    PluginNotice,
    StreamContext,
    StreamTerminated,
)


class ConcretePlugin(PluginBase):
    """Minimal concrete subclass for testing."""

    PLUGIN_ID = "plugin.test"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.commands: list = []
        self.connect_called = False

    def on_connect(self):
        self.connect_called = True

    def on_command(self, device_id: str, payload: dict) -> None:
        self.commands.append((device_id, payload))


def _make_plugin(**kwargs) -> ConcretePlugin:
    return ConcretePlugin(broker_host="127.0.0.1", broker_port=1883, **kwargs)


def _attach_mock_client(plugin: ConcretePlugin) -> MagicMock:
    mock = MagicMock()
    plugin._client = mock
    return mock


def _make_msg(topic: str, payload: bytes) -> MagicMock:
    msg = MagicMock()
    msg.topic = topic
    msg.payload = payload
    return msg


class TestPublishMethods(unittest.TestCase):
    def test_publish_state(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.publish_state("light.01", {"on": True, "brightness": 200})
        mc.publish.assert_called_once_with(
            "homecore/devices/light.01/state",
            json.dumps({"on": True, "brightness": 200}),
            qos=1,
            retain=True,
        )

    def test_publish_state_attaches_change_metadata(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.publish_state(
            "light.01",
            {"on": True},
            change={"kind": "external", "source": "wall_switch"},
        )
        mc.publish.assert_called_once_with(
            "homecore/devices/light.01/state",
            json.dumps(
                {
                    "on": True,
                    "_hc": {"change": {"kind": "external", "source": "wall_switch"}},
                }
            ),
            qos=1,
            retain=True,
        )

    def test_publish_state_partial(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.publish_state_partial("light.01", {"brightness": 128})
        mc.publish.assert_called_once_with(
            "homecore/devices/light.01/state/partial",
            json.dumps({"brightness": 128}),
            qos=1,
            retain=False,
        )

    def test_publish_state_for_command_preserves_command_metadata(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.publish_state_for_command(
            "light.01",
            {"on": True},
            {
                "on": True,
                "_hc": {
                    "command": {
                        "changed_at": "2026-04-01T12:00:00Z",
                        "kind": "homecore",
                        "source": "api",
                        "correlation_id": "corr-1",
                    }
                },
            },
        )
        mc.publish.assert_called_once_with(
            "homecore/devices/light.01/state",
            json.dumps(
                {
                    "on": True,
                    "_hc": {
                        "change": {
                            "changed_at": "2026-04-01T12:00:00Z",
                            "kind": "homecore",
                            "source": "api",
                            "correlation_id": "corr-1",
                        }
                    },
                }
            ),
            qos=1,
            retain=True,
        )

    def test_register_device(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        caps = {"on": {"type": "boolean"}}
        plugin.register_device("light.01", "Test Light", caps, area="living_room")

        mc.publish.assert_called_once()
        topic, payload_str = mc.publish.call_args[0][:2]
        payload = json.loads(payload_str)

        self.assertEqual(topic, "homecore/plugins/plugin.test/register")
        self.assertEqual(payload["device_id"], "light.01")
        self.assertEqual(payload["plugin_id"], "plugin.test")
        self.assertEqual(payload["name"], "Test Light")
        self.assertEqual(payload["area"], "living_room")
        self.assertEqual(payload["capabilities"], caps)

    def test_register_device_no_area(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.register_device("light.01", "Test Light", {})
        _, payload_str = mc.publish.call_args[0][:2]
        self.assertIsNone(json.loads(payload_str)["area"])

    def test_register_device_typed(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.register_device_typed("light.01", "Test Light", "light", area="living_room")

        mc.publish.assert_called_once()
        topic, payload_str = mc.publish.call_args[0][:2]
        payload = json.loads(payload_str)

        self.assertEqual(topic, "homecore/plugins/plugin.test/register")
        self.assertEqual(payload["device_id"], "light.01")
        self.assertEqual(payload["plugin_id"], "plugin.test")
        self.assertEqual(payload["name"], "Test Light")
        self.assertEqual(payload["device_type"], "light")
        self.assertEqual(payload["area"], "living_room")
        self.assertNotIn("capabilities", payload)

    def test_register_device_typed_no_area(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.register_device_typed("sensor.01", "Temp Sensor", "temperature_sensor")
        _, payload_str = mc.publish.call_args[0][:2]
        payload = json.loads(payload_str)
        self.assertIsNone(payload["area"])
        self.assertEqual(payload["device_type"], "temperature_sensor")

    def test_unregister_device(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.unregister_device("sensor.01")

        expected = [
            call(
                "homecore/devices/sensor.01/state",
                "",
                qos=1,
                retain=True,
            ),
            call(
                "homecore/devices/sensor.01/availability",
                "",
                qos=1,
                retain=True,
            ),
            call(
                "homecore/devices/sensor.01/schema",
                "",
                qos=1,
                retain=True,
            ),
            call(
                "homecore/plugins/plugin.test/unregister",
                json.dumps({"device_id": "sensor.01"}),
                qos=1,
                retain=False,
            ),
        ]
        self.assertEqual(mc.publish.call_args_list, expected)

    def test_publish_availability_online(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.publish_availability("light.01", True)
        mc.publish.assert_called_once_with(
            "homecore/devices/light.01/availability",
            "online",
            qos=1,
            retain=True,
        )

    def test_publish_availability_offline(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.publish_availability("light.01", False)
        mc.publish.assert_called_once_with(
            "homecore/devices/light.01/availability",
            "offline",
            qos=1,
            retain=True,
        )

    def test_publish_plugin_status(self):
        plugin = _make_plugin()
        mc = _attach_mock_client(plugin)
        plugin.publish_plugin_status("active")
        mc.publish.assert_called_once_with(
            "homecore/plugins/plugin.test/status",
            "active",
            qos=1,
            retain=True,
        )

    def test_publish_before_connect_logs_warning(self):
        plugin = _make_plugin()
        # _client is None — should warn, not raise
        with self.assertLogs("homecore_plugin_sdk", level="WARNING") as cm:
            plugin.publish_state("light.01", {"on": True})
        self.assertTrue(any("before run()" in line for line in cm.output))


class TestCommandRouting(unittest.TestCase):
    def test_on_command_routing(self):
        plugin = _make_plugin()
        plugin.subscribe_commands("light.01")
        msg = _make_msg("homecore/devices/light.01/cmd", json.dumps({"on": True}).encode())
        plugin._on_message_handler(msg)
        self.assertEqual(plugin.commands, [("light.01", {"on": True})])

    def test_invalid_json_payload(self):
        plugin = _make_plugin()
        plugin.subscribe_commands("light.01")
        msg = _make_msg("homecore/devices/light.01/cmd", b"not-json")
        plugin._on_message_handler(msg)
        self.assertEqual(len(plugin.commands), 1)
        device_id, payload = plugin.commands[0]
        self.assertEqual(device_id, "light.01")
        self.assertIn("raw", payload)

    def test_command_for_another_plugins_device_is_ignored(self):
        """The isolation property, which the SDK used to violate.

        It subscribed to `homecore/devices/+/cmd`, so every plugin in the
        system saw every other plugin's commands and could act on them.
        """
        plugin = _make_plugin()
        plugin.register_device_typed("light.mine", "Mine", "light")
        msg = _make_msg("homecore/devices/light.theirs/cmd", b'{"on": true}')
        plugin._on_message_handler(msg)
        self.assertEqual(plugin.commands, [])

    def test_registering_subscribes_to_that_device_only(self):
        plugin = _make_plugin()
        mock = _attach_mock_client(plugin)
        plugin.register_device_typed("light.01", "One", "light")
        mock.subscribe.assert_called_once_with("homecore/devices/light.01/cmd", qos=1)

    def test_unregister_stops_delivery(self):
        plugin = _make_plugin()
        _attach_mock_client(plugin)
        plugin.register_device_typed("light.01", "One", "light")
        plugin.unregister_device("light.01")
        msg = _make_msg("homecore/devices/light.01/cmd", b'{"on": true}')
        plugin._on_message_handler(msg)
        self.assertEqual(plugin.commands, [])

    def test_non_cmd_topic_ignored(self):
        plugin = _make_plugin()
        msg = _make_msg("homecore/devices/light.01/state", json.dumps({"on": True}).encode())
        plugin._on_message_handler(msg)
        self.assertEqual(plugin.commands, [])

    def test_wrong_prefix_ignored(self):
        plugin = _make_plugin()
        msg = _make_msg("other/devices/light.01/cmd", b"{}")
        plugin._on_message_handler(msg)
        self.assertEqual(plugin.commands, [])


class TestConfig(unittest.TestCase):
    def test_explicit_params(self):
        plugin = _make_plugin(password="secret")
        self.assertEqual(plugin.broker_host, "127.0.0.1")
        self.assertEqual(plugin.broker_port, 1883)
        self.assertEqual(plugin.password, "secret")

    def test_env_var_config(self):
        os.environ["HC_BROKER_HOST"] = "192.168.1.5"
        os.environ["HC_BROKER_PORT"] = "1884"
        os.environ["HC_PLUGIN_PASSWORD"] = "envpass"
        try:
            plugin = ConcretePlugin()
            self.assertEqual(plugin.broker_host, "192.168.1.5")
            self.assertEqual(plugin.broker_port, 1884)
            self.assertEqual(plugin.password, "envpass")
        finally:
            del os.environ["HC_BROKER_HOST"]
            del os.environ["HC_BROKER_PORT"]
            del os.environ["HC_PLUGIN_PASSWORD"]

    def test_explicit_params_override_env(self):
        os.environ["HC_BROKER_HOST"] = "10.0.0.1"
        try:
            plugin = ConcretePlugin(broker_host="192.168.0.1", broker_port=1883)
            self.assertEqual(plugin.broker_host, "192.168.0.1")
        finally:
            del os.environ["HC_BROKER_HOST"]


class TestRunLifecycle(unittest.TestCase):
    """Tests for run()-adjacent logic that don't require paho to be installed."""

    def test_run_raises_if_paho_not_installed(self):
        """run() raises a helpful ImportError when paho-mqtt is absent."""
        plugin = _make_plugin()
        # Ensure paho.mqtt.client is absent from sys.modules so the import fails.
        with patch.dict("sys.modules", {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            with self.assertRaises(ImportError) as ctx:
                plugin.run()
        self.assertIn("paho-mqtt", str(ctx.exception))

    def test_password_stored_on_instance(self):
        plugin = _make_plugin(password="mysecret")
        self.assertEqual(plugin.password, "mysecret")

    def test_no_password_stored_as_empty_string(self):
        plugin = _make_plugin(password="")
        self.assertEqual(plugin.password, "")

    def test_client_is_none_before_run(self):
        plugin = _make_plugin()
        self.assertIsNone(plugin._client)


if __name__ == "__main__":
    unittest.main()


# ──────────────────────────────────────────────────────────────────────────
# Notices
# ──────────────────────────────────────────────────────────────────────────


class TestNotices(unittest.TestCase):
    def test_raise_and_clear(self):
        plugin = _make_plugin()
        plugin.notices.raise_(
            PluginNotice.error("bridge_unreachable", "The bridge stopped answering")
        )
        self.assertIn("bridge_unreachable", plugin.notices)
        plugin.notices.clear("bridge_unreachable")
        self.assertNotIn("bridge_unreachable", plugin.notices)

    def test_clearing_something_not_raised_is_a_no_op(self):
        plugin = _make_plugin()
        plugin.notices.clear("never_raised")  # must not raise

    def test_reraising_a_code_replaces_it(self):
        """Re-deriving conditions on a poll loop is the intended usage."""
        plugin = _make_plugin()
        plugin.notices.raise_(PluginNotice.warning("c", "first"))
        plugin.notices.raise_(PluginNotice.error("c", "second"))
        self.assertEqual(len(plugin.notices), 1)
        (only,) = plugin.notices.snapshot()
        self.assertEqual(only.message, "second")
        self.assertIs(only.level, NoticeLevel.ERROR)

    def test_wire_form_omits_remedy_when_absent(self):
        plugin = _make_plugin()
        plugin.notices.raise_(PluginNotice.info("a", "no remedy"))
        plugin.notices.raise_(PluginNotice.info("b", "has one", remedy="do this"))
        wire = {n["code"]: n for n in plugin.notices.to_wire()}
        self.assertNotIn("remedy", wire["a"])
        self.assertEqual(wire["b"]["remedy"], "do this")
        self.assertEqual(wire["a"]["level"], "info")

    def test_set_replaces_the_whole_set(self):
        plugin = _make_plugin()
        plugin.notices.raise_(PluginNotice.warning("stale", "left over"))
        plugin.notices.set([PluginNotice.info("fresh", "current")])
        self.assertEqual([n.code for n in plugin.notices.snapshot()], ["fresh"])

    def test_heartbeat_carries_the_current_set(self):
        plugin = _make_plugin()
        mock = _attach_mock_client(plugin)
        plugin.register_device_typed("light.01", "One", "light")
        plugin.notices.raise_(
            PluginNotice.warning("no_devices", "none yet", remedy="add one")
        )
        plugin._publish_heartbeat()

        topic, payload = mock.publish.call_args[0][:2]
        self.assertEqual(topic, "homecore/plugins/plugin.test/heartbeat")
        hb = json.loads(payload)
        self.assertEqual(hb["device_count"], 1)
        self.assertEqual(len(hb["notices"]), 1)
        self.assertEqual(hb["notices"][0]["code"], "no_devices")
        self.assertEqual(hb["notices"][0]["remedy"], "add one")
        self.assertIn("protocol_version", hb)
        self.assertIn("sdk_version", hb)

    def test_cleared_notice_leaves_the_next_heartbeat(self):
        """Notices are state, not an event log — a cleared one just stops
        being sent, and core replaces rather than merges."""
        plugin = _make_plugin()
        mock = _attach_mock_client(plugin)
        plugin.notices.raise_(PluginNotice.error("gone", "transient"))
        plugin._publish_heartbeat()
        plugin.notices.clear("gone")
        plugin._publish_heartbeat()
        hb = json.loads(mock.publish.call_args[0][1])
        self.assertEqual(hb["notices"], [])


# ──────────────────────────────────────────────────────────────────────────
# Capability actions
# ──────────────────────────────────────────────────────────────────────────


class ActionPlugin(ConcretePlugin):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.seen: list = []

    def on_action(self, action, params, ctx=None):
        self.seen.append((action, params, ctx))
        if action == "immediate":
            return {"echo": params}
        if action == "boom":
            raise RuntimeError("handler exploded")
        if action == "sweep":
            ctx.progress(percent=50, message="halfway")
            ctx.item_add({"serial": "abc"})
            ctx.complete({"found": 1})
            return None
        if action == "silent":
            return None  # a streaming handler that forgets to terminate
        return None


def _manage_msg(action: str, request_id: str = "r1", **extra) -> MagicMock:
    body = {"action": action, "request_id": request_id, **extra}
    return _make_msg(
        "homecore/plugins/plugin.test/manage/cmd", json.dumps(body).encode()
    )


def _responses(mock) -> list:
    out = []
    for c in mock.publish.call_args_list:
        topic, payload = c[0][:2]
        if topic.endswith("/manage/response"):
            out.append(json.loads(payload))
    return out


def _stream_events(mock) -> list:
    out = []
    for c in mock.publish.call_args_list:
        topic, payload = c[0][:2]
        if "/commands/" in topic and payload:
            out.append(json.loads(payload))
    return out


class TestCapabilityActions(unittest.TestCase):
    def _plugin(self, actions):
        plugin = ActionPlugin(broker_host="127.0.0.1", broker_port=1883)
        mock = _attach_mock_client(plugin)
        plugin.enable_management(
            interval_secs=3600, capabilities=Capabilities(actions=actions)
        )
        return plugin, mock

    def test_manifest_is_published_retained(self):
        plugin, mock = self._plugin(
            [Action(id="immediate", label="Do it", description="desc")]
        )
        published = {
            c[0][0]: c for c in mock.publish.call_args_list
        }
        topic = "homecore/plugins/plugin.test/capabilities"
        self.assertIn(topic, published)
        kwargs = published[topic][1]
        self.assertTrue(kwargs["retain"])
        manifest = json.loads(published[topic][0][1])
        self.assertEqual(manifest["spec"], "1")
        self.assertEqual(manifest["plugin_id"], "plugin.test")
        self.assertEqual(manifest["actions"][0]["id"], "immediate")
        self.assertEqual(manifest["actions"][0]["requires_role"], "user")
        # Absent optionals are omitted, not sent as null — matching Rust.
        self.assertNotIn("params", manifest["actions"][0])

    def test_immediate_action_result_is_returned(self):
        plugin, mock = self._plugin([Action(id="immediate", label="Do it")])
        plugin._on_message_handler(_manage_msg("immediate", value=7))
        resp = _responses(mock)[-1]
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["echo"], {"value": 7})
        # Envelope keys are stripped from params.
        self.assertNotIn("action", resp["echo"])

    def test_unknown_action_is_an_error(self):
        plugin, mock = self._plugin([Action(id="immediate", label="Do it")])
        plugin._on_message_handler(_manage_msg("nope"))
        resp = _responses(mock)[-1]
        self.assertEqual(resp["status"], "error")
        self.assertIn("unknown action", resp["error"])

    def test_handler_exception_becomes_an_error_response(self):
        """A plugin bug must not take down the MQTT loop."""
        plugin, mock = self._plugin([Action(id="boom", label="Boom")])
        with self.assertLogs("homecore_plugin_sdk", level="ERROR"):
            plugin._on_message_handler(_manage_msg("boom"))
        resp = _responses(mock)[-1]
        self.assertEqual(resp["status"], "error")
        self.assertIn("handler exploded", resp["error"])

    def test_builtin_ping_still_works(self):
        plugin, mock = self._plugin([Action(id="immediate", label="Do it")])
        plugin._on_message_handler(_manage_msg("ping"))
        self.assertEqual(_responses(mock)[-1]["status"], "ok")


class TestStreamingActions(unittest.TestCase):
    def _run(self, action_id, **extra):
        plugin = ActionPlugin(broker_host="127.0.0.1", broker_port=1883)
        mock = _attach_mock_client(plugin)
        plugin.enable_management(
            interval_secs=3600,
            capabilities=Capabilities(
                actions=[
                    Action(id="sweep", label="Sweep", stream=True, item_key="serial"),
                    Action(id="silent", label="Silent", stream=True),
                ]
            ),
        )
        plugin._on_message_handler(_manage_msg(action_id, **extra))
        # The handler runs on its own thread; wait for that one only. The
        # heartbeat thread also belongs to this plugin and never exits.
        for t in threading.enumerate():
            if t.name.startswith("hc-action-"):
                t.join(timeout=5)
        return plugin, mock

    def test_accepted_response_carries_the_stream_topic(self):
        plugin, mock = self._run("sweep")
        resp = _responses(mock)[0]
        self.assertEqual(resp["status"], "accepted")
        self.assertEqual(
            resp["stream_topic"],
            "homecore/plugins/plugin.test/commands/r1/events",
        )

    def test_stages_are_emitted_in_order(self):
        plugin, mock = self._run("sweep")
        stages = [e["stage"] for e in _stream_events(mock)]
        self.assertEqual(stages, ["progress", "item", "complete"])

    def test_every_event_carries_request_id_and_timestamp(self):
        plugin, mock = self._run("sweep")
        for ev in _stream_events(mock):
            self.assertEqual(ev["request_id"], "r1")
            self.assertIn("ts", ev)

    def test_stream_topic_is_retained_cleared_at_the_end(self):
        """Otherwise a UI subscribing later replays a stale terminal as live."""
        plugin, mock = self._run("sweep")
        last = [
            c for c in mock.publish.call_args_list if "/commands/" in c[0][0]
        ][-1]
        self.assertEqual(last[0][1], "")
        self.assertTrue(last[1]["retain"])

    def test_handler_that_never_terminates_gets_a_synthetic_error(self):
        plugin, mock = self._run("silent")
        events = _stream_events(mock)
        self.assertEqual(events[-1]["stage"], "error")
        self.assertEqual(events[-1]["data"]["reason"], "plugin_dropped_stream")

    def test_emitting_after_terminal_raises(self):
        plugin = ActionPlugin(broker_host="127.0.0.1", broker_port=1883)
        _attach_mock_client(plugin)
        ctx = StreamContext(plugin, "r9", "sweep")
        ctx.complete({})
        with self.assertRaises(StreamTerminated):
            ctx.progress(message="too late")

    def test_cancel_routes_to_the_live_stream(self):
        plugin = ActionPlugin(broker_host="127.0.0.1", broker_port=1883)
        mock = _attach_mock_client(plugin)
        plugin.enable_management(interval_secs=3600)
        ctx = StreamContext(plugin, "r5", "sweep")
        plugin._active_streams["r5"] = ctx
        plugin._on_message_handler(
            _manage_msg("cancel", request_id="r6", target_request_id="r5")
        )
        self.assertTrue(ctx.is_canceled())
        self.assertEqual(_responses(mock)[-1]["status"], "ok")

    def test_cancel_for_an_unknown_stream_is_an_error(self):
        plugin = ActionPlugin(broker_host="127.0.0.1", broker_port=1883)
        mock = _attach_mock_client(plugin)
        plugin.enable_management(interval_secs=3600)
        plugin._on_message_handler(
            _manage_msg("cancel", request_id="r6", target_request_id="nope")
        )
        self.assertEqual(_responses(mock)[-1]["status"], "error")

    def test_respond_delivers_to_await_respond(self):
        plugin = ActionPlugin(broker_host="127.0.0.1", broker_port=1883)
        _attach_mock_client(plugin)
        plugin.enable_management(interval_secs=3600)
        ctx = StreamContext(plugin, "r7", "pair")
        plugin._active_streams["r7"] = ctx
        plugin._on_message_handler(
            _manage_msg(
                "respond", request_id="r8", target_request_id="r7", response={"pin": "1234"}
            )
        )
        self.assertEqual(ctx.await_respond(timeout=2), {"pin": "1234"})


class TestNoticePublishing(unittest.TestCase):
    """The change callback publishes a heartbeat, which reads the notice set
    back — so it must not hold the lock while calling out."""

    def _plugin(self):
        plugin = _make_plugin()
        mock = _attach_mock_client(plugin)
        plugin._management_enabled = True
        return plugin, mock

    def test_raising_a_notice_does_not_deadlock(self):
        plugin, _ = self._plugin()
        done = threading.Event()

        def go():
            plugin.notices.raise_(PluginNotice.error("x", "y"))
            done.set()

        threading.Thread(target=go, daemon=True).start()
        self.assertTrue(done.wait(5), "raise_ deadlocked against its own callback")

    def test_a_change_publishes_a_heartbeat_immediately(self):
        plugin, mock = self._plugin()
        plugin.notices.raise_(PluginNotice.warning("c", "m"))
        beats = [
            json.loads(c[0][1])
            for c in mock.publish.call_args_list
            if c[0][0].endswith("/heartbeat")
        ]
        self.assertEqual(len(beats), 1)
        self.assertEqual(beats[0]["notices"][0]["code"], "c")

    def test_re_deriving_the_same_set_does_not_republish(self):
        plugin, mock = self._plugin()
        notices = [PluginNotice.warning("c", "m")]
        plugin.notices.set(notices)
        before = mock.publish.call_count
        plugin.notices.set(list(notices))  # same content, new list
        self.assertEqual(mock.publish.call_count, before)

    def test_clearing_something_absent_does_not_republish(self):
        plugin, mock = self._plugin()
        before = mock.publish.call_count
        plugin.notices.clear("never_raised")
        self.assertEqual(mock.publish.call_count, before)


class TestRemoteConfig(unittest.TestCase):
    """The keys core actually uses. Getting these wrong meant the config editor
    showed the response envelope, and saving truncated the file."""

    def _plugin(self, tmp_path):
        plugin = _make_plugin()
        mock = _attach_mock_client(plugin)
        plugin._management_enabled = True
        plugin._config_path = tmp_path
        return plugin, mock

    def _responses(self, mock):
        return [
            json.loads(c[0][1])
            for c in mock.publish.call_args_list
            if c[0][0].endswith("/manage/response")
        ]

    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        self.tmp.write("[demo]\nvalue = 42\n")
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_get_config_answers_with_the_data_key(self):
        plugin, mock = self._plugin(self.tmp.name)
        plugin._on_message_handler(_manage_msg("get_config"))
        resp = self._responses(mock)[-1]
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["data"], "[demo]\nvalue = 42\n")

    def test_set_config_writes_the_string_form(self):
        plugin, mock = self._plugin(self.tmp.name)
        plugin._on_message_handler(_manage_msg("set_config", config="[demo]\nvalue = 99\n"))
        self.assertEqual(self._responses(mock)[-1]["status"], "ok")
        with open(self.tmp.name) as f:
            self.assertEqual(f.read(), "[demo]\nvalue = 99\n")

    def test_set_config_unwraps_the_raw_form_core_sends(self):
        """Core forwards the request body when it has no top-level `config`
        key, so the raw editor arrives as {"raw": "<text>"}."""
        plugin, mock = self._plugin(self.tmp.name)
        plugin._on_message_handler(
            _manage_msg("set_config", config={"raw": "[demo]\nvalue = 7\n"})
        )
        self.assertEqual(self._responses(mock)[-1]["status"], "ok")
        with open(self.tmp.name) as f:
            self.assertEqual(f.read(), "[demo]\nvalue = 7\n")

    def test_structured_config_is_refused_without_truncating(self):
        """It used to read the wrong key, default to "", and wipe the file."""
        plugin, mock = self._plugin(self.tmp.name)
        plugin._on_message_handler(
            _manage_msg("set_config", config={"demo": {"value": 7}})
        )
        self.assertEqual(self._responses(mock)[-1]["status"], "error")
        with open(self.tmp.name) as f:
            self.assertEqual(f.read(), "[demo]\nvalue = 42\n")

    def test_on_set_config_override_takes_over(self):
        plugin, mock = self._plugin(self.tmp.name)
        seen = []
        plugin.on_set_config = lambda cfg: (seen.append(cfg), True)[1]
        plugin._on_message_handler(
            _manage_msg("set_config", config={"demo": {"value": 7}})
        )
        self.assertEqual(self._responses(mock)[-1]["status"], "ok")
        self.assertEqual(seen, [{"demo": {"value": 7}}])
