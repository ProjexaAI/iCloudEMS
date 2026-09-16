"""Validated server configuration loaded from environment variables."""
import os
from pathlib import Path

def _env_bool(name: str, default: bool) -> bool:
	value = os.getenv(name)
	if value is None:
		return default
	return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
	value = int(os.getenv(name, str(default)))
	if not minimum <= value <= maximum:
		raise ValueError(f"{name} must be between {minimum} and {maximum}")
	return value


TOKEN_STORE_PATH = Path(os.getenv("ICLOUDEMS_TOKEN_STORE_PATH", str(Path.home() / ".icloudems_tokens.json")))
DEBUG_LOG        = Path(os.getenv("ICLOUDEMS_DEBUG_LOG", str(Path.home() / "icloudems_debug.log")))
ROSTER_DUMP      = Path(os.getenv("ICLOUDEMS_ROSTER_DUMP", str(Path.home() / "icloudems_roster_raw.json")))
SUBMIT_DUMP      = Path(os.getenv("ICLOUDEMS_SUBMIT_DUMP", str(Path.home() / "icloudems_submit_payload.json")))

DATABASE_URL = os.getenv("DATABASE_URL", "")
REDIS_URL = os.getenv("REDIS_URL", "")
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
SERVER_PORT = _env_int("SERVER_PORT", 8000, 1, 65535)

ROSTER_FETCH_CONCURRENCY = _env_int("ROSTER_FETCH_CONCURRENCY", 20, 1, 100)
MAX_JOBS_PER_SESSION = _env_int("MAX_JOBS_PER_SESSION", 2, 1, 20)
MAX_REQUESTS_PER_MINUTE = _env_int("MAX_REQUESTS_PER_MINUTE", 120, 1, 10000)

DEFAULT_ACADEMIC_YEAR = "2026-2027"

DEBUG_MODE = _env_bool("DEBUG_MODE", False)

if ENVIRONMENT == "production" and DEBUG_MODE:
	raise ValueError("DEBUG_MODE must be false in production")

if ENVIRONMENT == "production" and not TOKEN_ENCRYPTION_KEY:
	raise ValueError("TOKEN_ENCRYPTION_KEY must be configured in production")

