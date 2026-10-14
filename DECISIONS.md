# DECISIONS.md

1. **Indexes.**
   On `employees`, I created a unique index on `emp_code` (enforcing business key uniqueness) and a compound index `(department, emp_code)` (for filtered, sorted listings). On `attendance_logs`, I created a unique compound index `(emp_code, date)` (guaranteeing one record per day and race-safety), `(date, emp_code)` (for fast pagination), `(emp_code, punch_in)` (for punch-out lookups), and `(date, late_minutes)` (for leaderboard ranking). I rejected indexing `history` because it is an append-only audit trail never queried directly.

2. **Punch-in race.**
   Both concurrent requests validate input and compute the attendance date. Both execute `insert_one`. MongoDB's unique compound index on `(emp_code, date)` guarantees atomic insertion for exactly one request (HTTP 201). The second request triggers a `DuplicateKeyError`, caught by the application handler and cleanly returned as HTTP 409 without server crashes.

3. **Ties.**
   The leaderboard uses MongoDB's `$rank` window function for standard competition ranking (1, 2, 2, 4). When employees tie at the cutoff, they share the identical rank. Because the filter `rank <= limit` applies after ranking, all tied employees are returned, intentionally and safely exceeding `limit` per specification.

4. **Headcount.**
   The aggregation pipeline roots on the `employees` collection (filtering `joined_on <= month_end`), grouping by `department` to establish true department headcount before running a `$lookup` against `attendance_logs`. Employees with zero attendance logs are thus fully counted.

5. **One thing to change for 100x data.**
   I would pre-aggregate daily and monthly department stats into summary rollups using MongoDB change streams, while sharding `attendance_logs` on a hashed `(emp_code, date)` key to distribute high-volume writes across the cluster.
