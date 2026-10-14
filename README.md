# Employee Attendance & Analytics API

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat&logo=FastAPI&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat&logo=Python&logoColor=white)](https://www.python.org/)
[![MongoDB](https://img.shields.io/badge/MongoDB-6.0+-47A248.svg?style=flat&logo=MongoDB&logoColor=white)](https://www.mongodb.com/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A high-performance, race-safe REST API for employee attendance tracking, manual record regularization, and multi-dimensional HR analytics built with **FastAPI** and **MongoDB**.

All application logic is implemented in a single file (`app/main.py`), utilizing MongoDB indexes and aggregation pipelines for high throughput on datasets exceeding 100,000 records.

---

## Architecture & Engineering Highlights

- **Pure Database-Driven Analytics**: All analytical reports (`department summary`, `late leaderboard`, `department trend`) are computed directly in MongoDB using native aggregation stages (`$densify`, `$setWindowFields`, `$rank`, `$lookup`), with zero in-memory Python looping.
- **Race Condition Safety**: Prevents duplicate daily punch-ins, duplicate punch-outs, and conflicting employee registrations using unique compound indexes (`(emp_code, date)`) and atomic `find_one_and_update` operations.
- **Explain Endpoint & Index Scans**: Built-in `GET /admin/explain/{endpoint}` executes `explain(verbosity="executionStats")` on production query plans, guaranteeing index scans (`IXSCAN`) and zero collection scans (`COLLSCAN`).
- **Comprehensive Business Rules (R1–R10)**:
  - **R1**: IST timezone (UTC+05:30) date handling with support for overnight shifts crossing midnight and whole-second instant truncation.
  - **R2**: 10-minute grace period with strict floor minute calculations from shift start.
  - **R3**: Overtime calculation with a strict 30-minute threshold across calendar boundaries.
  - **R4 & R8**: Arithmetic half-up rounding (`Decimal.quantize` with `ROUND_HALF_UP`) instead of banker's rounding.
  - **R5 & R6**: Half-day thresholding (< 4.50 hours = 0.5 present day) and presence filtering.
  - **R7 & R9**: Mon–Fri working day calculations accounting for mid-month joiners and department headcounts including zero-log employees.

---

## Project Structure

```text
.
├── app/
│   ├── __init__.py
│   └── main.py              # Consolidated single-file API application
├── sample_data/             # Sample datasets for employees and attendance logs
│   ├── attendance_logs.json
│   └── employees.json
├── tests/                   # Complete integration and concurrency test suite
│   ├── test_all.py
│   └── test_advanced.py
├── sample_seed.py           # Database seeder script
├── requirements.txt         # Runtime dependencies
├── openapi.yaml             # Complete API contract and schema specification
├── DATA_MODEL.md            # MongoDB data model and field definitions
├── REVIEW.md                # Part A audit: 15 defects identified and resolved
├── DECISIONS.md             # Part C audit: Architectural and indexing decisions
└── README.md                # System documentation and instructions
```

---

## Getting Started

### 1. Prerequisites
- **Python 3.11+**
- **MongoDB 6.0+** (Docker or MongoDB Atlas)

### 2. Environment Configuration
The service reads connection details from environment variables with local fallbacks:
```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

### 3. Installation & Setup
```bash
# Clone the repository
git clone https://github.com/Sanket2329/employee-attendance-api.git
cd employee-attendance-api

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 4. Seed the Database
Populate the database with the initial sample dataset:
```bash
python sample_seed.py
```

### 5. Run the API Server
Start the service with `uvicorn`:
```bash
uvicorn app.main:app --port 8000 --reload
```
Once started, the readiness probe is accessible at `http://localhost:8000/health`.

---

## API Documentation & Interactive Exploration

FastAPI provides interactive API documentation out of the box:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

### Key Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service readiness probe (checks live MongoDB ping) |
| `POST` | `/employees` | Registers an employee (unique `emp_code`, validates shift times) |
| `GET` | `/employees` | Lists employees sorted by `emp_code`, with pagination and department filters |
| `POST` | `/attendance/punch-in` | Records punch-in; evaluates shift start and late minutes (R1, R2) |
| `POST` | `/attendance/punch-out` | Records punch-out; computes work hours, overtime, and half-day (R3–R5) |
| `GET` | `/attendance` | Paginated attendance list sorted by `date` desc, `emp_code` asc |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularizes a record; updates derived fields and appends audit trail |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance summary for an employee |
| `GET` | `/analytics/departments/summary` | Department summary with headcount (including zero-log employees) |
| `GET` | `/analytics/leaderboard/late` | Competition-ranked late-comers leaderboard (ties share ranks: 1, 2, 2, 4) |
| `GET` | `/analytics/departments/{department}/trend` | Gap-filled daily trend with 7-day moving averages (`$densify`) |
| `GET` | `/admin/explain/{endpoint}` | Raw MongoDB `explain` executionStats proving index usage |

---

## Verification & Automated Testing

Run the automated test suite covering all business rules, edge cases, window rankings, race conditions, and query plans:

```bash
PYTHONPATH=. pytest -v
```

All 11 test cases will execute and validate the system:
```text
tests/test_advanced.py::test_leaderboard_ties_and_competition_ranking PASSED
tests/test_advanced.py::test_department_summary_zero_logs_and_mid_month_joiners PASSED
tests/test_advanced.py::test_concurrent_punch_ins_and_punch_outs PASSED
tests/test_advanced.py::test_explain_index_scan_and_no_collscan PASSED
tests/test_all.py::test_health PASSED
tests/test_all.py::test_employee_crud_and_validation PASSED
tests/test_all.py::test_punch_in_and_late_calculation PASSED
tests/test_all.py::test_punch_out_and_work_hours PASSED
tests/test_all.py::test_patch_regularize PASSED
tests/test_all.py::test_explain_endpoints PASSED
tests/test_all.py::test_trend_analytics PASSED
```

---

## Design Artifacts

- **[REVIEW.md](REVIEW.md)**: Full audit of 15 defects identified and rectified in the starter code.
- **[DECISIONS.md](DECISIONS.md)**: Clear, concise responses to the 5 system design, indexing, concurrency, and scalability questions.
