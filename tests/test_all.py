import calendar
from datetime import date, datetime, timedelta, timezone
from fastapi.testclient import TestClient
import pytest
from app.main import app, db, IST

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    # Trigger lifespan to ensure indexes
    with TestClient(app) as tc:
        pass
    yield


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_employee_crud_and_validation():
    # 1. Invalid shift (equal start and end) -> 422
    bad_emp = {
        "emp_code": "EMP9991",
        "name": "Bad Shift",
        "email": "bad@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01"
    }
    res = client.post("/employees", json=bad_emp)
    assert res.status_code == 422

    # 2. Valid create
    valid_emp = {
        "emp_code": "EMP9991",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    db.employees.delete_one({"emp_code": "EMP9991"})
    res = client.post("/employees", json=valid_emp)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9991"
    assert "_id" not in data
    assert isinstance(data["created_at"], int)

    # 3. Duplicate create -> 409
    res_dup = client.post("/employees", json=valid_emp)
    assert res_dup.status_code == 409

    # 4. List employees pagination & department
    res_list = client.get("/employees?department=Testing&page=1&page_size=10")
    assert res_list.status_code == 200
    l_data = res_list.json()
    assert l_data["total"] >= 1
    assert any(e["emp_code"] == "EMP9991" for e in l_data["items"])


def test_punch_in_and_late_calculation():
    # Create test employee with 09:30 shift
    emp_code = "EMP9992"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Late Tester",
        "email": "late@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch-in exactly at 09:40:00 (10 min -> grace period, late = 0)
    dt_grace = datetime(2026, 7, 1, 9, 40, 0, tzinfo=IST)
    ms_grace = int(dt_grace.astimezone(timezone.utc).timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_grace,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 0
    assert res.json()["date"] == "2026-07-01"

    # Duplicate punch-in for same date -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_grace + 1000,
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # Punch-in at 09:40:01 on July 2 (strictly > 10 min -> late = 10 min)
    dt_late = datetime(2026, 7, 2, 9, 40, 1, tzinfo=IST)
    ms_late = int(dt_late.astimezone(timezone.utc).timestamp() * 1000)

    res_late = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": ms_late,
        "status": "PRESENT"
    })
    assert res_late.status_code == 201
    assert res_late.json()["late_minutes"] == 10
    assert res_late.json()["date"] == "2026-07-02"


def test_punch_out_and_work_hours():
    emp_code = "EMP9993"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "PunchOut Tester",
        "email": "po@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in 09:30
    in_dt = datetime(2026, 7, 3, 9, 30, 0, tzinfo=IST)
    in_ms = int(in_dt.astimezone(timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms})

    # Punch out invalid: <= punch_in -> 422
    res_bad = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": in_ms - 1000})
    assert res_bad.status_code == 422

    # Punch out valid: 19:15 (45 min after shift_end -> overtime 45 min, work hours 9.75)
    out_dt = datetime(2026, 7, 3, 19, 15, 0, tzinfo=IST)
    out_ms = int(out_dt.astimezone(timezone.utc).timestamp() * 1000)
    res_out = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms})
    assert res_out.status_code == 200
    data = res_out.json()
    assert data["work_hours"] == 9.75
    assert data["overtime_minutes"] == 45
    assert data["half_day"] is False

    # Second punch-out -> 409
    res_out_2 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_ms + 1000})
    assert res_out_2.status_code == 409


def test_patch_regularize():
    emp_code = "EMP9994"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Patch Tester",
        "email": "patch@example.com",
        "department": "Testing",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in 10:05 (35 min late)
    in_dt = datetime(2026, 7, 4, 10, 5, 0, tzinfo=IST)
    in_ms = int(in_dt.astimezone(timezone.utc).timestamp() * 1000)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms})

    # Regularize to 09:28 (0 late)
    fixed_in_dt = datetime(2026, 7, 4, 9, 28, 0, tzinfo=IST)
    fixed_in_ms = int(fixed_in_dt.astimezone(timezone.utc).timestamp() * 1000)

    res_patch = client.patch(f"/attendance/{emp_code}/2026-07-04", json={
        "punch_in": fixed_in_ms,
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    })
    assert res_patch.status_code == 200
    data = res_patch.json()
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "hr.admin"
    assert "late_minutes" in data["history"][0]["changes"]
    assert data["history"][0]["changes"]["late_minutes"]["from"] == 35
    assert data["history"][0]["changes"]["late_minutes"]["to"] == 0


def test_explain_endpoints():
    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list")
    assert res1.status_code == 200
    assert res1.json()["collection"] == "attendance_logs"
    assert "explain" in res1.json()

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    assert res2.json()["collection"] == "attendance_logs"

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    assert res3.json()["collection"] == "employees"

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200
    assert res4.json()["collection"] == "attendance_logs"

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-10")
    assert res5.status_code == 200
    assert res5.json()["collection"] == "attendance_logs"


def test_trend_analytics():
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 5
