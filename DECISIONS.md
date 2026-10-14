# DECISIONS.md

1. **Indexes.**
   On `employees`, I created `{"emp_code": 1}` (unique, enforcing identity uniqueness), `{"department": 1, "emp_code": 1}` (for sorted department listings), `{"joined_on": 1}` and `{"department": 1, "joined_on": 1}` (for headcount filters in analytics). On `attendance_logs`, I created `{"emp_code": 1, "date": 1}` (unique compound index preventing duplicate day records and backing employee lookups), `{"date": -1, "emp_code": 1}` (for default attendance listing pagination), `{"emp_code": 1, "punch_in": -1}` (for punch-out lookups), and `{"date": 1, "late_minutes": 1}` (for leaderboard aggregations). I considered and rejected indexing `history` because it is an append-only audit subdocument never queried directly.

2. **Punch-in race.**
   Both requests validate input and compute the same attendance date. Both issue `db.attendance_logs.insert_one(doc)`. MongoDB's unique compound index `{"emp_code": 1, "date": 1}` guarantees that exactly one insertion succeeds (HTTP 201). The concurrent request encounters a `DuplicateKeyError`, which the exception handler catches and translates cleanly to HTTP 409 without unhandled errors.

3. **Ties.**
   The late leaderboard uses MongoDB's `$rank` window function for standard competition ranking (e.g. 1, 2, 2, 4). When employees tie at the cutoff, all tied employees share the same rank (e.g., rank 2). Because the filter `rank <= limit` is applied *after* ranking, all tied employees are returned. The response may exceed `limit` rows, preserving fairness per the contract.

4. **Headcount.**
   The department summary aggregation pipeline begins on the `employees` collection (filtering by `joined_on <= month_end`), grouping by `department` to calculate true headcount before performing a `$lookup` into `attendance_logs`. Employees with zero attendance logs are thus included in the department headcount.

5. **One thing you would change for 100x data.**
   At 100x scale (~10M logs), cross-collection lookups and daily moving average aggregations across 92 days become expensive. I would pre-aggregate daily and monthly department stats into an incremental rollup collection via change streams, while sharding `attendance_logs` on a hashed key of `(emp_code, date)` to distribute write and query loads across a cluster.
