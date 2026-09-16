"""Logging helpers shared by the iCloudEMS layer.

Never raises: if the disk is unwritable, the app keeps running.
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path


_logger = logging.getLogger("icloudems")
_logger.setLevel(logging.INFO)
if not _logger.handlers:
    _logger.addHandler(logging.StreamHandler())

_SECRET_PATTERN = re.compile(
    r"(?i)(access_token|refresh_token|jwt_token|authorization|password|otp)"
    r"([=: ]+)([^, ]+)"
)


def _redact(message: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2[REDACTED]", message)


def _log(*parts):
    from .config import DEBUG_LOG
    msg = "  ".join(str(p) for p in parts)
    msg = _redact(msg)
    line = f"{datetime.now().isoformat()}  {msg}"
    try:
        if os.getenv("LOG_TO_FILE", "false").lower() == "true":
            with open(DEBUG_LOG, "a") as f:
                f.write(line + "\n")
    except Exception:
        pass
    try:
        _logger.info(msg)
    except Exception:
        pass


def _dump_json(data, path):
    try:
        Path(path).write_text(json.dumps(data, indent=2))
        _log(f"dumped json to {path}")
    except Exception as e:
        _log(f"failed to dump {path}: {e!r}")
