import concurrent.futures
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import pytest
from app.main import app, db, IST

client = TestClient(app)


def test_leaderboard_ties_and_competition_ranking():
    # Setup test data with ties:
    # EMP1001: 50 late min
    # EMP1002: 30 late min
    # EMP1003: 30 late min (tie with EMP1002)
    # EMP1004: 10 late min
    db.employees.delete_many({"emp_code": {"$in": ["EMP1001", "EMP1002", "EMP1003", "EMP1004"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP1001", "EMP1002", "EMP1003", "EMP1004"]}})

    for code, name in [
        ("EMP1001", "Alice"),
        ("EMP1002", "Bob"),
        ("EMP1003", "Charlie"),
        ("EMP1004", "David")
    ]:
        client.post("/employees", json={
            "emp_code": code,
            "name": name,
            "email": f"{code.lower()}@test.com",
            "department": "TieDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

    # Insert attendance logs directly to simulate late minutes
    db.attendance_logs.insert_many([
        {"emp_code": "EMP1001", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 50},
        {"emp_code": "EMP1002", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 30},
        {"emp_code": "EMP1003", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 30},
        {"emp_code": "EMP1004", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 10},
    ])

    # Query leaderboard with limit=2
    # Ranks should be: EMP1001 -> 1, EMP1002 -> 2, EMP1003 -> 2, EMP1004 -> 4
    # Because limit=2 is applied AFTER ranking (rank <= limit), both rank 1 and rank 2 items are returned (3 total items!)
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2&department=TieDept")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP1001" and items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP1002" and items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP1003" and items[2]["rank"] == 2


def test_department_summary_zero_logs_and_mid_month_joiners():
    # Dept: HeadcountDept
    # EMP2001: joined 2026-06-01 (has 1 log)
    # EMP2002: joined 2026-07-15 (has 0 logs, should still be counted in July headcount)
    # EMP2003: joined 2026-08-01 (joined after July, must NOT be counted in July headcount)
    db.employees.delete_many({"emp_code": {"$in": ["EMP2001", "EMP2002", "EMP2003"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP2001", "EMP2002", "EMP2003"]}})

    for code, joined in [("EMP2001", "2026-06-01"), ("EMP2002", "2026-07-15"), ("EMP2003", "2026-08-01")]:
        client.post("/employees", json={
            "emp_code": code,
            "name": f"User {code}",
            "email": f"{code.lower()}@test.com",
            "department": "HeadcountDept",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined
        })

    # Insert only 1 log for EMP2001 on a weekday (2026-07-06 Mon)
    db.attendance_logs.insert_one({
        "emp_code": "EMP2001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 8.5,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False
    })

    res = client.get("/analytics/departments/summary?month=2026-07&department=HeadcountDept")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    dept = items[0]
    assert dept["department"] == "HeadcountDept"
    assert dept["headcount"] == 2  # EMP2001 and EMP2002 (EMP2003 excluded)
    assert dept["present_days"] == 1.0
    assert dept["avg_work_hours"] == 8.5


def test_concurrent_punch_ins_and_punch_outs():
    emp_code = "EMP3001"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Race Tester",
        "email": "race@test.com",
        "department": "RaceDept",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    dt_in = datetime(2026, 7, 7, 9, 30, 0, tzinfo=IST)
    ms_in = int(dt_in.astimezone(timezone.utc).timestamp() * 1000)

    # 1. Concurrent punch-ins
    def do_punch_in():
        c = TestClient(app)
        return c.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": ms_in}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_in) for _ in range(5)]
        statuses = [f.result() for f in futures]

    assert statuses.count(201) == 1
    assert statuses.count(409) == 4

    # 2. Concurrent punch-outs
    dt_out = datetime(2026, 7, 7, 18, 30, 0, tzinfo=IST)
    ms_out = int(dt_out.astimezone(timezone.utc).timestamp() * 1000)

    def do_punch_out():
        c = TestClient(app)
        return c.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": ms_out}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        out_statuses = [f.result() for f in futures]

    assert out_statuses.count(200) == 1
    assert out_statuses.count(409) == 4


def test_explain_index_scan_and_no_collscan():
    # Helper to scan for COLLSCAN in raw explain dict
    def has_collscan(obj):
        if isinstance(obj, dict):
            if obj.get("stage") == "COLLSCAN":
                return True
            return any(has_collscan(v) for v in obj.values())
        elif isinstance(obj, list):
            return any(has_collscan(item) for item in obj)
        return False

    def has_ixscan(obj):
        if isinstance(obj, dict):
            if obj.get("stage") == "IXSCAN":
                return True
            return any(has_ixscan(v) for v in obj.values())
        elif isinstance(obj, list):
            return any(has_ixscan(item) for item in obj)
        return False

    endpoints_to_test = [
        "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-10",
    ]

    for ep in endpoints_to_test:
        res = client.get(ep)
        assert res.status_code == 200
        data = res.json()
        explain_tree = data["explain"]
        # queryPlanner or executionStats must contain IXSCAN and NO COLLSCAN
        assert has_ixscan(explain_tree), f"Expected IXSCAN for {ep}"
        assert not has_collscan(explain_tree), f"Found unwanted COLLSCAN for {ep}"
