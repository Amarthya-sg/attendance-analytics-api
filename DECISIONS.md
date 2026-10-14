Design Decisions

1. Which indexes are created and why?

Employees use unique emp_code, department plus join date, join date, and department plus employee code. Attendance uses unique (emp_code, date), (date desc, emp_code), (status, date desc, emp_code), and (emp_code, punch_in desc). These support natural-key concurrency, employee/department filtering, date-ordered attendance pagination, status filtering, and selecting the latest punch-in. Indexes are created idempotently at startup.

2. What happens when identical punch-ins arrive concurrently?

The API calculates the natural attendance day, then attempts the insert. The unique (emp_code, date) index is the authority: exactly one insert succeeds with 201, while duplicate-key failures become 409. There is no check-then-insert race.

3. What does the leaderboard return for a tie at the cutoff?

It uses standard competition ranking ($rank). The limit is applied to the rank, not the number of rows, so every employee tied at a rank within the cutoff is returned. Rows are finally ordered by total late minutes descending and emp_code ascending.

4. How are employees with no logs counted in department summaries?

The aggregation starts from employees eligible by joined_on, then looks up logs. The employee set is preserved even when the lookup is empty, so headcount includes people with no logs. Log-based metrics contribute zero for those employees; employees who join after the month are excluded.

5. What would change at 100 times the data volume?

I would benchmark explain plans, shard or partition by tenant/period if the product required it, and reduce repeated cross-collection work in trend queries. I would also maintain carefully scoped compound indexes, use bounded projections, monitor execution statistics, and consider pre-aggregated daily metrics for dashboards while keeping raw attendance records authoritative.

