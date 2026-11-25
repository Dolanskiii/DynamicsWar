"""Where DynamicsWar keeps its settings (outside the project folder, so a token never ends up in git)."""
import json
import os
import sys
from pathlib import Path

APP = "DynamicsWar"


def config_dir():
    if os.environ.get("DYNAMICSWAR_HOME"):
        base = Path(os.environ["DYNAMICSWAR_HOME"])
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP.lower()
    return base


def config_path():
    return config_dir() / "config.json"


def load():
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(cfg):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

