from fastapi import APIRouter, Query

from ..icloudems import extract_entries_for_date
from ..schemas import TimetableResponse
from . import get_client

router = APIRouter()


@router.get("/{sid}/timetable", response_model=TimetableResponse)
def timetable(sid: str, date: str = Query(...)):
    client = get_client(sid)
    data = client._with_auto_refresh(
        client.get_timetable, client.empid, date, date,
    )
    entries = extract_entries_for_date(data, date)
    return TimetableResponse(date=date, entries=entries)
