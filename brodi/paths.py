import os
import sys
from pathlib import Path


def user_data_dir() -> Path:
    if os.environ.get("BRODI_DATA_DIR"):
        return Path(os.environ["BRODI_DATA_DIR"]).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Brodi"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Brodi"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "brodi"
