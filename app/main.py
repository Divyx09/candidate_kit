"""
Employee Attendance & Analytics API
FastAPI + MongoDB backend for HROne Software Engineer Hiring Assignment (Contract v2.0).

All endpoints, business rules (R1-R10), validations, and aggregation pipelines
are self-contained in this file.
"""
import calendar
import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# MongoDB configuration (real environment variables take priority)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup: Create Indexes Idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING)])

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    except Exception as e:
        # Allow startup even if DB is temporarily unreachable (health check will report 503)
        pass


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R8)
# --------------------------------------------------------------------------- #
def to_epoch_ms(dt_val: Any) -> Optional[int]:
    """Convert a datetime (or existing epoch ms) to integer epoch milliseconds."""
    if dt_val is None:
        return None
    if isinstance(dt_val, (int, float)):
        return int(dt_val)
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=UTC)
        return int(dt_val.timestamp() * 1000)
    return None


def truncate_to_whole_seconds(dt: datetime) -> datetime:
    """Truncate datetime to whole seconds (microsecond=0) per R1."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.replace(microsecond=0)


def round_half_up(value: Union[float, int, Decimal], decimals: int = 2) -> Decimal:
    """Perform exact round-half-up (R4, R8)."""
    d = Decimal(str(value))
    target = Decimal("10") ** (-decimals)
    return d.quantize(target, rounding=ROUND_HALF_UP)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is the IST calendar date of punch_in.
    Exception: Overnight shift (shift_end <= shift_start):
    If punch-in IST time is strictly earlier than shift_end, it belongs
    to the shift starting on the previous calendar day.
    """
    punch_in_trunc = truncate_to_whole_seconds(punch_in_utc)
    punch_in_ist = punch_in_trunc.astimezone(IST)

    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(end_h, end_m):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str) -> int:
    """
    R1 & R2: Late if punch-in is strictly > 10:00 (600s) after shift_start.
    late_minutes = floor(elapsed_seconds / 60) since shift_start, else 0.
    """
    punch_in_trunc = truncate_to_whole_seconds(punch_in_utc)
    punch_in_ist = punch_in_trunc.astimezone(IST)

    att_date_str = get_attendance_date(punch_in_trunc, shift_start, shift_end)
    att_date = date.fromisoformat(att_date_str)

    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(att_date, time(start_h, start_m), tzinfo=IST)

    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:
        return math.floor(elapsed_seconds / 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> Optional[float]:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up.
    """
    if punch_in_utc is None or punch_out_utc is None:
        return None
    in_trunc = truncate_to_whole_seconds(punch_in_utc)
    out_trunc = truncate_to_whole_seconds(punch_out_utc)
    seconds = (out_trunc - in_trunc).total_seconds()
    if seconds <= 0:
        return 0.0
    val = Decimal(str(seconds)) / Decimal("3600")
    return float(round_half_up(val, 2))


def compute_overtime(
    punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """
    R3: Whole minutes floor from shift_end to punch_out, counted only if >= 30, else 0.
    For overnight shifts, shift_end falls on attendance_date + 1 day.
    """
    if punch_out_utc is None:
        return 0
    punch_out_trunc = truncate_to_whole_seconds(punch_out_utc)
    punch_out_ist = punch_out_trunc.astimezone(IST)

    att_date = date.fromisoformat(attendance_date)
    is_overnight = shift_end <= shift_start
    shift_end_date = att_date + timedelta(days=1) if is_overnight else att_date

    end_h, end_m = map(int, shift_end.split(":"))
    shift_end_dt = datetime.combine(shift_end_date, time(end_h, end_m), tzinfo=IST)

    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if elapsed_seconds <= 0:
        return 0
    minutes = math.floor(elapsed_seconds / 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document into strict AttendanceRecord schema."""
    half_day = doc.get("half_day", False)
    if half_day is None:
        half_day = False

    raw_history = doc.get("history", [])
    if raw_history is None:
        raw_history = []

    history = []
    for h in raw_history:
        at_ms = to_epoch_ms(h.get("at"))
        changes = {}
        for k, v in h.get("changes", {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                changes[k] = {
                    "from": to_epoch_ms(from_v),
                    "to": to_epoch_ms(to_v),
                }
            else:
                changes[k] = {"from": from_v, "to": to_v}
        history.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": half_day,
        "history": history,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document into Employee schema."""
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_and_joined(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date YYYY-MM-DD")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health", status_code=200)
def health():
    """Readiness probe: 200 only if MongoDB answers ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforced by unique index on emp_code."""
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee_doc(doc)


@app.get("/employees", status_code=200)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Operations
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record punch-in, compute date (R1) & late_minutes (R2). Race-safe."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_ms = (body.punched_at // 1000) * 1000
    else:
        now_dt = datetime.now(UTC)
        ts_ms = int(now_dt.timestamp()) * 1000

    punch_in_dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_dt, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    """
    Close employee's most recent record where punch_in <= punched_at.
    Race-safe: atomic update on punch_out: None.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_ms = (body.punched_at // 1000) * 1000
    else:
        now_dt = datetime.now(UTC)
        ts_ms = int(now_dt.timestamp()) * 1000

    punch_out_dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)

    # Find most recent record with punch_in <= punch_out_dt
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
    if punch_out_dt > punch_in_dt + timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_dt,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance", status_code=200)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
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

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """Regularize attendance record, recompute derived values, and append audit history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    orig_status = record.get("status")
    orig_in = record.get("punch_in")
    if orig_in and orig_in.tzinfo is None:
        orig_in = orig_in.replace(tzinfo=UTC)
    orig_out = record.get("punch_out")
    if orig_out and orig_out.tzinfo is None:
        orig_out = orig_out.replace(tzinfo=UTC)
    orig_work_hours = record.get("work_hours")
    orig_late = record.get("late_minutes", 0)
    orig_ot = record.get("overtime_minutes", 0)
    orig_half = record.get("half_day", False) or False

    target_status = body.status if body.status is not None else orig_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch times cannot be supplied when status is ABSENT or LEAVE",
            )
        target_in = None
        target_out = None
        new_work_hours = None
        new_late = 0
        new_ot = 0
        new_half = False
    else:
        # Presence status
        if body.punch_in is not None:
            in_ms = (body.punch_in // 1000) * 1000
            target_in = datetime.fromtimestamp(in_ms / 1000, tz=UTC)
        else:
            target_in = orig_in

        if target_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        # R1 attendance date check
        calc_date = get_attendance_date(target_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in falls on attendance date {calc_date}, which does not match {date}",
            )

        if body.punch_out is not None:
            out_ms = (body.punch_out // 1000) * 1000
            target_out = datetime.fromtimestamp(out_ms / 1000, tz=UTC)
        else:
            target_out = orig_out

        if target_out is not None:
            if target_out <= target_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if target_out > target_in + timedelta(hours=24):
                raise HTTPException(
                    status_code=422, detail="punch_out cannot be more than 24 hours after punch_in"
                )
            new_work_hours = compute_work_hours(target_in, target_out)
            new_ot = compute_overtime(target_out, emp["shift_start"], emp["shift_end"], date)
            new_half = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_ot = 0
            new_half = False

        new_late = compute_late_minutes(target_in, emp["shift_start"], emp["shift_end"])

    # Compute changes diff
    changes: Dict[str, Any] = {}
    if target_status != orig_status:
        changes["status"] = {"from": orig_status, "to": target_status}
    if target_in != orig_in:
        changes["punch_in"] = {"from": orig_in, "to": target_in}
    if target_out != orig_out:
        changes["punch_out"] = {"from": orig_out, "to": target_out}
    if new_work_hours != orig_work_hours:
        changes["work_hours"] = {"from": orig_work_hours, "to": new_work_hours}
    if new_late != orig_late:
        changes["late_minutes"] = {"from": orig_late, "to": new_late}
    if new_ot != orig_ot:
        changes["overtime_minutes"] = {"from": orig_ot, "to": new_ot}
    if new_half != orig_half:
        changes["half_day"] = {"from": orig_half, "to": new_half}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_dt = datetime.now(UTC)
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length
    curr_history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, curr_history_len]},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_in,
                "punch_out": target_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_half,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Endpoints: Analytics
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str, num_days: int) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": ["$day_of_week", 5]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", status_code=200)
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee via MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]

    # Calculate working days (Mon-Fri) on or after joined_on
    working_days = 0
    joined_on_str = emp["joined_on"]
    for day in range(1, num_days + 1):
        cur_date_str = f"{year:04d}-{m:02d}-{day:02d}"
        if cur_date_str >= joined_on_str:
            if date(year, m, day).weekday() < 5:
                working_days += 1

    pipeline = build_employee_monthly_pipeline(emp_code, month, num_days)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        leave_days = res.get("leave_days", 0)
        late_count = res.get("late_count", 0)
        total_late = res.get("total_late_minutes", 0)
        total_ot = res.get("total_overtime_minutes", 0)
        present_days = float(round_half_up(res.get("present_days", 0.0), 2))
    else:
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0
        present_days = 0.0

    if working_days == 0:
        attendance_pct = None
    else:
        pct_val = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
        attendance_pct = float(round_half_up(pct_val, 2))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(
    month: str, end_date: str, department: Optional[str]
) -> List[Dict[str, Any]]:
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_emps"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$lte": ["$$l.day_of_week", 5]},
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
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
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {"$sum": "$logs.late_minutes"},
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


@app.get("/analytics/departments/summary", status_code=200)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month via MongoDB cross-collection aggregation."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    end_date = f"{month}-{num_days:02d}"

    pipeline = build_department_summary_pipeline(month, end_date, department)
    docs = list(db.employees.aggregate(pipeline))

    items = []
    for d in docs:
        avg_wh = d.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = float(round_half_up(avg_wh, 2))
        items.append({
            "department": d["department"],
            "headcount": d["headcount"],
            "present_days": float(round_half_up(d.get("present_days", 0.0), 2)),
            "avg_work_hours": avg_wh,
            "late_count": d.get("late_count", 0),
            "total_late_minutes": d.get("total_late_minutes", 0),
            "leave_count": d.get("leave_count", 0),
            "on_duty_count": d.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, num_days: int, limit: int, department: Optional[str]
) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-{num_days:02d}"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", status_code=200)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard with standard competition ranking and ties."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]

    pipeline = build_late_leaderboard_pipeline(month, num_days, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str,
    from_date_str: str,
    to_date_str: str,
    from_dt: datetime,
    to_dt_bound: datetime,
    dept_emp_codes: List[str],
) -> List[Dict[str, Any]]:
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date_dt": {"$literal": from_dt}}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bound],
                },
            }
        },
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt", "timezone": "+00:00"}},
                "day_of_week": {"$isoDayOfWeek": "$date_dt"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$lte": ["$day_of_week", 5]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", dept_emp_codes]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$hc_docs.count"}, 0]},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]


@app.get("/analytics/departments/{department}/trend", status_code=200)
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend with gap filling ($densify) and 7-day moving average."""
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    dept_emp_codes = [e["emp_code"] for e in dept_emps]
    from_dt = datetime.combine(d_from, time(0, 0), tzinfo=UTC)
    to_dt_bound = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=UTC)

    pipeline = build_department_trend_pipeline(
        department, from_date, to_date, from_dt, to_dt_bound, dept_emp_codes
    )
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for row in raw_items:
        rate = row.get("attendance_rate")
        if rate is not None:
            rate = float(round_half_up(rate, 4))

        m_avg = row.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = float(round_half_up(m_avg, 4))

        items.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": float(round_half_up(row.get("present_count", 0.0), 2)),
            "late_count": row.get("late_count", 0),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints: Admin Explain (Part C)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", status_code=200)
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_filter: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(
        None, alias="status"
    ),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Returns raw MongoDB explain executionStats for the named endpoint's main query."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = build_employee_monthly_pipeline(emp_code, month, num_days)
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        end_date = f"{month}-{num_days:02d}"
        pipeline = build_department_summary_pipeline(month, end_date, department)
        explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        pipeline = build_late_leaderboard_pipeline(month, num_days, limit or 10, department)
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="to must be >= from")
        if (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
        dept_emp_codes = [e["emp_code"] for e in dept_emps]
        from_dt = datetime.combine(d_from, time(0, 0), tzinfo=UTC)
        to_dt_bound = datetime.combine(d_to + timedelta(days=1), time(0, 0), tzinfo=UTC)

        pipeline = build_department_trend_pipeline(
            department, from_date, to_date, from_dt, to_dt_bound, dept_emp_codes
        )
        explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    raise HTTPException(status_code=422, detail="Invalid endpoint")
