"""Load project configuration before importing modules with environment defaults."""

from pathlib import Path

from dotenv import load_dotenv


def load_environment(root: Path | None = None) -> None:
    root = root or Path(__file__).resolve().parent.parent
    # Existing environment wins, followed by .env.local, then .env.
    load_dotenv(root / ".env.local")
    load_dotenv(root / ".env")
