from fastapi import APIRouter, HTTPException, Query

from ..icloudems import extract_entries_for_date
from ..icloudems.http import HTTPError
from ..config import PROVIDER_COOLDOWN_SECONDS, PROVIDER_FAILURE_THRESHOLD
from ..runtime_state import runtime_state
from ..schemas import TimetableResponse
from . import get_client

router = APIRouter()


@router.get("/{sid}/timetable", response_model=TimetableResponse)
def timetable(sid: str, date: str = Query(...)):
    client = get_client(sid)
    provider_key = "timetable"
    if not runtime_state.provider_available(
        provider_key, PROVIDER_FAILURE_THRESHOLD, PROVIDER_COOLDOWN_SECONDS,
    ):
        raise HTTPException(503, "timetable provider temporarily unavailable; use cached data")
    try:
        data = client._with_auto_refresh(
            client.get_timetable, client.empid, date, date,
        )
        runtime_state.record_provider_success(provider_key)
    except HTTPError as exc:
        runtime_state.record_provider_failure(provider_key)
        if exc.status == 401:
            raise HTTPException(401, "session expired; please sign in again") from exc
        raise HTTPException(502, "timetable provider is temporarily unavailable") from exc
    except ValueError as exc:
        raise HTTPException(400, "invalid timetable date") from exc
    entries = extract_entries_for_date(data, date)
    return TimetableResponse(date=date, entries=entries)
