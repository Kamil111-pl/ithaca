"""Process-level configuration.

Only things that must be known before the database opens live here (paths,
bind address). Everything a user can change at runtime lives in the
`settings` table and is read through `ithaca.settings`.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("ITHACA_DATA_DIR", ROOT / "data")).resolve()
WORKSPACE_DIR = Path(os.environ.get("ITHACA_WORKSPACE_DIR", DATA_DIR / "workspace")).resolve()
DB_PATH = DATA_DIR / "ithaca.db"
WEB_DIST = Path(os.environ.get("ITHACA_WEB_DIST", ROOT / "web" / "dist")).resolve()

HOST = os.environ.get("ITHACA_HOST", "127.0.0.1")
PORT = int(os.environ.get("ITHACA_PORT", "7100"))

# Local MLX model server (see model/serve.sh). Registered as a provider on
# first boot so a fresh install can talk to the distilled model immediately.
LOCAL_MODEL_URL = os.environ.get("ITHACA_LOCAL_MODEL_URL", "http://127.0.0.1:8080/v1")
# Local vision model (Qwen3-VL via mlx-vlm) used for computer use.
LOCAL_VISION_URL = os.environ.get("ITHACA_LOCAL_VISION_URL", "http://127.0.0.1:8090/v1")

SESSION_TTL_SECONDS = 14 * 24 * 3600


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
