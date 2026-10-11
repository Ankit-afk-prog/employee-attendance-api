#	Where	What is wrong	How you fix it
1	compute_late_minutes	Uses UTC time, but shifts are in IST	Build the shift start in IST
2	compute_late_minutes	Rounds to minutes before checking the 10-minute grace, so 09:40:01 counts as on time	Compare in seconds (> 600)
3	compute_late_minutes	Overnight shifts not handled	Apply the overnight rule from R1
4	compute_work_hours	round() is not half-up (4.505 gives 4.5)	Use Decimal with ROUND_HALF_UP
5	compute_overtime	No 30-minute minimum, mixes naive and aware datetimes	Add the minimum, use aware datetimes
6	punch_in		Unknown employee crashes with 500	Return 404
7	punch_in		Uses the server’s local time for the date	Convert to IST
8	punch_in		Check, then insert, so two requests can both succeed	Unique index and handle DuplicateKeyError
9	PunchInIn.status	Accepts any text, even ABSENT	Allow only PRESENT, WFH, ON_DUTY
10	PunchInIn.punched_at	Accepts seconds, floats, strings	Strict integer in a valid millisecond range
11	responses		Returns id and datetimes instead of milliseconds	Build the response by hand
12	create_employee		Same race as #8, and naive time	Unique index, UTC time
13	EmployeeIn		No validation	Add patterns and checks
14	list_employees		Skips page 1, wrong total, no sort	(page-1)*page_size, filtered count, sort
15	list_attendance		Loads everything into memory and sorts in Python	Let MongoDB sort and paginate
16	list_attendance		No input validation	Validate dates, status, page
17	health			Always 	says OK	Ping MongoDB, return 503 if down
18	MongoClient		Returns naive datetimes	tz_aware=True
19	startup	No indexes	Create them at startup