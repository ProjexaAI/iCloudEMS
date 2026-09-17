"""Timetable routes — fetches directly from iCloudEMS."""
from typing import Any, Dict
from fastapi import APIRouter, HTTPException, Query

from ..config import (
    ROSTER_CACHE_TTL_SECONDS, ROSTER_CACHE_TTL_TODAY_SECONDS, ROSTER_CACHE_TTL_RECENT_SECONDS,
)
from ..icloudems import extract_entries_for_date
from ..logging_utils import _log
from ..mirror import mirror
from ..storage import create_token_store
from . import get_client
from datetime import date as dt_date

router = APIRouter()
token_store = create_token_store()


@router.get("/{sid}/timetable")
def timetable(sid: str, date: str = Query(...), force: bool = Query(False)) -> Dict[str, Any]:
    """Fetch timetable directly from iCloudEMS."""
    client = get_client(sid)
    empid = client.empid

    # 1. Check mirror cache first
    if mirror is not None and empid and not force:
        try:
            today = dt_date.today()
            target_dt = dt_date.fromisoformat(date)
            if target_dt == today:
                ttl = ROSTER_CACHE_TTL_TODAY_SECONDS
            elif (today - target_dt).days <= 7:
                ttl = ROSTER_CACHE_TTL_RECENT_SECONDS
            else:
                ttl = ROSTER_CACHE_TTL_SECONDS
        except Exception:
            ttl = ROSTER_CACHE_TTL_TODAY_SECONDS
        cached_entries = mirror.get_timetable(empid, date, date, max_age_seconds=ttl)
        if cached_entries is not None:
            _log(f"[timetable] CACHE HIT for empid={empid} date={date} ({len(cached_entries)} entries)")
            return {"date": date, "entries": cached_entries}

    # 2. Fetch directly from iCloudEMS
    _log(f"[timetable] fetching from iCloudEMS for empid={empid} date={date}")
    raw = client._with_auto_refresh(
        client.get_timetable, empid, date, date,
    )

    # Save to mirror
    if mirror is not None and empid:
        try:
            mirror.save_raw_timetable(empid, raw)
        except Exception as err:
            _log(f"[timetable] mirror write error: {err}")

    entries = extract_entries_for_date(raw, date) if date else []
    _log(f"[timetable] got {len(entries)} entries for {date}")
    return {"date": date, "entries": entries}
