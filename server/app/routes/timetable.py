from fastapi import APIRouter, HTTPException, Query

from ..icloudems import extract_entries_for_date
from ..icloudems.http import HTTPError
from ..config import PROVIDER_COOLDOWN_SECONDS, PROVIDER_FAILURE_THRESHOLD
from ..logging_utils import _log
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
        _log(f"[timetable] circuit breaker open for {provider_key}")
        raise HTTPException(503, "timetable provider temporarily unavailable; use cached data")
    try:
        _log(f"[timetable] fetching for empid={client.empid} date={date}")
        data = client._with_auto_refresh(
            client.get_timetable, client.empid, date, date,
        )
        runtime_state.record_provider_success(provider_key)
        _log(f"[timetable] success for date={date}")
    except HTTPError as exc:
        runtime_state.record_provider_failure(provider_key)
        _log(f"[timetable] HTTPError status={exc.status} reason={exc.reason} url={exc.url} body={exc.body[:500] if exc.body else '(empty)'}")
        if exc.status == 401:
            raise HTTPException(401, "session expired; please sign in again") from exc
        raise HTTPException(502, f"timetable provider error: {exc.status} {exc.reason}") from exc
    except ValueError as exc:
        _log(f"[timetable] ValueError: {exc}")
        raise HTTPException(400, "invalid timetable date") from exc
    except Exception as exc:
        _log(f"[timetable] unexpected error: {type(exc).__name__}: {exc}")
        raise
    entries = extract_entries_for_date(data, date)
    return TimetableResponse(date=date, entries=entries)
