# Employee Attendance & Analytics API

A FastAPI and MongoDB service for attendance tracking, regularization, and workforce analytics. Built for the HROne Software Engineer hiring assignment (Contract v2.0).

## Table of Contents

- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [API Endpoints](#api-endpoints)
- [Code Overview](#code-overview)
- [Business Rules](#business-rules)

## Quick Start

**Prerequisites:** Python 3.11+ and MongoDB 6.0+

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Once running, the interactive API documentation is available at `http://localhost:8000/docs`.

## Configuration

| Variable    | Description               |
|-------------|---------------------------|
| `MONGO_URI` | MongoDB connection string |
| `MONGO_DB`  | Database name             |

Set both variables in the environment before starting. A local `.env` file is supported, and real environment variables take priority over values in `.env`.

All indexes are created idempotently at startup, so no manual setup is required. The grader can also inject its own database directly.

## API Endpoints

### Health

| Method | Path      | Description                            |
|--------|-----------|----------------------------------------|
| `GET`  | `/health` | Returns 200 or 503 from a MongoDB ping |

### Employees

| Method | Path         | Description                                                              |
|--------|--------------|--------------------------------------------------------------------------|
| `POST` | `/employees` | Create an employee. A unique index prevents duplicates, with 409 on race |
| `GET`  | `/employees` | Filtered, paginated, sorted by `emp_code`                                |

### Attendance

| Method  | Path                            | Description                                                   |
|---------|---------------------------------|---------------------------------------------------------------|
| `POST`  | `/attendance/punch-in`          | R1, R2. Race-safe via unique index                            |
| `POST`  | `/attendance/punch-out`         | R3-R5. Works across midnight                                  |
| `GET`   | `/attendance`                   | Indexed, fast at 100k documents                               |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize with an optimistic version check and audit history |

### Analytics

| Method | Path                                        | Description                                      |
|--------|---------------------------------------------|--------------------------------------------------|
| `GET`  | `/analytics/employees/{emp_code}/monthly`   | Aggregation pipeline (R7-R8)                     |
| `GET`  | `/analytics/departments/summary`            | Cross-collection aggregation (R9 headcount)      |
| `GET`  | `/analytics/leaderboard/late`               | Standard competition ranking via window function |
| `GET`  | `/analytics/departments/{department}/trend` | `$densify` gap-fill with a 7-day moving average  |

### Admin

| Method | Path                        | Description                                            |
|--------|-----------------------------|--------------------------------------------------------|
| `GET`  | `/admin/explain/{endpoint}` | `executionStats` explain. Verified IXSCAN, no COLLSCAN |

## Code Overview

All application code lives in `app/main.py`. It is organized as follows.

### Startup and Indexes

`ensure_indexes()` runs inside the FastAPI lifespan hook and creates all required indexes idempotently. If MongoDB is not available at startup, `/health` retries index creation on the first successful ping. Eight indexes cover uniqueness constraints, list queries, and aggregation lookups.

### Time Handling

All instants enter the API as epoch milliseconds and are immediately converted to UTC-aware datetimes truncated to whole seconds (R1). The helpers `from_ms` and `to_ms` handle this boundary. IST conversion is used only where the business rules require it: deriving the attendance date and computing shift start and end datetimes.

### Business Rule Helpers

`late_minutes`, `work_hours`, and `overtime_minutes` are pure functions implementing R2-R4. They are called at punch-in, punch-out, and after every regularization, so stored derived fields are always current. `q2` and `q4` apply half-up rounding (R8) using `Decimal`.

### Serializers

`serialize_employee` and `serialize_attendance` are the only places where `_id` is dropped and datetimes are converted to epoch milliseconds. In history entries, only `datetime` values are converted to milliseconds. Status and metric changes keep their native types.

### Concurrency Safety

Duplicate punch-ins and duplicate employee creates are rejected by unique indexes, and `DuplicateKeyError` is translated into a 409 response. Punch-out and regularize use `find_one_and_update` with a `_version` field (optimistic concurrency), so two simultaneous writes produce one success and one 409 without a check-then-insert race.

### Analytics Pipelines

All four analytics endpoints are implemented as MongoDB aggregation pipelines.

| Endpoint            | Approach                                                                                                                                                                                                      |
|---------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Employee monthly    | Filters logs by month and `joined_on`, then groups into present, leave, late, and overtime totals. Working days are computed in Python (R7).                                                                   |
| Department summary  | Starts from `employees` so zero-log employees are counted in headcount (R9), joins logs with `$lookup`, and groups by department.                                                                              |
| Late leaderboard    | Groups by `emp_code` and uses `$setWindowFields` with `$rank` for standard competition ranking. Ties share a rank, and the next rank is skipped.                                                               |
| Department trend    | Uses `$densify` to generate one row per calendar day, including weekends and days with no records, then applies `$setWindowFields` with `$avg` over a 7-day document window for the moving average.            |

### Explain Endpoint

`/admin/explain/{endpoint}` rebuilds the exact query or pipeline used by the corresponding real endpoint and runs it with `executionStats` verbosity. It returns the raw MongoDB explain document.

## Business Rules

| Rule | Description                                     |
|------|-------------------------------------------------|
| R1   | IST date, overnight shifts, second truncation   |
| R2   | Late mark with a 10-minute grace period         |
| R3   | Overtime starts at 30 minutes or more           |
| R4   | Work hours with half-up rounding                |
| R5   | Half day when under 4.50 hours                  |
| R6   | Presence statuses                               |
| R7   | Mon-Fri working days, handles mid-month joiners |
| R8   | 2/4 decimal half-up rounding                    |
| R9   | Headcount includes zero-log employees           |
| R10  | Pagination                                      |