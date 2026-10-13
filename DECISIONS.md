# DECISIONS.md

1. **Indexes.** Unique indexes on `emp_code` and on `(emp_code, date)` stop duplicate employees and duplicate daily records even under concurrency. Department plus `joined_on` supports headcount, and date-based compound indexes support sorted pages and month scans. I skipped indexes on derived fields like `late_minutes`; analytics scan a date range anyway.

2. **Identical punch-ins.** Both requests find the employee and work out the same attendance date, then both try to insert the same `(emp_code, date)`. The unique index lets one through; the other gets a duplicate-key error, which I return as 409.

3. **Tie at the cutoff.** Ties share a rank and the next rank is skipped (1, 2, 2, 4). I keep every row whose rank is within `limit`, so a tie at the cutoff can return more than `limit` rows. Rows are ordered by total minutes, then `emp_code`.

4. **No-log employees.** The department pipeline starts from employees who joined by month end, then joins their logs. Someone with no logs still counts once toward headcount and adds nothing to the other totals.

5. **100x data.** I'd check the explain plans on realistic data and precompute monthly summaries, keeping raw logs as the source of truth.
