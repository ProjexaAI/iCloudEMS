"""Logging helpers shared by the iCloudEMS layer.

Never raises: if the disk is unwritable, the app keeps running.
"""
import json
from datetime import datetime
from pathlib import Path


def _log(*parts):
    from .config import DEBUG_LOG
    msg = "  ".join(str(p) for p in parts)
    line = f"{datetime.now().isoformat()}  {msg}"
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print("[log]", msg)
    except Exception:
        pass


def _dump_json(data, path):
    try:
        Path(path).write_text(json.dumps(data, indent=2))
        _log(f"dumped json to {path}")
    except Exception as e:
        _log(f"failed to dump {path}: {e!r}")
