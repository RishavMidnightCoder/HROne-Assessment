"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from bson import json_util
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False by default, so real environment variables win

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                   # datetimes come back as UTC-aware, never naive (DATA_MODEL.md hint)
    serverSelectionTimeoutMS=5000,   # fail in 5s instead of hanging when MongoDB is down
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000   # contract's EpochMillis range


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


def _must_be_int(v):
    # type() check, not isinstance: rejects "123" strings, 1.0 floats and True/False.
    if type(v) is not int:
        raise ValueError("must be an integer number of epoch milliseconds")
    return v


# Contract's EpochMillis: integer between 1e11 (rejects seconds-sized values and 0) and 4.1e12.
EpochMs = Annotated[int, BeforeValidator(_must_be_int), Field(ge=MIN_MS, le=MAX_MS)]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current time as a UTC-aware datetime, trimmed to milliseconds (BSON precision)."""
    n = datetime.now(timezone.utc)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime from MongoDB -> integer epoch milliseconds (the API format)."""
    if dt is None:
        return None
    if dt.tzinfo is None:                      # defensive: Mongo stores UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> UTC-aware datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start            # zero-padded HH:MM strings compare correctly


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: the IST calendar date of the punch-in; for an overnight shift, a punch-in
    earlier than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    h, m = map(int, emp["shift_end"].split(":"))
    if is_overnight(emp["shift_start"], emp["shift_end"]) and local.time() < time(h, m):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    d = date.fromisoformat(date_str)
    h, m = map(int, shift_start.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    d = date.fromisoformat(date_str)
    if is_overnight(shift_start, shift_end):
        d += timedelta(days=1)                 # an overnight shift ends on the next calendar day
    h, m = map(int, shift_end.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then whole minutes since shift start."""
    secs = int((punch_in - shift_start_dt(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals HALF-UP (Decimal, not float round())."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only when at least 30."""
    end = shift_end_dt(date_str, shift_start, shift_end)
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]      # the order the contract asks for


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """Validates the filters and builds the Mongo query. Shared by GET /attendance and its explain."""
    date_from, date_to = check_date(date_from, "date_from"), check_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
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
    return q


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    model_config = {
        "json_schema_extra": {
            "example": {
                "emp_code": "EMP0010",
                "name": "Test One",
                "email": "test.one@example.com",
                "department": "QA",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-07-01",
            }
        }
    }

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def must_be_real_date(cls, v: str) -> str:
        date.fromisoformat(v)      # raises ValueError for 2026-02-30, which becomes a 422
        return v

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"      # ABSENT / LEAVE are not created by punch-in

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None    

class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)    


# --------------------------------------------------------------------------- #
# Response serializers (whitelists: _id can never leak, instants become epoch ms)
# --------------------------------------------------------------------------- #
def employee_out(d: dict) -> dict:
    out = {k: d[k] for k in
           ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    out["created_at"] = dt_to_ms(d["created_at"])
    return out


def _history_out(h: dict) -> dict:
    changes = {}
    for key, v in h.get("changes", {}).items():
        changes[key] = {
            side: dt_to_ms(v.get(side)) if isinstance(v.get(side), datetime) else v.get(side)
            for side in ("from", "to")
        }
    return {"at": dt_to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes}


def attendance_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes", 0),          # legacy records may omit these
        "overtime_minutes": d.get("overtime_minutes", 0),
        "half_day": d.get("half_day", False),
        "history": [_history_out(h) for h in d.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes():
    # Uniqueness enforced by the database, so concurrent duplicates cannot both succeed.
    db.employees.create_index("emp_code", unique=True, name="uniq_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], unique=True, name="uniq_emp_date")
    # GET /attendance sorts by date desc, emp_code asc, with or without a status filter.
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp")
    # Punch-out finds an employee's latest punch-in at or before a given time.
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="emp_punch_in")

    # Department summary starts from employees filtered by joined_on (R9).
    db.employees.create_index([("joined_on", 1)], name="joined_on")


@asynccontextmanager
async def lifespan(_app):
    try:
        ensure_indexes()               # idempotent: same spec and name is a no-op
    except PyMongoError as e:
        print(f"WARNING: could not create indexes at startup: {e}")
    yield


# lifespan must be defined before this line, which is why app is created here.
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)



# --------------------------------------------------------------------------- #
# Analytics (R5-R9): month helpers, aggregation pipelines
# --------------------------------------------------------------------------- #
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
MAX_TREND_DAYS = 92
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]                 # R6: statuses that count as present
MonthQ = Annotated[str, Query(pattern=MONTH_RE, description="Calendar month, YYYY-MM")]


