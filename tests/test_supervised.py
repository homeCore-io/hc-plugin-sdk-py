"""A plugin homeCore started goes when homeCore does."""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from homecore_plugin_sdk.supervised import decide


def test_watches_only_when_core_is_the_parent():
    assert decide("4242", 4242) == 4242
    # Started through a launcher that stayed: its parent isn't core.
    assert decide("4242", 5000) is None
    # Not started by core at all.
    assert decide(None, 4242) is None
    assert decide("", 4242) is None
    assert decide("core", 4242) is None


@pytest.mark.skipif(os.name != "posix", reason="parents are only re-assigned on posix")
def test_a_plugin_stops_when_the_core_that_started_it_is_killed(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pid_file = tmp_path / "plugin.pid"
    # The plugin: watches, writes its pid, then waits as a plugin would.
    plugin = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {root!r})
        from homecore_plugin_sdk import supervised
        supervised.watch("plugin.test")
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        time.sleep(60)
        """
    )
    # Core: starts the plugin with its own id set, then waits to be killed.
    core = textwrap.dedent(
        f"""
        import os, subprocess, sys, time
        env = dict(os.environ, HOMECORE_SUPERVISED=str(os.getpid()))
        subprocess.Popen([sys.executable, "-c", {plugin!r}], env=env)
        time.sleep(60)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", core])
    try:
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.05)
        child = int(pid_file.read_text())
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(child, signal.SIGKILL)
        pytest.fail("the plugin kept running after the core that started it was killed")
    finally:
        if proc.poll() is None:
            proc.kill()
