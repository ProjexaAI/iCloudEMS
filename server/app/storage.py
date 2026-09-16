"""File-backed token store: email -> {access_token, refresh_token, ...}.

For the platform integration, swap this for a DB-backed implementation
that encrypts tokens at rest. The interface is intentionally tiny.
"""
import json
from pathlib import Path

from .config import TOKEN_STORE_PATH
from .logging_utils import _log


class TokenStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else TOKEN_STORE_PATH
        self.data = {}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text()) or {}
        except Exception:
            self.data = {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2))
            try:
                self.path.chmod(0o600)
            except Exception:
                pass
        except Exception as e:
            _log(f"[tokenstore] save failed: {e!r}")

    def get(self, email):
        return self.data.get(email.lower().strip())

    def set(self, email, record):
        self.data[email.lower().strip()] = record
        self._save()

    def delete(self, email):
        key = email.lower().strip()
        if key in self.data:
            del self.data[key]
            self._save()
