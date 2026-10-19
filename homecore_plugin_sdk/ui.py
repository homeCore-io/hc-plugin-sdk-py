"""A plugin's own screens, carried and served by the plugin.

The UI is a built folder — web components in one ES module, plus a
``manifest.json`` the build writes:

.. code-block:: json

    { "host_api": 1, "entry": "index.js",
      "slots": [{ "slot": "plugin.page", "element": "hc-x-page" }] }

It travels in whatever package installs the plugin, as ``ui/`` inside the
plugin's package. :meth:`PluginBase.with_ui_dir
<homecore_plugin_sdk.PluginBase.with_ui_dir>` reads it, declares it on the
manifest with a hash of its files as its ``build``, and answers homeCore's
``ui_file`` requests from it — homeCore passes the files through to the app and
keeps none.

This is the Python twin of ``plugin_sdk_rs::ui`` and must agree with it: the
same folder gives the same ``build`` in every SDK.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field

#: The version of the app's ``host`` interface a UI is built against.
HOST_API = 1

#: The slots the app knows.
SLOTS = ("plugin.page", "device.panel")


class UiError(Exception):
    """Why a folder offers no UI, or why a file can't be served."""


@dataclass
class UiSlot:
    """One place in the app a plugin's component fills.

    ``slot`` is ``plugin.page`` (its page, in place of the generic action list)
    or ``device.panel`` (its devices' panel, in place of the generic controls);
    ``element`` the custom element the module defines for it. For
    ``device.panel``, ``device_types`` limits it to devices of those types —
    every one of the plugin's devices when empty.
    """

    slot: str
    element: str
    device_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {"slot": self.slot, "element": self.element}
        if self.device_types:
            out["device_types"] = list(self.device_types)
        return out


@dataclass
class PluginUi:
    """What a plugin's UI is and where it goes — the manifest's ``ui``."""

    host_api: int
    version: str
    build: str = ""
    entry: str = "index.js"
    slots: list[UiSlot] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {"host_api": self.host_api, "version": self.version}
        if self.build:
            out["build"] = self.build
        out["entry"] = self.entry
        out["slots"] = [s.to_dict() for s in self.slots]
        return out

    def validated(self) -> "PluginUi | None":
        """Whether it can be offered at all: a version, a module inside the
        folder, and at least one slot the app knows, each naming a valid
        custom element. Slots that don't pass are dropped; ``None`` when none
        is left."""
        if (
            not self.version.strip()
            or not relative_path(self.entry)
            or not all(_ascii_alnum(c) for c in self.build)
        ):
            return None
        slots = [s for s in self.slots if s.slot in SLOTS and _custom_element(s.element)]
        if not slots:
            return None
        return PluginUi(self.host_api, self.version, self.build, self.entry, slots)


def _ascii_alnum(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9")


def _custom_element(name: str) -> bool:
    """A custom element name: lowercase, starting with a letter, with a hyphen."""
    return (
        "-" in name
        and "a" <= name[:1] <= "z"
        and all(("a" <= c <= "z") or ("0" <= c <= "9") or c in "-._" for c in name)
    )


def relative_path(p: str) -> bool:
    """A path that stays inside the folder."""
    return (
        p != ""
        and not p.startswith("/")
        and "\\" not in p
        and all(seg != "" and seg != ".." for seg in p.split("/"))
    )


def _has_manifest(d: str) -> bool:
    return os.path.isfile(os.path.join(d, "manifest.json"))


def _project_root(start: str) -> str:
    """The nearest folder at or above *start* holding ``pyproject.toml`` or
    ``setup.py`` — a source checkout's root; *start*'s parent when none."""
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, "pyproject.toml")) or os.path.isfile(
            os.path.join(d, "setup.py")
        ):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.dirname(os.path.abspath(start))
        d = parent


