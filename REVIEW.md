# REVIEW.md

I read the starter `app/main.py` as if it were a teammate's pull request, then fixed what I found. The first six rows are bugs in code that was already there. Rows 7 to 11 are also defects, mostly in endpoints that were thin or missing, and I fixed them as part of building those endpoints.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The 10-minute grace check works on elapsed whole minutes, so 09:40:01 is treated as on time. It also doesn't anchor the shift start to IST or to the attendance day, so overnight shifts break. | 09:30 shift, punch-in at 09:40:01: expect 10 late minutes, the starter gives 0. Then try an overnight shift. | Truncate to seconds, work out the IST attendance day, build shift start on that day, compare against 600 seconds, then floor to minutes. |
| 2 | `compute_work_hours` | Python's `round()` rounds half to even, but R4 wants half-up. | A duration that lands exactly on a half at the 2nd decimal, e.g. 4.125 h, rounds the wrong way. | Compute whole seconds, divide with `Decimal`, quantize with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Compares against the shift end on the same date, which is wrong for overnight shifts, and counts any positive minute instead of needing at least 30. | Punch out 29 minutes after shift end (should be 0); punch out at 06:45 on a 22:00-06:00 shift. | Put shift end on the correct day and return 0 unless the floored minutes are 30 or more. |
| 4 | Mongo client and startup | Naive datetimes come back from the client and no indexes are created, so nothing stops duplicate employees or duplicate attendance rows. | Fire two identical punch-ins at once, or look at the collection indexes. | `tz_aware=True` client, and idempotent unique indexes on `emp_code` and `(emp_code, date)` at startup, plus the supporting ones. |
| 5 | `health` | Returns ok without touching MongoDB. | Stop MongoDB, `/health` still says ok. | Ping the database and return 503 if it fails. |
| 6 | `create_employee` | The "does it exist" check before insert is racy, `created_at` is naive, the response returns a datetime, and the body isn't validated (codes, dates, shift times). | Two simultaneous creates both pass the check; send `joined_on` as `2026-13-01`. | Validate with Pydantic, let the unique index decide, store UTC, return epoch ms. |
| 7 | `list_employees` | Skips `page * page_size` rows (one page too many), `total` ignores the department filter, no sort order. | Ask for page 1 with a department filter. | Skip `(page - 1) * page_size`, count with the same filter, sort by `emp_code`. |
| 8 | `punch_in` | Unknown employee gives a 500, `punched_at` is read as seconds in local time, no date/status rules, race between two requests. | Unknown code, millisecond timestamp, overnight employee, parallel requests. | Check the employee, parse epoch ms as UTC, derive the IST date, and let the unique index pick the winner (loser gets 409). |
| 9 | `list_attendance` | Pulls every match into Python, sorts there, then slices. No tie-break on the sort. | Seed a large collection and watch time and memory; several employees on one date come back in random order. | Filter, sort (`date` desc, `emp_code` asc), skip and limit in MongoDB with a matching index. |
| 10 | Response serialization | Datetimes (including inside `history`) aren't consistently turned into epoch ms, and seeded docs without `history` or `half_day` break the response shape. | Read a seeded record, or a corrected record's history. | One recursive converter, drop `_id`, default missing fields. |
| 11 | Missing endpoints | Punch-out, regularize, the analytics endpoints and explain didn't exist. | Call any of them. | Built them. Punch-out and regularize use conditional updates so a double punch-out gives one 200 and edits don't overwrite each other's history. Analytics are aggregation pipelines and explain reuses the same builders. |

## Things I looked at and left alone

- The work-hours formula itself (punch-out minus punch-in) is right; only the rounding was wrong.
- The inclusive date bounds in the list filter were fine.
- I didn't expose Mongo's `_id` anywhere. The contract uses `emp_code` as the key, so that's deliberate.
