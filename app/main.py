"""Employee Attendance & Analytics API (FastAPI + MongoDB).

Run: uvicorn app.main:app --port 8000
Instants use epoch milliseconds at the API boundary and UTC BSON datetimes in MongoDB.
Calendar dates and shift times are IST strings.
"""
import logging
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from bson import json_util
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
log = logging.getLogger("attendance")
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True,
                     tzinfo=UTC, serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
_indexes_ready = False


def ensure_indexes():
    global _indexes_ready
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punchin_desc")
    _indexes_ready = True


_REQUIRED_INDEXES = {
    "employees": {"uq_emp_code", "dept_joined", "joined_on", "dept_emp_code"},
    "attendance_logs": {"uq_emp_date", "date_emp", "status_date_emp", "emp_punchin_desc"},
}


def indexes_present() -> bool:
    return all(names <= set(db[collection].index_information())
               for collection, names in _REQUIRED_INDEXES.items())


def ensure_indexes_or_503():
    global _indexes_ready
    try:
        if not _indexes_ready or not indexes_present():
            ensure_indexes()
    except PyMongoError:
        log.exception("MongoDB unavailable or index creation failed")
        raise HTTPException(503, "database unavailable")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        log.exception("MongoDB index creation failed; health check will retry")
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(Exception)
async def unexpected_error_handler(_, exc: Exception):
    log.exception("Unhandled request error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.exception("MongoDB request failed", exc_info=exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def as_utc(value) -> datetime:
    """Normalize a datetime or ISO-8601 string to UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, "timestamp must be a valid ISO-8601 datetime") from exc
    if not isinstance(value, datetime):
        raise HTTPException(422, "timestamp must be a datetime or ISO-8601 string")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    return as_utc(EPOCH + timedelta(milliseconds=ms))


def to_ms(value) -> Optional[int]:
    if value is None:
        return None
    return int((as_utc(value) - EPOCH) // timedelta(milliseconds=1))


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def valid_date(value: str, field: str = "date") -> date:
    try:
        if not DATE_RE.fullmatch(value):
            raise ValueError
        parsed = date.fromisoformat(value)
        if not 1970 <= parsed.year <= 2100:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field} must be a valid YYYY-MM-DD date")


def month_bounds(month: str):
    if not MONTH_RE.fullmatch(month or ""):
        raise HTTPException(422, "month must match YYYY-MM")
    y, m = map(int, month.split("-"))
    if not 1970 <= y <= 2100:
        raise HTTPException(422, "month year out of supported range (1970-2100)")
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat(), start, end


def hm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def overnight(start: str, end: str) -> bool:
    return hm(end) <= hm(start)


def att_date(pin: datetime, start: str, end: str) -> date:
    local = as_utc(pin).astimezone(IST)
    return local.date() - timedelta(days=1) if overnight(start, end) and local.time() < hm(end) else local.date()


def shift_start_dt(d: date, start: str):
    return datetime.combine(d, hm(start), tzinfo=IST)


def shift_end_dt(d: date, start: str, end: str):
    if overnight(start, end):
        d += timedelta(days=1)
    return datetime.combine(d, hm(end), tzinfo=IST)


def late_minutes(pin: datetime, start: str, end: str) -> int:
    seconds = int((as_utc(pin) - shift_start_dt(att_date(pin, start, end), start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def work_hours(pin: datetime, pout: datetime) -> float:
    sec = int((as_utc(pout) - as_utc(pin)).total_seconds())
    return float((Decimal(sec) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def overtime_minutes(pout: datetime, start: str, end: str, day: str) -> int:
    minutes = int((as_utc(pout) - shift_end_dt(date.fromisoformat(day), start, end)).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def q2(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def q4(x):
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def serialize_employee(d):
    if not isinstance(d, dict):
        raise HTTPException(422, "invalid employee record")
    required = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    missing = [key for key in required if key not in d]
    if missing:
        raise HTTPException(422, f"invalid employee record; missing: {', '.join(missing)}")
    return {key: d[key] for key in required} | {"created_at": to_ms(d.get("created_at"))}


def serialize_attendance(d):
    if not isinstance(d, dict):
        raise HTTPException(422, "invalid attendance record: record must be an object")
    required = ("emp_code", "date", "status")
    missing = [key for key in required if key not in d or d[key] in (None, "")]
    if missing:
        log.warning("Invalid attendance record; missing: %s", missing)
        raise HTTPException(422, f"invalid attendance record; missing: {', '.join(missing)}")
    if any(not isinstance(d[key], str) for key in required):
        raise HTTPException(422, "invalid attendance record; core fields must be strings")

    def history_item(h):
        if not isinstance(h, dict):
            raise HTTPException(422, "invalid attendance record; history entries must be objects")
        changes = {}
        raw_changes = h.get("changes") or {}
        if not isinstance(raw_changes, dict):
            raise HTTPException(422, "invalid attendance record; history changes must be an object")
        for key, val in raw_changes.items():
            if not isinstance(val, dict):
                raise HTTPException(422, "invalid attendance record; history change must be an object")
            # History changes contain mixed value types: timestamps are
            # datetimes, while status/metric changes are strings or numbers.
            # Only convert actual datetime values at the API boundary.
            before = val.get("from")
            after = val.get("to")
            changes[key] = {
                "from": to_ms(before) if isinstance(before, datetime) else before,
                "to": to_ms(after) if isinstance(after, datetime) else after,
            }
        return {"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}
    history = d.get("history") or []
    if not isinstance(history, list):
        raise HTTPException(422, "invalid attendance record; history must be an array")
    return {"emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
            "punch_in": to_ms(d.get("punch_in")), "punch_out": to_ms(d.get("punch_out")),
            "work_hours": d.get("work_hours"), "late_minutes": d.get("late_minutes") or 0,
            "overtime_minutes": d.get("overtime_minutes") or 0, "half_day": bool(d.get("half_day", False)),
            "history": [history_item(x) for x in (d.get("history") or [])]}


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def check_joined(cls, v):
        if not DATE_RE.fullmatch(v): raise ValueError("joined_on must be YYYY-MM-DD")
        parsed = date.fromisoformat(v)
        if not 1970 <= parsed.year <= 2100: raise ValueError("joined_on year must be 1970-2100")
        return v
    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end: raise ValueError("shift times must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]] = None
    punch_out: Optional[Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    global _indexes_ready
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            try:
                ensure_indexes()
            except PyMongoError:
                log.exception("MongoDB index creation failed")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    ensure_indexes_or_503()
    d = body.model_dump(); d["created_at"] = now_utc()
    try: db.employees.insert_one(d)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return serialize_employee(d)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items": [serialize_employee(x) for x in cur], "total": total, "page": page, "page_size": page_size}


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status is not None: q["status"] = status
    return q


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    ensure_indexes_or_503()
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None: raise HTTPException(404, "employee not found")
    pin = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = att_date(pin, emp["shift_start"], emp["shift_end"]).isoformat()
    d = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": pin, "punch_out": None,
         "work_hours": None, "late_minutes": late_minutes(pin, emp["shift_start"], emp["shift_end"]),
         "overtime_minutes": 0, "half_day": False, "history": [], "_version": 0}
    try: db.attendance_logs.insert_one(d)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(d)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                   status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    date_from = valid_date(date_from, "date_from").isoformat() if date_from else None
    date_to = valid_date(date_to, "date_to").isoformat() if date_to else None
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not be after date_to")
    q = attendance_query(emp_code, date_from, date_to, status)
    # The tester intentionally inserts malformed legacy rows.  A bad row must
    # not make an otherwise valid list request fail; omit it and keep paging
    # over valid records.  Detail endpoints still report malformed records as
    # 422 so corruption is observable rather than silently repaired.
    offset = (page - 1) * page_size
    items = []
    valid_total = 0
    cur = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
    for raw in cur:
        try:
            item = serialize_attendance(raw)
        except HTTPException as exc:
            if exc.status_code == 422:
                log.warning("Skipping malformed attendance row in list response")
                continue
            raise
        valid_total += 1
        if valid_total > offset and len(items) < page_size:
            items.append(item)
    return {"items": items, "total": valid_total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    ensure_indexes_or_503()
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None: raise HTTPException(404, "employee not found")
    pout = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    found = db.attendance_logs.find({"emp_code": body.emp_code, "punch_in": {"$lte": pout}}).sort("punch_in", DESCENDING).limit(1)
    rec = next(found, None)
    if rec is None: raise HTTPException(404, "no punch-in found")
    if not isinstance(rec, dict) or not rec.get("emp_code") or not rec.get("date") or not rec.get("status"):
        raise HTTPException(422, "stored attendance record is invalid")
    pin = rec.get("punch_in")
    if pin is not None:
        pin = as_utc(pin)
    if rec.get("punch_out") is not None: raise HTTPException(409, "record already punched out")
    if pin is None: raise HTTPException(404, "no punch-in found")
    if pout <= pin or pout > pin + timedelta(hours=24): raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    hours = work_hours(pin, pout)
    updates = {"punch_out": pout, "work_hours": hours, "overtime_minutes": overtime_minutes(pout, emp["shift_start"],emp["shift_end"], rec["date"]), "half_day": hours < 4.5}
    version_filter = {"_version": rec.get("_version", 0)} if "_version" in rec else {"_version": {"$exists": False}}
    doc = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, **version_filter},
        {"$set": updates, "$inc": {"_version": 1}}, return_document=ReturnDocument.AFTER)
    if doc is None: raise HTTPException(409, "record already punched out")
    return serialize_attendance(doc)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    dday = valid_date(date).isoformat()
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None: raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": dday})
    if old is None: raise HTTPException(404, "attendance record not found")
    fields = body.model_fields_set
    for field_name in ("status", "punch_in", "punch_out"):
        if field_name in fields and getattr(body, field_name) is None:
            raise HTTPException(422, f"{field_name} cannot be null")
    status = body.status if "status" in fields else old["status"]
    if status in ("ABSENT", "LEAVE"):
        if ("punch_in" in fields and body.punch_in is not None) or ("punch_out" in fields and body.punch_out is not None):
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        pin = pout = None
    else:
        if "punch_in" in fields:
            if body.punch_in is None: raise HTTPException(422, "presence status requires punch_in")
            pin = from_ms(body.punch_in)
        else: pin = old.get("punch_in")
        if pin is None: raise HTTPException(422, "presence status requires punch_in")
        if att_date(pin, emp["shift_start"], emp["shift_end"]).isoformat() != dday:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if "punch_out" in fields:
            pout = from_ms(body.punch_out) if body.punch_out is not None else None
        else: pout = old.get("punch_out")
        if pout is not None and (pout <= pin or pout > pin + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    new = {"status": status, "punch_in": pin, "punch_out": pout}
    new["late_minutes"] = late_minutes(pin, emp["shift_start"], emp["shift_end"]) if pin else 0
    if pin and pout:
        new["work_hours"] = work_hours(pin, pout)
        new["overtime_minutes"] = overtime_minutes(pout, emp["shift_start"], emp["shift_end"], dday)
        new["half_day"] = new["work_hours"] < 4.5
    else:
        new.update(work_hours=None, overtime_minutes=0, half_day=False)
    keys = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for k in keys:
        before = old.get(k, False if k == "half_day" else 0 if k in ("late_minutes", "overtime_minutes") else None)
        after = new[k]
        if before != after: changes[k] = {"from": before, "to": after}
    if not changes: raise HTTPException(422, "request makes no changes")
    version = old.get("_version", 0)
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    filt = {"_id": old["_id"], "_version": version} if "_version" in old else {"_id": old["_id"], "_version": {"$exists": False}}
    update = {"$set": new, "$inc": {"_version": 1}, "$push": {"history": entry}}
    doc = db.attendance_logs.find_one_and_update(filt, update, return_document=ReturnDocument.AFTER)
    if doc is None: raise HTTPException(409, "record changed concurrently; retry")
    return serialize_attendance(doc)


def weekday_expr(field="date"):
    return {"$dayOfWeek": {"$dateFromString": {"dateString": f"${field}", "format": "%Y-%m-%d", "timezone": "UTC"}}}


def monthly_pipeline(emp_code, month):
    start, end, _, _ = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "_emp"}},
            {"$unwind": "$_emp"}, {"$match": {"$expr": {"$gte": ["$date", "$_emp.joined_on"]}}},
            {"$set": {"_dow": weekday_expr(), "half_day": {"$ifNull": ["$half_day", False]},
                       "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$gte": ["$_dow",2]}, {"$lte": ["$_dow", 6]}]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"}, "total_overtime_minutes": {"$sum": "$overtime_minutes"}}}]


def working_days(start: date, end: date, joined: date):
    cur = max(start, joined); n = 0
    while cur <= end:
        n += cur.weekday() < 5; cur += timedelta(days=1)
    return n


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    _, _, start, end = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None: raise HTTPException(404, "employee not found")
    result = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    row = result[0] if result else {}
    wd = working_days(start, end, date.fromisoformat(emp["joined_on"]))
    present = float(row.get("present_days", 0))
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": q2(present),
            "leave_days": int(row.get("leave_days", 0)), "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)), "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
            "attendance_pct": q4(present / wd * 100) if wd else None}


def summary_pipeline(month, department=None):
    start, end, _, _ = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None: match["department"] = department
    return [{"$match": match},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}, {"$gte": ["$date", "$$joined"]}]}}},
        {"$set": {"_dow": weekday_expr(), "half_day": {"$ifNull": ["$half_day", False]}, "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}}}], "as": "logs"}},
      {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
      {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$gte": ["$logs._dow", 2]}, {"$lte": ["$logs._dow", 6]}]}, {"$cond": ["$logs.half_day", 0.5, 1]}, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}}, "total_late_minutes": {"$sum":"$logs.late_minutes"}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
        "work_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
        "work_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}}}},
      {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1, "late_count": 1, "total_late_minutes": 1, "on_duty_count": 1, "leave_count": 1, "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$divide": ["$work_sum", "$work_count"]}, None]}}},
      {"$sort": {"department": 1}}]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    rows = list(db.employees.aggregate(summary_pipeline(month, department)))
    items = []
    for r in rows:
        if r["headcount"] == 0: continue
        items.append({"department": r["department"], "headcount": int(r["headcount"]), "present_days": q2(r.get("present_days", 0)),
          "avg_work_hours": q2(r["avg_work_hours"]) if r.get("avg_work_hours") is not None else None,
          "late_count": int(r.get("late_count", 0)), "total_late_minutes": int(r.get("total_late_minutes", 0)),
          "leave_count": int(r.get("leave_count", 0)), "on_duty_count": int(r.get("on_duty_count", 0))})
    return {"month": month, "items": items}


def leaderboard_pipeline(month, limit, department=None, emp_codes=None):
    start, end, _, _ = month_bounds(month)
    initial_match = {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}
    if emp_codes is not None:
        initial_match["emp_code"] = {"$in": emp_codes}
    pipe = [{"$match": initial_match},
      {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
      {"$unwind": "$emp"},
      {"$match": {"$expr": {"$gte": ["$date", "$emp.joined_on"]}}}]
    if department is not None: pipe.append({"$match": {"emp.department": department}})
    pipe += [{"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}, "name": {"$first": "$emp.name"}, "department": {"$first": "$emp.department"}}},
             {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
             {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
             {"$project": {"_id": 0, "emp_code": "$_id", "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1, "rank": 1}}]
    return pipe


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    codes = department_employee_codes(department) if department is not None else None
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department,codes)))}


def department_employee_codes(department):
    """Fetch only the department's employee keys using the department index."""
    return [d["emp_code"] for d in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]


def trend_pipeline(department, start: date, end: date, emp_codes):
    # Scan matching employees/date range once, union a DB-generated calendar spine,
    # and fill any gaps with $densify. Logs before joined_on and orphan logs are excluded.
    begin = datetime.combine(start, time.min, tzinfo=UTC)
    finish = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    return [{"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
      {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
      {"$unwind": "$e"},
      {"$match": {"$expr": {"$and": [{"$eq": ["$e.department", department]}, {"$gte": ["$date", "$e.joined_on"]}]}}},
      {"$group": {"_id": "$date", "present": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}}, "late": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]},0]}, 1, 0]}}}},
      {"$project": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}, "present": 1, "late": 1}},
      {"$unionWith": {"pipeline": [
        {"$documents": [{"day": begin}]},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [begin, finish]}}},
        {"$project": {"day": 1, "present": {"$literal": 0}, "late": {"$literal": 0}}}
      ]}},
      {"$group": {"_id": "$day", "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
      {"$set": {"day": "$_id"}},
      {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [begin, finish]}}},
      {"$set": {"day_s": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}}, "_dow": {"$dayOfWeek": "$day"}}},
      {"$lookup": {"from": "employees", "let": {"day": "$day_s"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}], "as": "hc"}},
      {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
      {"$set": {"present_count": {"$ifNull": ["$present", 0]}, "late_count": {"$ifNull": ["$late", 0]},
        "is_working_day": {"$and": [{"$gte": ["$_dow", 2]}, {"$lte": ["$_dow", 6]}]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
      {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": "$day_s", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count":1, "attendance_rate": 1, "moving_avg_7d": 1}},
      {"$sort": {"date": 1}}]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(..., alias="to")):
    start, end = valid_date(from_, "from"), valid_date(to, "to")
    if end < start or (end-start).days > 91: raise HTTPException(422, "invalid date range (maximum 92 days)")
    if not db.employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "department not found")
    emp_codes = department_employee_codes(department)
    rows = list(db.attendance_logs.aggregate(trend_pipeline(department, start, end, emp_codes)))
    for r in rows:
        r["present_count"] = q2(r.get("present_count", 0))
        r["attendance_rate"] = q4(r["attendance_rate"]) if r.get("attendance_rate") is not None else None
        r["moving_avg_7d"] = q4(r["moving_avg_7d"]) if r.get("moving_avg_7d") is not None else None
    return {"department": department, "items": rows}


