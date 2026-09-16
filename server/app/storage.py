"""File-backed token store: email -> {access_token, refresh_token, ...}.

For the platform integration, swap this for a DB-backed implementation
that encrypts tokens at rest. The interface is intentionally tiny.
"""
import json
import threading
from contextlib import closing
from pathlib import Path

from .config import DATABASE_URL, ENVIRONMENT, TOKEN_ENCRYPTION_KEY, TOKEN_STORE_PATH
from .logging_utils import _log


class TokenStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else TOKEN_STORE_PATH
        self._lock = threading.Lock()
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
        with self._lock:
            return self.data.get(email.lower().strip())

    def set(self, email, record):
        with self._lock:
            self.data[email.lower().strip()] = record
            self._save()

    def delete(self, email):
        with self._lock:
            key = email.lower().strip()
            if key in self.data:
                del self.data[key]
                self._save()


class DatabaseTokenStore:
    """PostgreSQL-backed token store with application-layer encryption."""

    def __init__(self, database_url=None, encryption_key=None):
        self.database_url = database_url or DATABASE_URL
        key = encryption_key or TOKEN_ENCRYPTION_KEY
        if not self.database_url or not key:
            raise RuntimeError("DATABASE_URL and TOKEN_ENCRYPTION_KEY are required")
        try:
            from cryptography.fernet import Fernet
            import psycopg
        except ImportError as exc:
            raise RuntimeError("install psycopg and cryptography for database storage") from exc
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        self._psycopg = psycopg
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self):
        return self._psycopg.connect(self.database_url)

    def _ensure_schema(self):
        with closing(self._connect()) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS provider_tokens (
                        email TEXT PRIMARY KEY,
                        contact TEXT,
                        username TEXT,
                        empid TEXT,
                        access_token BYTEA,
                        refresh_token BYTEA,
                        device_id TEXT,
                        saved_at DOUBLE PRECISION NOT NULL
                    )
                """)
            conn.commit()

    def _encrypt(self, value):
        return self._fernet.encrypt((value or "").encode())

    def _decrypt(self, value):
        return self._fernet.decrypt(bytes(value)).decode() if value else None

    def get(self, email):
        with self._lock, closing(self._connect()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT contact, username, empid, access_token, refresh_token, device_id, saved_at "
                    "FROM provider_tokens WHERE email = %s",
                    (email.lower().strip(),),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {
            "contact": row[0], "username": row[1], "empid": row[2],
            "access_token": self._decrypt(row[3]),
            "refresh_token": self._decrypt(row[4]),
            "device_id": row[5], "saved_at": row[6],
        }

    def set(self, email, record):
        with self._lock, closing(self._connect()) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO provider_tokens
                        (email, contact, username, empid, access_token,
                         refresh_token, device_id, saved_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET
                        contact = EXCLUDED.contact,
                        username = EXCLUDED.username,
                        empid = EXCLUDED.empid,
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        device_id = EXCLUDED.device_id,
                        saved_at = EXCLUDED.saved_at
                """, (
                    email.lower().strip(), record.get("contact"), record.get("username"),
                    record.get("empid"), self._encrypt(record.get("access_token")),
                    self._encrypt(record.get("refresh_token")), record.get("device_id"),
                    record.get("saved_at"),
                ))
            conn.commit()

    def delete(self, email):
        with self._lock, closing(self._connect()) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM provider_tokens WHERE email = %s", (email.lower().strip(),))
            conn.commit()


def create_token_store():
    if ENVIRONMENT == "production":
        return DatabaseTokenStore()
    return TokenStore()
