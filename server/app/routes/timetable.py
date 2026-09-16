from fastapi import APIRouter, Depends, HTTPException, Query

from ..icloudems import ICloudEMSClient, extract_entries_for_date
from ..schemas import TimetableResponse
from ..sessions import store

router = APIRouter()


def get_client(sid: str):
    client = store.get(sid)
    if not client:
        raise HTTPException(404, "session not found")
    if not client.empid:
        raise HTTPException(400, "session has no empid (not logged in?)")
    return client


@router.get("/{sid}/timetable", response_model=TimetableResponse)
def timetable(sid: str, date: str = Query(...)):
    client: ICloudEMSClient = get_client(sid)
    data = client._with_auto_refresh(
        client.get_timetable, client.empid, date, date,
    )
    entries = extract_entries_for_date(data, date)
    return TimetableResponse(date=date, entries=entries)
