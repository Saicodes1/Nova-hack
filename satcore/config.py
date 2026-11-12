import hashlib
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
CONTENT = (ROOT / "config/thresholds.yaml").read_bytes()
THRESHOLDS = yaml.safe_load(CONTENT)
THRESHOLDS_VERSION = hashlib.sha256(CONTENT).hexdigest()


def data_path(key: str, default: str) -> Path:
    return ROOT / os.getenv(key, default)
