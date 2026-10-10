# DECISIONS.md (about 150-200 words in total - plain language, your own words)

1. **Indexes.** We created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to enforce business uniqueness and guarantee race safety. We added compound indexes `attendance_logs(date: -1, emp_code: 1)` to support fast paginated sorting with `IXSCAN`, `attendance_logs(emp_code: 1, punch_in: -1)` for immediate punch-out lookups, and `employees(department: 1, joined_on: 1)` for headcount filtering. We rejected an index on `(late_minutes)` alone because analytics always scope by date first, making a standalone late index redundant.

2. **Punch-in race.** When two identical requests arrive simultaneously, both compute the attendance date and attempt insertion. The unique compound index on `(emp_code, date)` allows exactly one document to insert atomically (returning 201). The second request triggers MongoDB's `DuplicateKeyError`, which our exception handler catches to return 409 Conflict.

3. **Ties.** Our pipeline uses `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4). The `rank <= limit` filter runs after ranking. If two employees tie at rank 10, both share rank 10 and are returned (yielding 11 total items), ordered by late minutes descending, then employee code ascending.

4. **Headcount.** The department summary aggregation starts from the `employees` collection filtered by `joined_on <= end_of_month`. It groups employees to compute headcount directly before `$lookup`-ing attendance records, ensuring employees with zero logs are preserved.

5. **One thing you would change for 100x data.** At 10 million records, real-time aggregation across large date ranges becomes expensive. We would maintain pre-aggregated daily/monthly summary rollups updated incrementally on punch-out/regularization, and shard `attendance_logs` on `(department, date)`.
