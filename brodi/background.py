"""Per-user Hey bro service: login startup, one microphone owner, and crash recovery.

Recognition stays in the existing cloud pipeline. No UI needs to be open, but this
supervisor and its voice child must be running while the computer is awake.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psutil

from .config import flag, load_environment
from .paths import user_data_dir

ROOT = Path(__file__).resolve().parent.parent
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
log = logging.getLogger("brodi.background")


def identity(root: Path = ROOT) -> str:
    return "com.brodi.wake." + hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:12]


def service_dir(root: Path = ROOT) -> Path:
    return user_data_dir() / identity(root)


def service_command(root: Path = ROOT, *, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    relative = "Scripts/pythonw.exe" if platform == "win32" else "bin/python"
    python = root / ".venv" / relative
    if not python.is_file():
        raise RuntimeError("Run uv sync in the Brodi folder before enabling Hey bro startup.")
    return [str(python.absolute()), str((root / "background_service.py").resolve()), "run"]


def launch_agent_path(root: Path = ROOT) -> Path:
    return Path.home() / "Library/LaunchAgents" / (identity(root) + ".plist")


def launch_agent_config(root: Path = ROOT) -> dict:
    return {
        "Label": identity(root),
        "ProgramArguments": service_command(root, platform="darwin"),
        "WorkingDirectory": str(root.resolve()),
        "RunAtLoad": True,
        # The supervisor recovers the voice child. Explicit Stop must stay stopped.
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(service_dir(root) / "service.log"),
        "StandardErrorPath": str(service_dir(root) / "service.log"),
    }


def installed(root: Path = ROOT) -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                command, _ = winreg.QueryValueEx(key, identity(root))
            return command == subprocess.list2cmdline(service_command(root))
        except (OSError, RuntimeError):
            return False
    if sys.platform == "darwin":
        try:
            config = plistlib.loads(launch_agent_path(root).read_bytes())
            return config["ProgramArguments"] == service_command(root)
        except (OSError, ValueError, KeyError, RuntimeError):
            return False
    return False


def install(root: Path = ROOT) -> None:
    service_dir(root).mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import winreg

        command = subprocess.list2cmdline(service_command(root))
        if len(command) > 260:
            raise RuntimeError("The startup command exceeds Windows' 260-character limit; use a shorter checkout path.")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, identity(root), 0, winreg.REG_SZ, command)
    elif sys.platform == "darwin":
        path = launch_agent_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(launch_agent_config(root)))
        path.chmod(0o600)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{identity(root)}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True)
    else:
        raise RuntimeError("Hey bro login startup supports Windows and macOS.")


def uninstall(root: Path = ROOT) -> None:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, identity(root))
        except FileNotFoundError:
            pass
    elif sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{identity(root)}"], capture_output=True)
        launch_agent_path(root).unlink(missing_ok=True)


def running_process(root: Path = ROOT):
    """Never trust a stale PID or signal a process belonging to another checkout."""
    try:
        state = json.loads((service_dir(root) / "process.json").read_text(encoding="utf-8"))
        process = psutil.Process(state["pid"])
        expected = str((root / "background_service.py").resolve())
        if (
            process.create_time() == state["created"]
            and expected in process.cmdline()
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        ):
            return process
    except (OSError, ValueError, KeyError, psutil.Error):
        pass
    return None


def request_stop(root: Path = ROOT) -> None:
    data = service_dir(root)
    data.mkdir(parents=True, exist_ok=True)
    (data / "stop").touch()


def stop_service(root: Path = ROOT) -> None:
    request_stop(root)
    process = running_process(root)
    if process is None:
        return
    try:
        process.wait(timeout=8)
    except psutil.TimeoutExpired:
        # Revalidate identity before fallback termination (PID reuse / concurrent stop).
        if current := running_process(root):
            current.terminate()
            current.wait(timeout=3)


def start_service(root: Path = ROOT) -> None:
    if running_process(root):
        return
    command = service_command(root)
    data = service_dir(root)
    data.mkdir(parents=True, exist_ok=True)
    (data / "stop").unlink(missing_ok=True)
    with (data / "service.log").open("ab", buffering=0) as output:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=sys.platform != "win32",
        )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if running_process(root):
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError(f"Hey bro service did not start; see {data / 'service.log'}")


@contextmanager
def instance_lock(path: Path):
    """OS lock releases on crash; concurrent starts cannot open two microphones."""
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def supervise(controller, stop: threading.Event, stop_path: Path, *, clock=time.monotonic) -> None:
    """Retry missing/failed voice children, with bounded backoff and interruptible waits."""
    retry_at, backoff, healthy_since = 0.0, 1.0, None
    try:
        while not stop.is_set() and not stop_path.exists():
            now = clock()
            if controller.processes():
                healthy_since = now if healthy_since is None else healthy_since
                if now - healthy_since >= 30:
                    backoff = 1.0
            elif now >= retry_at:
                healthy_since = None
                try:
                    controller.start(manage_background=False, wake=True)
                    log.info("Started Hey bro voice listener")
                except Exception:
                    log.exception("Voice start failed; retrying in %.0fs", backoff)
                retry_at = now + backoff
                backoff = min(backoff * 2, 30)
            stop.wait(0.5)
    finally:
        controller.stop(manage_background=False)


def run(root: Path = ROOT) -> None:
    from .tray import AgentController

    os.chdir(root)
    load_environment(root)
    data = service_dir(root)
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=data / "service.log", level=logging.INFO, format="%(asctime)s %(message)s")
    with instance_lock(data / "instance.lock") as acquired:
        if not acquired:
            return
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        # A stale stop from the previous login must not disable login startup.
        (data / "stop").unlink(missing_ok=True)
        state = {"pid": os.getpid(), "created": psutil.Process().create_time()}
        temporary = data / "process.tmp"
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(data / "process.json")
        try:
            if sys.platform in {"win32", "darwin"} and flag("BRODI_TRAY"):
                subprocess.Popen(
                    [service_command(root)[0], "-m", "brodi.tray"],
                    cwd=root,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            supervise(AgentController(root), stop, data / "stop")
        finally:
            (data / "process.json").unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Always-available cloud Hey bro listener")
    parser.add_argument("action", choices=["enable", "disable", "status", "start", "stop", "restart", "run"])
    options = parser.parse_args(argv)
    if options.action == "run":
        run()
        return 0
    from .tray import AgentController

    load_environment()
    controller = AgentController()
    try:
        if options.action == "enable":
            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("Set OPENAI_API_KEY in .env.local first.")
            service_command()  # Validate before stopping a working session.
            controller.stop()
            install()
            start_service()
            print("Hey bro enabled at sign-in. Cloud microphone listening continues with the window closed.")
        elif options.action == "disable":
            uninstall()
            controller.stop()
            print("Hey bro startup disabled; microphone listener stopped.")
        elif options.action == "status":
            process = running_process()
            print(f"Login startup: {'enabled' if installed() else 'disabled'}")
            print(f"Supervisor: {'running (pid ' + str(process.pid) + ')' if process else 'stopped'}")
            print(f"Voice process: {'running' if controller.processes() else 'stopped'}")
        elif options.action == "stop":
            controller.stop()
            print("Microphone listener stopped. Start Brodi to resume; enabled startup resumes next sign-in.")
        elif options.action == "start":
            controller.start()
            print("Brodi started. Use wake status to check background startup.")
        elif options.action == "restart":
            controller.restart()
    except (OSError, RuntimeError, subprocess.SubprocessError, psutil.Error) as exc:
        print(f"Brodi: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
