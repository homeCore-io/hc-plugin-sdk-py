"""Capability actions — plugin-specific commands the UI renders as buttons.

A device command tells one device to do something. A *capability action* is a
command aimed at the plugin itself: "Pair the bridge", "Rescan the network",
"Forget devices that no longer answer". Declaring one in a manifest is all it
takes for it to appear as a button on the plugin's page in hc-web and to become
callable from hc-mcp — neither of them needs code for your plugin specifically.

Two kinds:

**Immediate** (``stream=False``) — your handler returns a dict and that is the
result. Good for anything that finishes in a moment.

**Streaming** (``stream=True``) — your handler gets a
:class:`~homecore_plugin_sdk.streaming.StreamContext` and reports progress,
items found, warnings, and a terminal result as it goes. Good for a network
sweep, a pairing flow that has to say "press the button on the device now", or
anything long enough that a spinner would be a lie.

.. code-block:: python

    from homecore_plugin_sdk import Action, Capabilities

    def capabilities(self):
        return Capabilities(actions=[
            Action(
                id="discover",
                label="Discover devices",
                description="Sweep the local network and register what answers.",
                stream=True,
                cancelable=True,
                item_key="serial",
                timeout_ms=30_000,
            ),
        ])

The manifest is published retained to ``homecore/plugins/{id}/capabilities`` on
every connect, so a homeCore that starts later still sees it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Concurrency(str, Enum):
    """Whether a second invocation may start while the first is running."""

    #: May run concurrently with itself.
    MULTI = "multi"
    #: A second invocation is rejected with ``busy`` and the active request id.
    SINGLE = "single"


class RequiresRole(str, Enum):
    """The least-privileged role allowed to invoke the action.

    homeCore enforces this; it is not a UI hint. Use ``ADMIN`` for anything
    destructive — unregistering devices, clearing pairings, factory resets.
    """

    ADMIN = "admin"
    USER = "user"
    READ_ONLY = "read_only"


class ItemOp(str, Enum):
    """Item operations a streaming action may emit, if it emits items at all."""

    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


@dataclass
class Action:
    """One declared action.

    :param id: Stable identifier. This is what arrives as ``action`` in the
        management command, and what your handler dispatches on.
    :param label: What the button says.
    :param description: Shown next to the button. Say what it will do, and to
        what — an operator is deciding whether to press it.
    :param params: JSON Schema for the parameters. ``None`` means the action
        takes none, and the UI renders a plain button rather than a form.
    :param result: JSON Schema of the result, for display.
    :param stream: ``True`` if the handler takes a ``StreamContext``.
    :param cancelable: ``True`` if the stream honours a cancel. Only meaningful
        with ``stream=True``, and only claim it if you actually check.
    :param concurrency: See :class:`Concurrency`.
    :param item_key: For a streaming action that emits items, the field in each
        item that identifies it — so the UI updates a row rather than appending
        a duplicate.
    :param item_operations: Which of add/update/remove this action emits.
    :param requires_role: See :class:`RequiresRole`.
    :param timeout_ms: How long homeCore should wait before giving up. Set it
        above the action's realistic worst case; the default window is short.
    """

    id: str
    label: str
    description: str | None = None
    params: dict | None = None
    result: dict | None = None
    stream: bool = False
    cancelable: bool = False
    concurrency: Concurrency = Concurrency.MULTI
    item_key: str | None = None
    item_operations: list[ItemOp] | None = None
    requires_role: RequiresRole = RequiresRole.USER
    timeout_ms: int | None = None

    def to_dict(self) -> dict:
        out: dict = {
            "id": self.id,
            "label": self.label,
            "stream": self.stream,
            "cancelable": self.cancelable,
            "concurrency": self.concurrency.value,
            "requires_role": self.requires_role.value,
        }
        # Optional fields are omitted rather than sent as null, matching the
        # Rust SDK's `skip_serializing_if` so both produce the same manifest.
        if self.description is not None:
            out["description"] = self.description
        if self.params is not None:
            out["params"] = self.params
        if self.result is not None:
            out["result"] = self.result
        if self.item_key is not None:
            out["item_key"] = self.item_key
        if self.item_operations is not None:
            out["item_operations"] = [op.value for op in self.item_operations]
        if self.timeout_ms is not None:
            out["timeout_ms"] = self.timeout_ms
        return out


@dataclass
class Capabilities:
    """The manifest: everything this plugin declares about itself.

    ``plugin_id`` is filled in by the SDK, so leave it alone — it has to match
    the MQTT client id and there is no reason to say it twice.
    """

    actions: list[Action] = field(default_factory=list)
    #: JSON Schema for the plugin's own config file. When present, hc-web
    #: renders a typed settings form instead of a raw TOML box.
    config_schema: dict | None = None
    #: A plugin-authored field descriptor. Takes precedence over
    #: ``config_schema`` for rendering, when you want to control grouping,
    #: labels, and help text rather than let a schema be guessed at.
    config_descriptor: dict | None = None
    spec: str = "1"
    plugin_id: str = ""

    def to_dict(self) -> dict:
        out: dict = {
            "spec": self.spec,
            "plugin_id": self.plugin_id,
            "actions": [a.to_dict() for a in self.actions],
        }
        # These ride on the manifest rather than a topic of their own; core
        # extracts them from this payload.
        if self.config_schema is not None:
            out["config_schema"] = self.config_schema
        if self.config_descriptor is not None:
            out["config_descriptor"] = self.config_descriptor
        return out
