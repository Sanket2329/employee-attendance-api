# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking, manual regularization, and analytics aggregation.

## How to Run

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables (optional)**:
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```
   *(If not set, defaults to `mongodb://localhost:27017` and `attendance_db`)*

3. **Start the API service**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

4. **Run tests**:
   ```bash
   PYTHONPATH=. pytest -v
   ```

## Key Highlights

- **Single Application File**: All application code lives inside `app/main.py`.
- **Database Indexes**: Created idempotently during the FastAPI lifespan startup (`emp_code`, compound `(emp_code, date)`, `(date, emp_code)`, `(joined_on, department)`, etc.).
- **Pure Database Analytics**: All analytics endpoints (`employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`) are implemented exclusively using MongoDB aggregation pipelines (including `$densify` for gap filling and `$setWindowFields` for competition ranking and 7-day moving averages) without looping in Python.
- **Explain Endpoint**: `GET /admin/explain/{endpoint}` executes `explain(verbosity="executionStats")` on the exact queries used by the production endpoints, verifying index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`).
- **Defects & Decisions**: Detailed audit documented in `REVIEW.md` and design decisions answered in `DECISIONS.md`.
