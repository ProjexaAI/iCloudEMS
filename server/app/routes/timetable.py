"""Timetable routes — always returns proxy instructions to the mobile app."""
from typing import Any, Dict, Union
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
    """Return proxy instruction for the mobile app to fetch timetable.

    The mobile app executes this against iCloudEMS using its own IP,
    then posts the raw response to /proxy/ingest.
    """
    client = get_client(sid)
    empid = client.empid

    # 1. Check mirror cache first (no proxy needed if cached)
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

    # 2. Return proxy instruction — mobile fetches from iCloudEMS
    tasks = client.build_proxy_timetable_tasks(empid, date, date)
    task = tasks[0] if tasks else client.build_proxy_timetable(empid, date, date)
    task["meta"] = {"route": "timetable", "date": date}
    task["proxy_required"] = True
    _log(f"[timetable] proxy instruction for empid={empid} date={date}")
    return task
