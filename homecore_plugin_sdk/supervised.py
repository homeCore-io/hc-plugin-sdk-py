"""A plugin homeCore started goes when homeCore does.

Core sets ``HOMECORE_SUPERVISED`` to its own process id on every plugin it
starts. Core stopped the usual way stops its plugins first. Core killed or
crashed leaves them running, and the next core starts a second copy of each
under the same broker id; the two knock each other off the broker every few
seconds for as long as both run. So a plugin whose parent was core, and is no
longer, stops.

A plugin started any other way (by hand, by systemd, in its own container) has
no such variable and is left alone. One started by core through another
process, a launcher script that doesn't ``exec``, has a parent that isn't core,
and is left alone too, rather than stopped by mistake.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

SUPERVISED_ENV = "HOMECORE_SUPERVISED"
EVERY_S = 2.0

logger = logging.getLogger(__name__)
_started = False
_lock = threading.Lock()


def decide(var: str | None, parent: int) -> int | None:
    """Core's id when core is the parent and so worth watching, else None."""
    try:
        core = int((var or "").strip())
    except ValueError:
        return None
    return core if core == parent else None


def watch(plugin_id: str) -> None:
    """Start watching, once per process, if core is the parent."""
    global _started
    if os.name != "posix":
        return
    with _lock:
        if _started:
            return
        _started = True
    var = os.environ.get(SUPERVISED_ENV)
    core = decide(var, os.getppid())
    if core is None:
        if var is not None:
            logger.info(
                "%s: started by homeCore through another process, so not "
                "watching for homeCore stopping",
                plugin_id,
            )
        return

    def loop() -> None:
        while True:
            time.sleep(EVERY_S)
            if os.getppid() != core:
                msg = (
                    f"{plugin_id}: homeCore (pid {core}), which started this plugin, "
                    "has stopped; stopping too, so the next homeCore's copy runs alone"
                )
                # Its output went to core, which is gone: writing may fail, and
                # mustn't keep the plugin from stopping.
                try:
                    logger.warning(msg)
                    print(msg, file=sys.stderr, flush=True)
                except Exception:
                    pass
                os._exit(0)

    threading.Thread(target=loop, name="homecore-watch", daemon=True).start()
