from fastapi import APIRouter, HTTPException, Query

from ..icloudems import extract_entries_for_date
from ..icloudems.http import HTTPError
from ..schemas import TimetableResponse
from . import get_client

router = APIRouter()


@router.get("/{sid}/timetable", response_model=TimetableResponse)
def timetable(sid: str, date: str = Query(...)):
    client = get_client(sid)
    try:
        data = client._with_auto_refresh(
            client.get_timetable, client.empid, date, date,
        )
    except HTTPError as exc:
        if exc.status == 401:
            raise HTTPException(401, "session expired; please sign in again") from exc
        raise HTTPException(502, "timetable provider is temporarily unavailable") from exc
    except ValueError as exc:
        raise HTTPException(400, "invalid timetable date") from exc
    entries = extract_entries_for_date(data, date)
    return TimetableResponse(date=date, entries=entries)
