"""Exercise the detached macOS console without starting the real voice app."""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS pseudo-terminal launcher")
def test_console_service_owns_terminal_and_stops_child(tmp_path: pathlib.Path) -> None:
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'printf "args=%s\\n" "$*"\n'
        "exec python3 -u -c 'import os,sys,time; "
        'print(f"child={os.getpid()} tty={sys.stdin.isatty()}", flush=True); '
        "time.sleep(60)'\n"
    )
    uv.chmod(0o755)
    log = tmp_path / "console.log"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "BRODI_OUTPUT_DEVICE": "MacBook Air Speakers"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "brodi.console_service", str(log)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            content = log.read_text() if log.exists() else ""
            if "tty=True" in content:
                child_pid = int(content.split("child=")[1].split()[0])
                break
            time.sleep(0.05)
        assert child_pid is not None, f"child did not acquire a terminal: {content}"
        assert "--output-device MacBook Air Speakers" in content
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, signal.SIGCONT)
