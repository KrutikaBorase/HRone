# DECISIONS.md

1. **Indexes.** Unique employee-code and `(emp_code, date)` indexes prevent duplicate employees and attendance records under concurrency. Department/join-date indexes support headcount and trends; attendance compound indexes support monthly queries, sorted pages, and date scans. I rejected separate indexes on derived values such as `late_minutes`: analytics group a date range, so those indexes add write cost without avoiding that work.

2. **Punch-in race.** Both requests look up the employee and calculate the same attendance date. Both attempt the same natural key. MongoDB's unique index accepts one insert; the other's `DuplicateKeyError` becomes 409. Correctness does not depend on a pre-insert check.

3. **Ties.** Competition ranking assigns tied employees the same rank and skips the following rank. Filtering on `rank <= limit` returns every employee tied at the cutoff, then orders by total minutes and employee code.

4. **Headcount.** The department aggregation begins with employees who joined by month end and left-joins monthly logs. An employee with no logs remains in the pipeline and counts once.

5. **100x data.** I would benchmark explain plans with production-like distributions and precompute recurring monthly summaries, keeping raw logs as the source of truth.
