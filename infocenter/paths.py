import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def home_dir() -> Path:
    raw = os.environ.get("INFOCENTER_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".infocenter"


def default_url() -> str:
    from infocenter import DEFAULT_HOST, DEFAULT_PORT

    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
