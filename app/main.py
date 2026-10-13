"""Employee Attendance & Analytics API."""
import json
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="employee_code_unique"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="employee_department_joined",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)],
        name="attendance_trend_date_employee",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_status_employee",
    )


# --------------------------------------------------------------------------- #
# Validation and serialization
# --------------------------------------------------------------------------- #
def validate_date_string(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


def validate_shift_time(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("time must be HH:MM")
    return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("invalid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value: str) -> str:
        return validate_shift_time(value)

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        return validate_date_string(value)

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_null_times(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null")
        for field_name in ("punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


def truncate_seconds(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def datetime_from_epoch_ms(value: Optional[int]) -> datetime:
    if value is None:
        return truncate_seconds(datetime.now(UTC))
    return truncate_seconds(EPOCH + timedelta(milliseconds=value))


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int((instant - EPOCH).total_seconds() * 1000)


def api_value(value):
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, dict):
        return {key: api_value(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [api_value(item) for item in value]
    return value


def attendance_response(doc):
    result = {key: value for key, value in doc.items() if key != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    return api_value(result)


def rounded(value, places: int) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def month_bounds(month: str):
    year, month_number = map(int, month.split("-"))
    try:
        first = date(year, month_number, 1)
        last = date(year, month_number, monthrange(year, month_number)[1])
    except ValueError as exc:
        raise HTTPException(422, "month must be a valid YYYY-MM") from exc
    next_first = last + timedelta(days=1)
    return first.isoformat(), next_first.isoformat(), last.isoformat()


def count_weekdays(start: date, end: date) -> int:
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


# --------------------------------------------------------------------------- #
# Attendance calculations
# --------------------------------------------------------------------------- #
def attendance_date(punch_in: datetime, employee) -> date:
    local = punch_in.astimezone(IST)
    shift_start = time.fromisoformat(employee["shift_start"])
    shift_end = time.fromisoformat(employee["shift_end"])
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < shift_end:
        return local.date() - timedelta(days=1)
    return local.date()


def shift_start_datetime(attendance_day: date, shift_start: str) -> datetime:
    return datetime.combine(attendance_day, time.fromisoformat(shift_start), tzinfo=IST)


def shift_end_datetime(attendance_day: date, employee) -> datetime:
    shift_start = time.fromisoformat(employee["shift_start"])
    shift_end = time.fromisoformat(employee["shift_end"])
    end_day = attendance_day + timedelta(days=1) if shift_end <= shift_start else attendance_day
    return datetime.combine(end_day, shift_end, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, employee, attendance_day: date) -> int:
    start = shift_start_datetime(attendance_day, employee["shift_start"]).astimezone(UTC)
    elapsed_seconds = int((punch_in - start).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return rounded(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, attendance_day: date, employee) -> int:
    shift_end = shift_end_datetime(attendance_day, employee).astimezone(UTC)
    overtime_seconds = int((punch_out - shift_end).total_seconds())
    return overtime_seconds // 60 if overtime_seconds >= 30 * 60 else 0


def derived_values(punch_in: Optional[datetime], punch_out: Optional[datetime], attendance_day: date, employee):
    if punch_in is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    late_minutes = compute_late_minutes(punch_in, employee, attendance_day)
    if punch_out is None:
        return {
            "late_minutes": late_minutes,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": late_minutes,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(punch_out, attendance_day, employee),
        "half_day": work_hours < 4.5,
    }


# --------------------------------------------------------------------------- #
# Shared MongoDB query builders
# --------------------------------------------------------------------------- #
def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


def monthly_employee_pipeline(emp_code: str, month: str):
    first, next_first, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lt": next_first}}},
        {
            "$set": {
                "_weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$in": ["$_weekday", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]):
    first, next_first, last = month_bounds(month)
    employee_match = {"joined_on": {"$lte": last}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", first]},
                                    {"$lt": ["$date", next_first]},
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d",
                                    }
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$in": ["$logs._weekday", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def leaderboard_pipeline(month: str, department: Optional[str], limit: int):
    first, next_first, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lt": next_first}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def trend_pipeline(department: str, from_day: date, to_day: date):
    start = datetime.combine(from_day, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(to_day + timedelta(days=1), time.min, tzinfo=UTC)
    day_count = (to_day - from_day).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$set": {
                "_trend_start": start,
                "_trend_dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$_trend_dates"},
        {
            "$group": {
                "_id": "$_trend_dates",
                "headcount": {
                    "$sum": {
                        "$cond": [
                            {
                                "$lte": [
                                    "$joined_on",
                                    {
                                        "$dateToString": {
                                            "date": "$_trend_dates",
                                            "format": "%Y-%m-%d",
                                        }
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "employee_codes": {"$addToSet": "$emp_code"},
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {"step": 1, "unit": "day", "bounds": [start, end_exclusive]},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "trend_date": {
                        "$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}
                    },
                    "codes": {"$ifNull": ["$employee_codes", []]},
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$trend_date"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_logs",
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},
                "headcount": {"$ifNull": ["$headcount", 0]},
                "present_count": {
                    "$ifNull": [{"$arrayElemAt": ["$daily_logs.present_count", 0]}, 0]
                },
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily_logs.late_count", 0]}, 0]},
                "is_working_day": {"$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            "$headcount",
                                                        ]
                                                    },
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {"$multiply": ["$moving_avg_7d", 10000]},
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Health and employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return api_value(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": api_value(items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = datetime_from_epoch_ms(body.punched_at)
    attendance_day = attendance_date(punched_at, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee, attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    doc["_id"] = result.inserted_id
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = datetime_from_epoch_ms(body.punched_at)
    existing = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if existing is None:
        raise HTTPException(404, "no punch-in found")
    if existing.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_at = truncate_seconds(existing["punch_in"])
    duration = punched_at - punch_in_at
    if duration <= timedelta(0) or duration > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    attendance_day = date.fromisoformat(existing["date"])
    derived = derived_values(punch_in_at, punched_at, attendance_day, employee)
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": existing["_id"],
            "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
        },
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    from_string = date_from.isoformat() if date_from else None
    to_string = date_to.isoformat() if date_to else None
    query = attendance_filter(emp_code, from_string, to_string, status)
    total = db.attendance_logs.count_documents(query)
    docs = list(
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_response(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn):
    day_string = date.isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": day_string})
    if original is None:
        raise HTTPException(404, "attendance record not found")

    changes_requested = body.model_fields_set
    final_status = body.status if "status" in changes_requested else original["status"]
    final_punch_in = (
        datetime_from_epoch_ms(body.punch_in)
        if "punch_in" in changes_requested
        else original.get("punch_in")
    )
    final_punch_out = (
        datetime_from_epoch_ms(body.punch_out)
        if "punch_out" in changes_requested
        else original.get("punch_out")
    )

    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_requested or "punch_out" in changes_requested:
            raise HTTPException(422, "ABSENT and LEAVE cannot include punch times")
        final_punch_in = None
        final_punch_out = None
    else:
        if final_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(final_punch_in, employee).isoformat() != day_string:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if final_punch_out is not None:
            duration = final_punch_out - final_punch_in
            if duration <= timedelta(0) or duration > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    updates = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        **derived_values(final_punch_in, final_punch_out, date, employee),
    }
    actual_changes = {}
    for field_name, new_value in updates.items():
        default_value = {
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "work_hours": None,
            "punch_in": None,
            "punch_out": None,
        }.get(field_name)
        old_value = original.get(field_name, default_value)
        if old_value != new_value:
            actual_changes[field_name] = {"from": old_value, "to": new_value}
    if not actual_changes:
        raise HTTPException(422, "request does not change the record")

    history_entry = {
        "at": truncate_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": actual_changes,
    }
    filter_query = {"_id": original["_id"]}
    if "history" in original:
        filter_query["history"] = original["history"]
    else:
        filter_query["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        {"$set": updates, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry the correction")
    return attendance_response(updated)


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")
    first, _, last = month_bounds(month)
    joined = date.fromisoformat(employee["joined_on"])
    working_start = max(joined, date.fromisoformat(first))
    working_days = (
        count_weekdays(working_start, date.fromisoformat(last))
        if working_start <= date.fromisoformat(last)
        else 0
    )
    result = list(db.attendance_logs.aggregate(monthly_employee_pipeline(emp_code, month)))
    stats = result[0] if result else {}
    present_days = rounded(stats.get("present_days", 0), 2)
    attendance_pct = (
        rounded(present_days / working_days * 100, 2) if working_days else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    items = []
    for row in rows:
        if row["headcount"] == 0:
            continue
        row["present_days"] = rounded(row["present_days"], 2)
        if row["avg_work_hours"] is not None:
            row["avg_work_hours"] = rounded(row["avg_work_hours"], 2)
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Month,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(
        db.attendance_logs.aggregate(leaderboard_pipeline(month, department, limit))
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_day: date = Query(alias="from"),
    to_day: date = Query(alias="to"),
):
    if to_day < from_day:
        raise HTTPException(422, "to must be on or after from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    rows = list(
        db.employees.aggregate(trend_pipeline(department, from_day, to_day))
    )
    for row in rows:
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = rounded(row["moving_avg_7d"], 4)
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Explain endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_day: Optional[date] = Query(default=None, alias="from"),
    to_day: Optional[date] = Query(default=None, alias="to"),
):
    if endpoint == "attendance_list":
        query = attendance_filter(
            emp_code,
            date_from.isoformat() if date_from else None,
            date_to.isoformat() if date_to else None,
            status,
        )
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        explanation = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": monthly_employee_pipeline(emp_code, month),
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        explanation = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": department_summary_pipeline(month, department),
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": leaderboard_pipeline(month, department, limit),
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if department is None or from_day is None or to_day is None:
            raise HTTPException(422, "department, from, and to are required")
        if to_day < from_day or (to_day - from_day).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        explanation = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": trend_pipeline(department, from_day, to_day),
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection = "employees"
    else:
        raise HTTPException(422, "unknown endpoint")
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explanation)),
    }