def find_dir(package: str) -> str | None:
    """Where a plugin's UI folder is.

    Pass ``__file__`` of a module in your plugin's package (or the package
    folder itself). In order:

    1. ``HC_PLUGIN_UI_DIR`` when set — taken as given;
    2. ``ui/`` inside your package — how a wheel installs it (ship it as
       package data);
    3. ``ui/`` beside the program being run (``sys.argv[0]``) — a script or
       zipapp with its folder next to it;
    4. for a plugin run from its source tree, ``<project>/ui/dist``, where
       ``<project>`` is the nearest folder above your package with a
       ``pyproject.toml`` (or ``setup.py``).

    Apart from the first, a folder counts only if it holds ``manifest.json``.
    ``None`` when there is none.
    """
    env = os.environ.get("HC_PLUGIN_UI_DIR")
    if env:
        return env
    pkg = package if os.path.isdir(package) else os.path.dirname(os.path.abspath(package))
    candidates = [os.path.join(pkg, "ui")]
    if sys.argv and sys.argv[0]:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "ui"))
    candidates.append(os.path.join(_project_root(pkg), "ui", "dist"))
    for d in candidates:
        if _has_manifest(d):
            return d
    return None


def _is_u32(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 0xFFFFFFFF


def _slot_from(v: object) -> UiSlot:
    if not isinstance(v, dict):
        raise UiError("manifest.json: a slot is not an object")
    slot, element = v.get("slot"), v.get("element")
    types = v.get("device_types", [])
    if not isinstance(slot, str) or not isinstance(element, str):
        raise UiError("manifest.json: a slot needs `slot` and `element` strings")
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        raise UiError("manifest.json: `device_types` must be a list of strings")
    return UiSlot(slot, element, list(types))


def load(directory: str, version: str) -> PluginUi:
    """The UI in *directory*, as its manifest declares it.

    :raises UiError: with the reason, when there is none to offer.
    """
    path = os.path.join(directory, "manifest.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise UiError(f"{path}: {exc}") from None
    try:
        m = json.loads(text)
    except ValueError as exc:
        raise UiError(f"manifest.json: {exc}") from None
    if not isinstance(m, dict) or not _is_u32(m.get("host_api")):
        raise UiError("manifest.json: `host_api` must be a whole number")
    entry = m.get("entry", "index.js")
    if not isinstance(entry, str):
        raise UiError("manifest.json: `entry` must be a string")
    slots_raw = m.get("slots", [])
    if not isinstance(slots_raw, list):
        raise UiError("manifest.json: `slots` must be a list")
    slots = [_slot_from(s) for s in slots_raw]
    if not os.path.isfile(os.path.join(directory, entry)):
        raise UiError(f"its entry {entry} isn't there")
    ui = PluginUi(
        host_api=m["host_api"],
        version=version,
        build=build_of(directory),
        entry=entry,
        slots=slots,
    ).validated()
    if ui is None:
        raise UiError("it declares nowhere the app can put it")
    return ui


def _walk(root: str, d: str, out: list[str]) -> None:
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if os.path.isdir(p):
            _walk(root, p, out)
        else:
            out.append(os.path.relpath(p, root).replace("\\", "/"))


def build_of(directory: str) -> str:
    """A hash of every file's name and contents: what changes when the UI does.

    SHA-256 over the files sorted by relative path (forward slashes), each fed
    as its path, a 0 byte, then its contents; the first 8 bytes, in lowercase
    hex. Identical in every homeCore SDK.
    """
    files: list[str] = []
    try:
        _walk(directory, directory, files)
    except OSError as exc:
        raise UiError(str(exc)) from None
    # Rust sorts by UTF-8 bytes; so must we, to agree on non-ASCII names.
    files.sort(key=lambda f: f.encode("utf-8"))
    h = hashlib.sha256()
    for f in files:
        h.update(f.encode("utf-8"))
        h.update(b"\x00")
        try:
            with open(os.path.join(directory, f), "rb") as fh:
                h.update(fh.read())
        except OSError as exc:
            raise UiError(str(exc)) from None
    return h.digest()[:8].hex()


def piece(directory: str, file: str, offset: int, max_bytes: int) -> tuple[bytes, bool]:
    """Up to *max_bytes* of *file* from *offset*, and whether that is the end.

    :raises UiError: ``no such file`` for a path outside the folder or one
        that isn't there.
    """
    if not relative_path(file):
        raise UiError("no such file")
    path = os.path.join(directory, file)
    if not os.path.isfile(path):
        raise UiError("no such file")
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(min(offset, size))
            data = f.read(max_bytes)
    except FileNotFoundError:
        raise UiError("no such file") from None
    except OSError as exc:
        raise UiError(str(exc)) from None
    return data, offset + max_bytes >= size