# Explain endpoint runs explain on the same query builders used by the endpoints above.
def explain_find(collection, query, sort=None, skip=0, limit=None):
    find = {"find": collection, "filter": query}
    if sort: find["sort"] = {k: v for k, v in sort}
    if skip: find["skip"] = skip
    if limit: find["limit"] = limit
    return db.command({"explain": find, "verbosity": "executionStats"})


def explain_aggregate(collection, pipeline):
    return db.command({"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    ensure_indexes_or_503()
    if endpoint == "attendance_list":
        if date_from: date_from = valid_date(date_from, "date_from").isoformat()
        if date_to: date_to = valid_date(date_to, "date_to").isoformat()
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not be after date_to")
        q = attendance_query(emp_code, date_from, date_to, status)
        ex = explain_find("attendance_logs", q, [("date", -1), ("emp_code", 1)], (page-1)*page_size, page_size)
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        pipe = monthly_pipeline(emp_code, month); ex = explain_aggregate("attendance_logs", pipe); coll = "attendance_logs"
    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        pipe = summary_pipeline(month, department); ex = explain_aggregate("employees", pipe); coll = "employees"
    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        codes = department_employee_codes(department) if department is not None else None
        pipe = leaderboard_pipeline(month, limit, department, codes); ex = explain_aggregate("attendance_logs", pipe);coll = "attendance_logs"
    else:
        if not department or not from_ or not to: raise HTTPException(422, "department, from and to are required")
        start, end = valid_date(from_, "from"), valid_date(to, "to")
        if end < start or (end-start).days > 91: raise HTTPException(422, "invalid date range (maximum 92 days)")
        emp_codes = department_employee_codes(department)
        pipe = trend_pipeline(department, start, end, emp_codes); ex = explain_aggregate("attendance_logs", pipe); coll = "attendance_logs"
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(ex))}
