"""Absolute-path entrypoint used by Windows login startup and macOS launchd."""

from brodi.background import main

if __name__ == "__main__":
    raise SystemExit(main())
