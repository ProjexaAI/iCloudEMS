"""Server-wide constants. No iCloudEMS specifics here — those live in
app/icloudems/."""
from pathlib import Path

TOKEN_STORE_PATH = Path.home() / ".icloudems_tokens.json"
DEBUG_LOG        = Path.home() / "icloudems_debug.log"
ROSTER_DUMP      = Path.home() / "icloudems_roster_raw.json"
SUBMIT_DUMP      = Path.home() / "icloudems_submit_payload.json"

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000

ROSTER_FETCH_CONCURRENCY = 50

