"""Streaming actions — long-running work that reports as it goes.

An immediate action returns a dict and is done. A streaming action gets a
:class:`StreamContext` and publishes events while it works, which is what lets
hc-web show a live progress bar, a list of devices appearing one by one, and a
prompt like "press the button on the device now".

Events go to ``homecore/plugins/{plugin_id}/commands/{request_id}/events``.
There are six stages:

===============  ==========================================================
``progress``     percent / label / message. Emit as often as is useful.
``item``         one thing found or changed, with ``op`` add/update/remove.
``warning``      something recoverable. **Non-terminal** — the stream lives.
``awaiting_user``a prompt; pair with :meth:`StreamContext.await_respond`.
``complete``     terminal, success, carries the result.
``error``        terminal, failure.
===============  ==========================================================

Plus ``canceled``, which you emit yourself after noticing
:meth:`StreamContext.is_canceled` — the SDK does not emit it for you, because
only your code knows what needs rolling back first.

**Terminal stages are latched.** The first one wins; a second is refused. If
your handler returns or raises without emitting one, the SDK synthesises an
``error`` so the UI is never left waiting on a stream that quietly stopped.

.. code-block:: python

    def on_action(self, action, params, ctx):
        if action == "discover":
            found = 0
            for i, host in enumerate(self.candidates()):
                if ctx.is_canceled():
                    ctx.canceled()
                    return
                ctx.progress(percent=int(100 * i / total), message=f"Probing {host}")
                if (dev := probe(host)) is not None:
                    found += 1
                    ctx.item_add({"serial": dev.serial, "name": dev.name})
            ctx.complete({"found": found})
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StreamTerminated(RuntimeError):
    """Raised when emitting after a terminal stage has already been sent."""


class StreamContext:
    """Handle passed to a streaming action handler.

    One per invocation. Thread-safe, so a handler may fan work out to threads
    and have each report progress.
    """

    def __init__(self, plugin: Any, request_id: str, action_id: str) -> None:
        self._plugin = plugin
        self.request_id = request_id
        self.action_id = action_id
        self.topic = (
            f"homecore/plugins/{plugin.PLUGIN_ID}/commands/{request_id}/events"
        )
        self._terminal = threading.Event()
        self._canceled = threading.Event()
        self._responses: queue.Queue = queue.Queue()

    # ── non-terminal stages ───────────────────────────────────────────────

    def progress(
        self,
        *,
        percent: int | None = None,
        label: str | None = None,
        message: str | None = None,
    ) -> None:
        """Report progress. Every field is optional — send whichever you have."""
        ev: dict = {"stage": "progress"}
        if percent is not None:
            ev["percent"] = int(percent)
        if label is not None:
            ev["label"] = label
        if message is not None:
            ev["message"] = message
        self._emit(ev, terminal=False)

    def item_add(self, data: dict) -> None:
        """One thing was found. Include the manifest's ``item_key`` field so
        the UI can tell rows apart."""
        self._emit({"stage": "item", "op": "add", "data": data}, terminal=False)

    def item_update(self, data: dict) -> None:
        """Something already reported has changed — same ``item_key``, so the
        UI updates that row instead of appending another."""
        self._emit({"stage": "item", "op": "update", "data": data}, terminal=False)

    def item_remove(self, data: dict) -> None:
        self._emit({"stage": "item", "op": "remove", "data": data}, terminal=False)

    def warning(self, message: str, data: dict | None = None) -> None:
        """A recoverable problem. The stream continues.

        Use this for a retry or a host that did not answer. If the action
        cannot continue, that is :meth:`error`, which is terminal.
        """
        ev: dict = {"stage": "warning", "message": message}
        if data is not None:
            ev["data"] = data
        self._emit(ev, terminal=False)

    def awaiting_user(self, prompt: str, response_schema: dict | None = None) -> None:
        """Ask the operator for something and keep the stream open.

        Emit this, then block on :meth:`await_respond`. Z-Wave inclusion is the
        motivating case: the plugin cannot proceed until somebody physically
        presses a button.
        """
        ev: dict = {"stage": "awaiting_user", "prompt": prompt}
        if response_schema is not None:
            ev["response_schema"] = response_schema
        self._emit(ev, terminal=False)

    # ── terminal stages ───────────────────────────────────────────────────

    def complete(self, data: dict | None = None) -> None:
        """Terminal, success. ``data`` should match the manifest's ``result``."""
        self._emit({"stage": "complete", "data": data or {}}, terminal=True)

    def error(self, message: str) -> None:
        """Terminal, failure. For something recoverable use :meth:`warning`."""
        self._emit({"stage": "error", "message": message}, terminal=True)

    def canceled(self) -> None:
        """Terminal, acknowledging a cancel.

        Call it yourself once :meth:`is_canceled` is true and you have unwound
        whatever needed unwinding. The SDK will not emit it for you, because it
        does not know when your rollback is finished.
        """
        self._emit({"stage": "canceled"}, terminal=True)

    # ── cancel / respond ──────────────────────────────────────────────────

    def is_canceled(self) -> bool:
        """Whether a cancel has arrived.

        Cooperative — poll it in your loop. Nothing interrupts your handler.
        """
        return self._canceled.is_set()

    def await_respond(self, timeout: float | None = None) -> dict:
        """Block until the operator answers an :meth:`awaiting_user` prompt.

        :raises TimeoutError: if *timeout* elapses first.
        """
        try:
            return self._responses.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(
                f"no response to awaiting_user within {timeout}s"
            ) from exc

    # ── internals, driven by PluginBase ───────────────────────────────────

    def _cancel(self) -> None:
        self._canceled.set()

    def _deliver_response(self, response: dict) -> None:
        self._responses.put(response)

    def _emit(self, ev: dict, *, terminal: bool) -> None:
        if self._terminal.is_set():
            raise StreamTerminated(
                f"stream {self.request_id} already terminated; "
                f"cannot emit {ev.get('stage')!r}"
            )
        if terminal:
            self._terminal.set()
        ev["request_id"] = self.request_id
        ev["ts"] = _iso_now()
        # Retained, so a UI that subscribes mid-action still sees the latest
        # frame rather than an empty screen until the next one.
        self._plugin._publish(self.topic, json.dumps(ev), qos=1, retain=True)

    def _finalize(self, error: BaseException | None) -> None:
        """Guarantee exactly one terminal stage, then clear the retained topic.

        A handler that returns without terminating, or raises, would otherwise
        leave the UI waiting forever on a stream that has already stopped.
        """
        if not self._terminal.is_set():
            self._terminal.set()
            message = (
                f"plugin action failed: {error}"
                if error is not None
                else "plugin dropped stream without emitting a terminal stage"
            )
            self._plugin._publish(
                self.topic,
                json.dumps(
                    {
                        "stage": "error",
                        "request_id": self.request_id,
                        "ts": _iso_now(),
                        "message": message,
                        "data": {"reason": "plugin_dropped_stream"},
                    }
                ),
                qos=1,
                retain=True,
            )
        # An empty retained payload deletes the retained frame, so a subscriber
        # arriving later does not replay a stale terminal as if it were live.
        self._plugin._publish(self.topic, "", qos=1, retain=True)
