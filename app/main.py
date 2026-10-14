"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from typing_extensions import Annotated

# Load local .env, but environment variables take priority (load_dotenv default behavior)
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Startup & Indexing Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1)])

    # 2. Attendance logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Logic
# --------------------------------------------------------------------------- #
def round_half_up(val: float | Decimal, decimals: int) -> float:
    """Arithmetic round half-up (R8)."""
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = map(int, s.split(":"))
    return h, m


def is_overnight(shift_start: str, shift_end: str) -> bool:
    h_s, m_s = parse_hhmm(shift_start)
    h_e, m_e = parse_hhmm(shift_end)
    return (h_e, m_e) <= (h_s, m_s)


def epoch_ms_to_utc_dt(ms: int) -> datetime:
    # Truncate instant to whole seconds before calculation / storage (R1)
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def utc_dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    if is_overnight(shift_start, shift_end):
        h_e, m_e = parse_hhmm(shift_end)
        shift_end_time = time(h_e, m_e, 0)
        if punch_in_ist.time() < shift_end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, att_date: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start;
    then late_minutes = floor((punch_in - shift_start) / 60).
    """
    y, m, d = map(int, att_date.split("-"))
    h_s, m_s = parse_hhmm(shift_start)
    shift_start_dt = datetime(y, m, d, h_s, m_s, 0, tzinfo=IST)
    diff_sec = int((punch_in_ist - shift_start_dt).total_seconds())
    if diff_sec > 600:  # strictly more than 10 minutes (600 seconds)
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = Decimal(int((punch_out_dt - punch_in_dt).total_seconds()))
    hours = diff_sec / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_ist: datetime, shift_start: str, shift_end: str, att_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; else 0.
    Overnight shifts end on the next calendar day.
    """
    y, m, d = map(int, att_date.split("-"))
    h_e, m_e = parse_hhmm(shift_end)
    shift_end_dt = datetime(y, m, d, h_e, m_e, 0, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        shift_end_dt += timedelta(days=1)
    diff_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    if diff_sec >= 1800:  # at least 30 minutes
        return diff_sec // 60
    return 0


def format_employee(doc: dict) -> dict:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = utc_dt_to_epoch_ms(created_at)
    else:
        created_at_ms = created_at

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def format_attendance_record(doc: dict) -> dict:
    history_api = []
    for h in doc.get("history", []) or []:
        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_ms = utc_dt_to_epoch_ms(at_val)
        else:
            at_ms = at_val

        changes_api = {}
        for k, v in (h.get("changes") or {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_v, datetime):
                    from_v = utc_dt_to_epoch_ms(from_v)
                if isinstance(to_v, datetime):
                    to_v = utc_dt_to_epoch_ms(to_v)
            changes_api[k] = {"from": from_v, "to": to_v}

        history_api.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_api,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_api,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(ge=100000000000, le=4102444800000, strict=True)]
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="emp_code already exists")
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0)

    ts_ist = ts_utc.astimezone(IST)
    att_date = get_attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(ts_ist, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find employee's most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts_utc}},
        sort=[("punch_in", -1)],
    )
    if not record:
        future_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$gt": ts_utc}},
            sort=[("punch_in", 1)],
        )
        if future_record:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punched_at must be strictly after punch_in and within 24 hours",
            )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Validate: punched_at <= punch_in or more than 24 hours after it -> 422
    if ts_utc <= punch_in_utc or ts_utc > punch_in_utc + timedelta(hours=24):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in and within 24 hours",
        )

    ts_ist = ts_utc.astimezone(IST)
    work_hrs = compute_work_hours(punch_in_utc, ts_utc)
    ot_min = compute_overtime(ts_ist, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hrs < 4.50

    # Race-safe atomic update
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="already punched out")

    return format_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="employee not found")

    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="attendance record not found for this date")

    # Business rule: setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cannot supply punch_in or punch_out when setting ABSENT or LEAVE",
        )

    target_status = body.status if body.status is not None else doc["status"]

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_utc_dt(body.punch_in)
        else:
            new_punch_in = doc.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="presence status requires punch_in",
            )

        if new_punch_in.tzinfo is None:
            new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        # punch_in must stay on the record's attendance date per R1
        computed_date = get_attendance_date(new_punch_in.astimezone(IST), emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in does not belong to the record's attendance date under R1",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_utc_dt(body.punch_out)
        else:
            new_punch_out = doc.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)
            if new_punch_out <= new_punch_in or new_punch_out > new_punch_in + timedelta(hours=24):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be after punch_in and within 24 hours",
                )

        # Recompute derived fields
        new_late_minutes = compute_late_minutes(new_punch_in.astimezone(IST), emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out.astimezone(IST),
                emp["shift_start"],
                emp["shift_end"],
                date,
            )
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute changes
    old_punch_in = doc.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = doc.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    old_vals = {
        "status": doc.get("status"),
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
    }
    new_vals = {
        "status": target_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    changes_db = {}
    for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old_vals[k] != new_vals[k]:
            changes_db[k] = {"from": old_vals[k], "to": new_vals[k]}

    if not changes_db:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    current_history_len = len(doc.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": doc["_id"],
            "history": {"$size": current_history_len},
        },
        {
            "$set": new_vals,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="concurrent modification conflict")

    return format_attendance_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="employee not found")

    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    month_start_date = date(year, m, 1)
    month_end_date = date(year, m, num_days)

    joined_date = date.fromisoformat(emp["joined_on"])
    effective_start = max(month_start_date, joined_date)

    working_days = 0
    if effective_start <= month_end_date:
        cur = effective_start
        while cur <= month_end_date:
            if cur.weekday() < 5:  # Mon-Fri
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
        }},
        {"$project": {
            "date": 1,
            "status": 1,
            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            "half_day": {"$ifNull": ["$half_day", False]},
            "dayOfWeek": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$dayOfWeek", [1, 2, 3, 4, 5]]},
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        ]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": "$late_minutes"
            },
            "total_overtime_minutes": {
                "$sum": "$overtime_minutes"
            },
        }},
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg.get("present_days", 0.0))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)
    month_end_str = f"{month}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": month_end_str}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"dept_emps": "$emp_codes"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$in": ["$emp_code", "$$dept_emps"]},
                            {"$gte": ["$date", f"{month}-01"]},
                            {"$lte": ["$date", month_end_str]},
                        ]
                    }
                }},
                {"$project": {
                    "emp_code": 1,
                    "date": 1,
                    "status": 1,
                    "work_hours": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "dayOfWeek": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": "$_id",
            "_id": 0,
            "headcount": 1,
            "present_days": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0.0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [
                                {"$and": [
                                    {"$in": ["$$this.dayOfWeek", [1, 2, 3, 4, 5]]},
                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]},
                                {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                0.0,
                            ]},
                        ]
                    },
                }
            },
            "late_count": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [{"$gt": ["$$this.late_minutes", 0]}, 1, 0]},
                        ]
                    },
                }
            },
            "total_late_minutes": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0,
                    "in": {
                        "$add": ["$$value", "$$this.late_minutes"],
                    },
                }
            },
            "leave_count": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                        ]
                    },
                }
            },
            "on_duty_count": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                        ]
                    },
                }
            },
            "valid_work_hours": {
                "$filter": {
                    "input": "$logs",
                    "as": "l",
                    "cond": {
                        "$and": [
                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$$l.work_hours", None]},
                        ]
                    },
                }
            },
        }},
        {"$project": {
            "department": 1,
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                    {"$avg": "$valid_work_hours.work_hours"},
                    None,
                ]
            },
        }},
        {"$sort": {"department": 1}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        avg_wh = doc.get("avg_work_hours")
        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": float(doc["present_days"]),
            "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
            "late_count": int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count": int(doc["leave_count"]),
            "on_duty_count": int(doc["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    _, num_days = calendar.monthrange(year, m)

    pipeline: List[Dict[str, Any]] = [
        {"$match": {
            "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}},
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from or (d_to - d_from).days > 92:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid date range")

    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)

    dept_emps = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]

    pipeline = [
        {"$match": {
            "emp_code": {"$in": dept_emps},
            "date": {"$gte": from_date, "$lte": to_date},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
        }},
        {"$project": {
            "date": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
            "present_count": 1,
            "late_count": 1,
            "_id": 0,
        }},
        {"$densify": {
            "field": "date",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt_plus_1],
            },
        }},
        {"$set": {
            "dateStr": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$isoDayOfWeek": "$date"}, [1, 2, 3, 4, 5]]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"cur_date": "$dateStr"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$cur_date"]},
                        ]
                    }
                }},
                {"$count": "count"},
            ],
            "as": "hc",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
        }},
        {"$set": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]
            },
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, "current"]},
                }
            },
        }},
        {"$project": {
            "_id": 0,
            "date": "$dateStr",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]

    items = []
    for r in db.attendance_logs.aggregate(pipeline):
        rate = r.get("attendance_rate")
        m_avg = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(m_avg, 4) if m_avg is not None else None,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusType] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from > date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        res = db.command("explain", command, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(res))}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
            }},
            {"$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$dayOfWeek", [1, 2, 3, 4, 5]]},
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            ]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                },
            }},
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(res))}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        month_end_str = f"{month}-{num_days:02d}"
        match_emp = {"joined_on": {"$lte": month_end_str}}
        if department:
            match_emp["department"] = department
        pipeline = [
            {"$match": match_emp},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"dept_emps": "$emp_codes"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$in": ["$emp_code", "$$dept_emps"]},
                                {"$gte": ["$date", f"{month}-01"]},
                                {"$lte": ["$date", month_end_str]},
                            ]
                        }
                    }},
                    {"$project": {
                        "emp_code": 1,
                        "date": 1,
                        "status": 1,
                        "work_hours": 1,
                        "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        "half_day": {"$ifNull": ["$half_day", False]},
                        "dayOfWeek": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
                    }},
                ],
                "as": "logs",
            }},
            {"$project": {
                "department": "$_id",
                "_id": 0,
                "headcount": 1,
            }},
        ]
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(res))}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="month is required")
        year, m = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, m)
        pipeline = [
            {"$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ])
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(res))}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="department, from, and to are required")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid date range")
        from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
        to_dt_plus_1 = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc) + timedelta(days=1)
        dept_emps = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]

        pipeline = [
            {"$match": {
                "emp_code": {"$in": dept_emps},
                "date": {"$gte": from_date, "$lte": to_date},
            }},
            {"$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }},
            {"$project": {
                "date": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1,
                "_id": 0,
            }},
            {"$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }},
        ]
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(res))}

    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"unknown endpoint: {endpoint}")
