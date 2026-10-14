# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne attendance engineering assignment.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+. Set `MONGO_URI` and `MONGO_DB` in the environment (or in an uncommitted `.env`), then:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000
```

Indexes are created idempotently at startup and checked before write/explain operations. `/health` pings MongoDB and returns 503 when it is unavailable. Dates are accepted for years 1970–2100; instants use epoch milliseconds in the API and UTC BSON datetimes in MongoDB.

## Notes

All documented endpoints are implemented, including MongoDB aggregation analytics and explain output. Writes use unique indexes and optimistic version checks. Local sample-data smoke checks passed; concurrent create, punch-in, and punch-out tests each produced one success and seven 409 responses. A two-edit PATCH race produced one 200, one 409, and exactly one history entry. All five sample explain plans included IXSCAN and no COLLSCAN. Index recreation, repeated startup, malformed month/date and explicit-null PATCH checks passed. Simulated MongoDB unavailability returned 503. The 100k-record load/explain benchmark and full automated rule-boundary suite remain pending. Analytics exclude records dated before `joined_on`.
