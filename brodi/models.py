"""Explicit model defaults and stable cache keys; never silently substitute models."""

import hashlib
import json
import os

DEFAULT_MODEL = "gpt-6-luna"


def model(role: str) -> str:
    return os.environ.get(f"BRODI_{role.upper()}_MODEL", DEFAULT_MODEL)


def cache_key(role: str, schema: object = "") -> str:
    stable = json.dumps(schema, sort_keys=True, default=str, separators=(",", ":"))
    return "brodi-" + role + "-" + hashlib.sha256(stable.encode()).hexdigest()[:24]
