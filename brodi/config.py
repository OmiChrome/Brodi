"""Load project configuration before importing modules with environment defaults."""

import os
from pathlib import Path

from dotenv import load_dotenv


def flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        raise ValueError(f"{name} must be true/false or 1/0")
    return value.lower() in {"true", "1", "yes", "on"}


def load_environment(root: Path | None = None) -> None:
    root = root or Path(__file__).resolve().parent.parent
    # Existing environment wins, followed by .env.local, then .env.
    load_dotenv(root / ".env.local")
    load_dotenv(root / ".env")
    yaml_path = Path(os.environ.get("BRODI_CONFIG", root / "brodi.yaml"))
    if yaml_path.exists():
        import yaml

        values = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        if not isinstance(values, dict):
            raise ValueError("Brodi YAML configuration must be a mapping of BRODI_* keys")
        for key, value in values.items():
            if not str(key).startswith("BRODI_") or not isinstance(value, (str, int, float, bool)):
                raise ValueError("YAML settings must be scalar BRODI_* values")
            os.environ.setdefault(key, str(value).lower() if isinstance(value, bool) else str(value))