def month_bounds(month: str) -> tuple[str, str]:
    """First and last calendar day of 'YYYY-MM', as 'YYYY-MM-DD' strings."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
        last_day = calendar.monthrange(year, mon)[1]
        return date(year, mon, 1).isoformat(), date(year, mon, last_day).isoformat()
    except ValueError:                                    # e.g. year 0000
        raise HTTPException(422, "month must be a valid YYYY-MM")


def count_working_days(start: str, end: str) -> int:
    """R7: number of Mon-Fri days in [start, end], inclusive. No holiday calendar."""
    d, last, n = date.fromisoformat(start), date.fromisoformat(end), 0
    while d <= last:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def round_half_up(value, places: int = 2) -> float:
    """R8: half-up rounding on exact decimals. MongoDB's $round and Python's round() round halves to even."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _is_weekday(date_expr):
    """Aggregation expression: is this 'YYYY-MM-DD' string a Monday to Friday?"""
    day = {"$isoDayOfWeek": {"$dateFromString": {"dateString": date_expr, "format": "%Y-%m-%d"}}}
    return {"$lte": [day, 5]}


def _present_value(prefix: str, weekdays_only: bool = True):
    """One record's contribution to present days (R5-R6): 1, or 0.5 for a half day, only for a
    presence status. weekdays_only=True (R7) is for present_days; the trend's present_count
    passes False because it counts weekend records too."""
    present = {"$in": [prefix + "status", PRESENCE]}
    cond = {"$and": [present, _is_weekday(prefix + "date")]} if weekdays_only else present
    return {"$cond": [
        cond,
        {"$cond": [{"$eq": [{"$ifNull": [prefix + "half_day", False]}, True]}, 0.5, 1]},
        0,
    ]}


