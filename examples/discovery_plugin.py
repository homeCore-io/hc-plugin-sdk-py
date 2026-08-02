#!/usr/bin/env python3
"""discovery_plugin.py — notices and capability actions, end to end.

A plugin for an imaginary hub. It has nothing to control until the hub is
discovered, which is the situation notices exist for: without one it would sit
there looking healthy and doing nothing.

Run it against a homeCore::

    pip install homecore-plugin-sdk
    python discovery_plugin.py
    HC_BROKER_HOST=10.0.0.5 python discovery_plugin.py

Then, in the web UI, open Plugins → Discovery Demo and you will see:

* a **warning notice** saying no hub is configured, with a remedy,
* a **Discover hubs** button that streams progress and results,
* a **Ping hub** button that answers immediately.

Press Discover and the notice clears itself, because the condition it reports
stopped being true — that is the whole model.
"""

import logging
import time

from homecore_plugin_sdk import (
    Action,
    Capabilities,
    PluginBase,
    PluginNotice,
    RequiresRole,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger("discovery_plugin")

# Stand-ins for a real network sweep.
CANDIDATE_HOSTS = [f"10.0.0.{n}" for n in range(10, 16)]
HUBS_THAT_ANSWER = {"10.0.0.12": "HUB-A1B2"}


class DiscoveryPlugin(PluginBase):
    PLUGIN_ID = "plugin.discovery_demo"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.hub_host: str | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def on_connect(self) -> None:
        self.enable_management(
            interval_secs=30,
            version="1.0.0",
            capabilities=Capabilities(
                actions=[
                    Action(
                        id="discover_hubs",
                        label="Discover hubs",
                        description=(
                            "Probe the local subnet for hubs and register what "
                            "answers."
                        ),
                        stream=True,
                        cancelable=True,
                        item_key="serial",
                        # Above the realistic worst case: core's default window
                        # is short, and a sweep that gets cut off looks like a
                        # broken plugin rather than a slow network.
                        timeout_ms=30_000,
                    ),
                    Action(
                        id="ping_hub",
                        label="Ping hub",
                        description="Check the configured hub is still answering.",
                        result={"reachable": {"type": "boolean"}},
                    ),
                    Action(
                        id="forget_hub",
                        label="Forget hub",
                        description="Unregister the hub and its devices.",
                        # Destructive, so an operator account should not be able
                        # to press it by accident.
                        requires_role=RequiresRole.ADMIN,
                    ),
                ]
            ),
        )
        self._refresh_notices()

    def on_command(self, device_id: str, payload: dict) -> None:
        logger.info("command for %s: %s", device_id, payload)
        state = {k: v for k, v in payload.items() if not k.startswith("_")}
        self.publish_state_for_command(
            device_id, state, payload, fallback_source="discovery_demo"
        )

    # ── actions ───────────────────────────────────────────────────────────

    def on_action(self, action, params, ctx=None):
        if action == "discover_hubs":
            self._discover(ctx)
            return None
        if action == "ping_hub":
            return {"reachable": self.hub_host in HUBS_THAT_ANSWER}
        if action == "forget_hub":
            if self.hub_host is None:
                return {"status": "nothing to forget"}
            self.unregister_device(f"hub_{HUBS_THAT_ANSWER[self.hub_host]}")
            self.hub_host = None
            self._refresh_notices()
            return {"status": "forgotten"}
        return None  # not ours — the SDK answers "unknown action"

    def _discover(self, ctx) -> None:
        """A streaming action: report as it goes, and stay cancelable."""
        found = 0
        for i, host in enumerate(CANDIDATE_HOSTS):
            # Cancellation is cooperative — nothing interrupts this loop, so it
            # has to be checked. Emitting `canceled` is also ours to do, because
            # only we know when any rollback is finished.
            if ctx.is_canceled():
                ctx.canceled()
                return

            ctx.progress(
                percent=100 * i // len(CANDIDATE_HOSTS),
                message=f"Probing {host}",
            )
            time.sleep(0.3)  # a real probe would be a socket timeout

            serial = HUBS_THAT_ANSWER.get(host)
            if serial is None:
                continue

            found += 1
            self.hub_host = host
            device_id = f"hub_{serial}"
            self.register_device_full(device_id, f"Hub {serial}", device_type="switch")
            self.publish_availability(device_id, True)
            self.publish_state(device_id, {"on": False})
            # `serial` is the manifest's item_key, so the UI keys the row on it
            # and an update lands on the same row rather than appending.
            ctx.item_add({"serial": serial, "host": host, "name": f"Hub {serial}"})

        if found == 0:
            # Non-terminal: the sweep finished, it just found nothing. An error
            # would be wrong — nothing failed.
            ctx.warning("No hubs answered on this subnet.")

        self._refresh_notices()
        ctx.complete({"found": found})

    # ── notices ───────────────────────────────────────────────────────────

    def _refresh_notices(self) -> None:
        """Re-derive every condition from current state.

        Called after connect and after each discovery sweep. Deriving the whole
        set and calling `set` cannot leave a stale notice behind, which is the
        failure mode of scattered raise/clear pairs.
        """
        notices = []
        if self.hub_host is None:
            notices.append(
                PluginNotice.warning(
                    "no_hub_configured",
                    "No hub has been found, so this plugin publishes nothing.",
                    remedy="Run the Discover hubs action.",
                )
            )
        elif self.hub_host not in HUBS_THAT_ANSWER:
            notices.append(
                PluginNotice.error(
                    "hub_unreachable",
                    f"The hub at {self.hub_host} stopped answering.",
                    remedy="Check that it is powered on and on this network.",
                )
            )
        self.notices.set(notices)


if __name__ == "__main__":
    DiscoveryPlugin().run()
