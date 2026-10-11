# DECISIONS.md

1. **Indexes.** On attendance_logs I made four: a unique (emp_code, date) index so one employee
   can have only one record per day; (date, emp_code) for the default list order and month
   queries; (status, date, emp_code) for filtering by status; (emp_code, punch_in) to find the
   open record at punch-out. On employees I made unique emp_code, department, and joined_on
   indexes. I did not copy department into every log, because it can become outdated.

2. **Punch-in race.** Both requests try to insert. The unique index allows only one. The other
   one gets a duplicate error and I return 409.

3. **Ties.** I rank people with $rank, so equal late minutes get the same rank. Then I keep
   everyone with rank <= limit, so a tie at the cutoff returns both people.

4. **Headcount.** My query starts from the employees collection, not from the logs. Employees
   with no logs are kept after the lookup, so they are still counted.

5. **100x data.** I would store a monthly summary per employee, updated on each punch, so
   analytics don't have to scan millions of logs.