def build_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_value("$")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary_pipeline(first: str, last: str, department: Optional[str] = None) -> list:
    emp_match = {"joined_on": {"$lte": last}}             # R9: joined on or before the last day of the month
    if department is not None:
        emp_match["department"] = department
    L = "$$l."

    def over_logs(expr):
        return {"$map": {"input": "$logs", "as": "l", "in": expr}}

    late = {"$ifNull": [L + "late_minutes", 0]}
    has_hours = {"$and": [{"$in": [L + "status", PRESENCE]}, {"$isNumber": L + "work_hours"}]}
    return [
        {"$match": emp_match},
        {"$lookup": {                                      # every employee keeps a (possibly empty) logs array
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                              "late_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {                                     # per-employee numbers, one row per employee
            "department": 1,
            "present": {"$sum": over_logs(_present_value(L))},
            "late_count": {"$sum": over_logs({"$cond": [{"$gt": [late, 0]}, 1, 0]})},
            "late_minutes": {"$sum": over_logs(late)},
            "leave_count": {"$sum": over_logs({"$cond": [{"$eq": [L + "status", "LEAVE"]}, 1, 0]})},
            "on_duty_count": {"$sum": over_logs({"$cond": [{"$eq": [L + "status", "ON_DUTY"]}, 1, 0]})},
            "hours_n": {"$sum": over_logs({"$cond": [has_hours, 1, 0]})},
            # hours summed as integer hundredths, so the later average has no floating-point drift
            "hours_cents": {"$sum": over_logs({"$cond": [
                has_hours, {"$round": [{"$multiply": [L + "work_hours", 100]}, 0]}, 0]})},
        }},
        {"$group": {                                       # one row per department; headcount = employees
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "hours_n": {"$sum": "$hours_n"},
            "hours_cents": {"$sum": "$hours_cents"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str] = None) -> list:
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": late},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},    # only employees with a total above 0
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                               # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})   # filter BEFORE ranking
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},             # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def prepare_trend(department: str, from_: str, to: str):
    """Validates the range and loads the department's employees. Shared by the trend endpoint and its explain."""
    first, last = check_date(from_, "from"), check_date(to, "to")
    if last < first:
        raise HTTPException(422, "to must be >= from")
    if (date.fromisoformat(last) - date.fromisoformat(first)).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"range must not exceed {MAX_TREND_DAYS} days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "department not found")
    return first, last, [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


def build_trend_pipeline(codes: list, joined: list, first: str, last: str) -> list:
    """Daily trend for ONE department. codes/joined are the department's emp_codes and joined_on dates."""
    start = datetime.fromisoformat(first).replace(tzinfo=timezone.utc)
    end_excl = datetime.fromisoformat(last).replace(tzinfo=timezone.utc) + timedelta(days=1)
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        # 1. only this department's logs inside the range (served by an index)
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": first, "$lte": last}}},
        # 2. one row per day that has records
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": _present_value("$", weekdays_only=False)},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        # 3. $densify needs a real date field, not a string
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # 4. gap filling: one row for EVERY day from `from` to `to`; upper bound is exclusive
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end_excl]}}},
        # 5. label the day; the filled rows have no counts, so default them to 0
        {"$addFields": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
        }},
        # 6. R9 headcount: employees whose joined_on is on or before that day
        {"$addFields": {"headcount": {"$size": {"$filter": {
            "input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}}},
        # 7. rate only on working days with headcount > 0, otherwise null
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # 8. moving average over this row and up to 6 earlier rows; $avg skips nulls
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                                                           "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


# --------------------------------------------------------------------------- #
# Admin: explain helper
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary",
                        "late_leaderboard", "department_trend"]


def _require(**params):
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(422, "missing required query parameter(s): " + ", ".join(missing))


def _explain(command: dict) -> dict:
    """Run explain with executionStats; json_util turns BSON types (dates, Int64, ...) into plain JSON."""
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))




# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)           # the unique index rejects duplicates atomically
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)                 # total of the FILTERED set (R10)
    cur = (db.employees.find(q)
           .sort("emp_code", 1)                              # stable order, so pages never overlap
           .skip((page - 1) * page_size)
           .limit(page_size))
    return {"items": [employee_out(d) for d in cur],
            "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc().replace(microsecond=0)
    d = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)                   # unique (emp_code, date) index decides races
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    out = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc().replace(microsecond=0)

    # Most recent record whose punch_in is at or before the punch-out. No date logic,
    # so it works across midnight. Records with a null punch_in never match a range query.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if out <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if out - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    hours = compute_work_hours(pin, out)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},          # only succeeds if the record is still open
        {"$set": {
            "punch_out": out,
            "work_hours": hours,
            "overtime_minutes": compute_overtime(out, rec["date"], emp["shift_start"], emp["shift_end"]),
            "half_day": hours < 4.50,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:                                   # someone else closed it first
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


def check_date(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")
    return value


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)            # total of the FILTERED set (R10)
    cur = (db.attendance_logs.find(q)
           .sort(ATTENDANCE_SORT)                            # the exact order the contract asks for
           .skip((page - 1) * page_size)
           .limit(page_size))                                # database does the work, not Python
    return {"items": [attendance_out(d) for d in cur],
            "total": total, "page": page, "page_size": page_size}



@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # `date` shadows the datetime.date class inside this function only. It is safe because
    # the class is never used here; the string is validated and renamed straight away.
    att_date = check_date(date, "date")
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(422, "nothing to change")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # ---- Work out the final state of the record -------------------------------------
    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot carry punch times")
        new_in = new_out = None                      # these statuses clear both punch times
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp) != att_date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # ---- Recompute derived fields from the FINAL times (R2-R5) ------------------------
    late = compute_late_minutes(new_in, att_date, emp["shift_start"]) if new_in is not None else 0
    if new_out is not None:
        hours = compute_work_hours(new_in, new_out)
        overtime = compute_overtime(new_out, att_date, emp["shift_start"], emp["shift_end"])
        half_day = hours < 4.50
    else:
        hours, overtime, half_day = None, 0, False

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           "work_hours": hours, "late_minutes": late,
           "overtime_minutes": overtime, "half_day": half_day}
    old = {"status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes", 0),
           "overtime_minutes": rec.get("overtime_minutes", 0), "half_day": rec.get("half_day", False)}

    # ---- Only fields that actually changed go into history ----------------------------
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not ({"status", "punch_in", "punch_out"} & changes.keys()):
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- Atomic write, guarded against a concurrent edit ------------------------------
    guard = {"_id": rec["_id"], "status": old["status"],
             "punch_in": old["punch_in"], "punch_out": old["punch_out"]}
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:                               # someone else changed the record first
        raise HTTPException(409, "record was modified concurrently, retry")
    return attendance_out(updated)



