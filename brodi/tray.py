"""Windows tray controls; voice execution stays in its own process."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import psutil

from .activity import JournalReader, clean, default_path
from .config import flag, load_environment
from .paths import user_data_dir

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("brodi.tray")


def owns_agent(command: list[str], cwd: str, root: Path = ROOT) -> bool:
    """Match an exact entrypoint and checkout, not a substring in another app."""
    for index, argument in enumerate(command[:-1]):
        if Path(argument).name.casefold() == "agent.py" and command[index + 1] == "console":
            path = Path(argument)
            if not path.is_absolute():
                path = Path(cwd) / path
            return path.resolve() == (root / "agent.py").resolve()
        if argument == "-m" and command[index + 1] == "brodi.wake_runtime":
            return Path(cwd).resolve() == root.resolve()
    return False


class AgentController:
    """Manage this checkout's voice processes; leave launched user apps open."""

    def __init__(self, root: Path = ROOT):
        self.root = root
        self.log_path = Path(os.environ.get("BRODI_LOG", Path(tempfile.gettempdir()) / "brodi-console.log"))
        self._child: subprocess.Popen | None = None

    @property
    def python(self) -> str:
        executable = Path(sys.executable)
        console = executable.with_name("python.exe")
        return str(console if sys.platform == "win32" and console.exists() else executable)

    def processes(self) -> list:
        if self._child is not None and self._child.poll() is not None:
            self._child = None
        found = []
        for process in psutil.process_iter(["name", "cmdline"]):
            try:
                info = process.info
                if not (info.get("name") or "").lower().startswith(("python", "uv")):
                    continue
                command = info.get("cmdline") or []
                if owns_agent(command, process.cwd(), self.root):
                    found.append(process)
            except (psutil.Error, OSError):
                continue
        return found

    @staticmethod
    def leaf_pid(processes: list) -> int | None:
        parents = set()
        for process in processes:
            try:
                parents.add(process.ppid())
            except psutil.Error:
                pass
        return next((process.pid for process in processes if process.pid not in parents), None)

    def start(self) -> None:
        if self.processes():
            return
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Set OPENAI_API_KEY in .env.local before starting Brodi.")
        if flag("BRODI_LOCAL_WAKE"):
            from .wake import model_path

            if not model_path().is_dir():
                raise RuntimeError("Download the local voice model first: python -m brodi.wake --download-model")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab", buffering=0) as output:
            self._child = subprocess.Popen(
                [self.python, str(self.root / "agent.py"), "console"],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def stop(self) -> None:
        targets = {process.pid: process for process in self.processes()}
        for process in list(targets.values()):
            try:
                for child in process.children(recursive=True):
                    command = child.cmdline()
                    if any(
                        command[index : index + 2] == ["-m", "brodi.windows_uia"] for index in range(len(command) - 1)
                    ):
                        targets[child.pid] = child
            except psutil.Error:
                pass
        for process in reversed(list(targets.values())):
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(list(targets.values()), timeout=2)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=1)
        if alive:
            raise RuntimeError("Brodi could not stop. Check the activity log before restarting.")

    def restart(self) -> None:
        self.stop()
        self.start()

    def show_activity(self) -> None:
        arguments = [self.python, "-m", "brodi.activity"]
        if pid := self.leaf_pid(self.processes()):
            arguments.extend(["--pid", str(pid)])
        subprocess.Popen(arguments, cwd=self.root, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def show_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        subprocess.Popen(["notepad.exe", str(self.log_path)], cwd=self.root)


class TrayApp:
    def __init__(self, controller: AgentController | None = None):
        self.controller = controller or AgentController()
        self.status = "Stopped"
        self.running = False
        self.busy = threading.Lock()
        self.closing = threading.Event()
        self.reader = JournalReader(default_path())
        self.states: dict[int, str] = {}
        self.icon = None

    def refresh(self) -> None:
        processes = self.controller.processes()
        self.running = bool(processes)
        for event in self.reader.poll():
            if event["kind"] in {"status", "error"} and isinstance(event.get("pid"), int):
                self.states[event["pid"]] = str(clean(event["message"]))
        self.states = {pid: state for pid, state in self.states.items() if pid in {p.pid for p in processes}}
        self.status = self.states.get(self.controller.leaf_pid(processes), "Starting") if self.running else "Stopped"
        if self.icon is not None:
            self.icon.title = ("Brodi — " + self.status)[:127]
            self.icon.update_menu()

    def perform(self, action: str) -> bool:
        if not self.busy.acquire(blocking=False):
            return False
        try:
            if action == "quit":
                self.controller.stop()
                self.closing.set()
                if self.icon is not None:
                    self.icon.stop()
            else:
                getattr(self.controller, action)()
            return True
        except Exception as exc:
            message = str(clean(exc))
            log.error("Tray action %s failed: %s", action, message)
            if self.icon is not None:
                self.icon.notify(message, "Brodi needs attention")
            return False
        finally:
            self.busy.release()

    def callback(self, action: str):
        def invoke(icon, item):
            threading.Thread(target=self.perform, args=(action,), daemon=True, name="brodi-tray-action").start()

        return invoke

    def menu(self, pystray):
        item = pystray.MenuItem

        def ready(_):
            return not self.busy.locked()

        return pystray.Menu(
            item(lambda _: "Brodi — " + self.status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Start assistant", self.callback("start"), enabled=lambda _: ready(_) and not self.running),
            item("Stop assistant", self.callback("stop"), enabled=lambda _: ready(_) and self.running),
            item("Restart assistant", self.callback("restart"), enabled=lambda _: ready(_) and self.running),
            pystray.Menu.SEPARATOR,
            item("Show activity", self.callback("show_activity"), default=True, enabled=ready),
            item("Open log", self.callback("show_log"), enabled=ready),
            pystray.Menu.SEPARATOR,
            item("Quit Brodi", self.callback("quit"), enabled=ready),
        )

    def run(self) -> None:
        import pystray
        from PIL import Image

        image_path = self.controller.root / "assets" / "branding" / "brodi.ico"
        with Image.open(image_path) as source:
            image = source.convert("RGBA")
        self.icon = pystray.Icon("brodi", image, "Brodi — Starting", self.menu(pystray))

        def monitor(icon):
            icon.visible = True
            while not self.closing.is_set():
                try:
                    self.refresh()
                except Exception as exc:
                    log.error("Tray refresh failed: %s", clean(exc))
                self.closing.wait(1)

        try:
            self.icon.run(setup=monitor)
        finally:
            self.closing.set()


def acquire_instance(root: Path = ROOT):
    """Hold a session-local mutex until this tray process exits."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    name = hashlib.sha256(str(root.resolve()).casefold().encode()).hexdigest()[:20]
    handle = kernel.CreateMutexW(None, False, "Local\\BrodiTray-" + name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return None
    return kernel, handle


def main() -> None:
    parser = argparse.ArgumentParser(description="Brodi Windows notification-area controls")
    parser.add_argument("--force", action="store_true", help="show even if automatic tray is disabled")
    options = parser.parse_args()
    if sys.platform != "win32":
        parser.error("The tray controller is currently available on Windows only.")
    load_environment()
    if not options.force and not flag("BRODI_TRAY"):
        return
    data = user_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=data / "tray.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    instance = acquire_instance()
    if instance is None:
        return
    kernel, handle = instance
    try:
        TrayApp().run()
    except Exception:
        log.exception("Tray stopped unexpectedly")
        raise
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
