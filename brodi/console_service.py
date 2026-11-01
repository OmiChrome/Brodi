"""Run LiveKit's console mode in a pseudo-terminal for detached macOS use."""

from __future__ import annotations

import argparse
import os
import pty
import select
import signal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_path")
    parser.add_argument("--python", help="Absolute virtualenv Python for login startup without shell PATH")
    options = parser.parse_args()
    log_path = options.log_path
    child, master = pty.fork()
    if child == 0:
        command = (
            [options.python, "agent.py", "console"]
            if options.python
            else ["uv", "run", "python", "agent.py", "console"]
        )
        if output_device := os.getenv("BRODI_OUTPUT_DEVICE"):
            command.extend(["--output-device", output_device])
        if input_device := os.getenv("BRODI_INPUT_DEVICE"):
            command.extend(["--input-device", input_device])
        os.execvp(command[0], command)

    def stop(_signum: int, _frame: object) -> None:
        try:
            os.killpg(child, signal.SIGTERM)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with open(log_path, "ab", buffering=0) as log:
            while True:
                ready, _, _ = select.select([master], [], [], 0.5)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    log.write(data)
                ended, status = os.waitpid(child, os.WNOHANG)
                if ended:
                    return os.waitstatus_to_exitcode(status)
    finally:
        os.close(master)
        try:
            os.killpg(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