# --------------------------------------------------------------------------- #
# Analytics: endpoints
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: MonthQ):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rows = list(db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, first, last)))
    a = rows[0] if rows else {}
    present = a.get("present_days", 0)
    start = max(first, emp["joined_on"])                   # R7: count working days from joined_on
    working = count_working_days(start, last) if start <= last else 0
    pct = round_half_up(Decimal(str(present)) / Decimal(working) * 100, 2) if working else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": round_half_up(present, 2),
        "leave_days": int(a.get("leave_days", 0)),
        "late_count": int(a.get("late_count", 0)),
        "total_late_minutes": int(a.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(a.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: MonthQ, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(build_department_summary_pipeline(first, last, department)):
        n = int(r["hours_n"])
        avg = None
        if n:
            avg = round_half_up(Decimal(int(round(r["hours_cents"]))) / Decimal(100) / Decimal(n), 2)
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: MonthQ,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    rows = db.attendance_logs.aggregate(build_leaderboard_pipeline(first, last, limit, department))
    items = [{
        "rank": int(r["rank"]),
        "emp_code": r["emp_code"],
        "name": r["name"],
        "department": r["department"],
        "total_late_minutes": int(r["total_late_minutes"]),
        "late_count": int(r["late_count"]),
    } for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),       # `from` is a Python keyword, so the parameter is aliased
    to: str = Query(...),
):
    first, last, codes, joined = prepare_trend(department, from_, to)

    items = []
    for r in db.attendance_logs.aggregate(build_trend_pipeline(codes, joined, first, last)):
        rate, avg = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(avg, 4) if avg is not None else None,
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = Query(None, example="EMP0001"),
    month: Optional[str] = Query(None, pattern=MONTH_RE, example="2026-06"),
    department: Optional[str] = Query(None, example="Engineering"),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, example="2026-06-01"),
    date_to: Optional[str] = Query(None, example="2026-06-30"),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", example="2026-05-01"),
    to: Optional[str] = Query(None, example="2026-07-31"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {"find": collection,
                   "filter": build_attendance_filter(emp_code, date_from, date_to, status),
                   "sort": dict(ATTENDANCE_SORT),
                   "skip": (page - 1) * page_size,
                   "limit": page_size}
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": build_monthly_pipeline(emp_code, first, last)}
    elif endpoint == "department_summary":
        _require(month=month)
        first, last = month_bounds(month)
        collection = "employees"                       # this pipeline starts from employees
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": build_department_summary_pipeline(first, last, department)}
    elif endpoint == "late_leaderboard":
        _require(month=month)
        first, last = month_bounds(month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": build_leaderboard_pipeline(first, last, limit, department)}
    else:  # department_trend
        _require(department=department, from_=from_, to=to)
        first, last, codes, joined = prepare_trend(department, from_, to)
        collection = "attendance_logs"
        command = {"aggregate": collection, "cursor": {},
                   "pipeline": build_trend_pipeline(codes, joined, first, last)}

    return {"endpoint": endpoint, "collection": collection, "explain": _explain(command)}


