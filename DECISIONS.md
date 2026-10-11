# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

`Answer :-` The indexes I created are uniq_emp_code that prevents duplicate employee codes. uniq_emp_date that prevents two attendance records for one employee/day. dept_emp finds employees by department. joined_on counts employees joined by month end. date_emp helps attendance queries by date and employee code. status_date_emp helps status/date queries. emp_punch_in helps punch-out find the latest punch-in. I did not create late_minutes because the leaderboard add late minutes for every month record.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

`answer :-` If two punch-in requests come at the same time, both may find no record and try to insert. The database is the source of truth. The unique emp_code, date index allows one to succeed and the second to fail with Duplicate Key error.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

`answer :-` If two employees have the same late minutes at the cutoff, I return both using MongoDB $rank. For limit 2, ranks 1,2,2,4 return 3 employees. $limit:2 could remove one tied employee.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?

`answer :-` I start from the employees collection because some employees may have zero attendance logs. I select employees joined by month end, by using $lookup for attendance logs, and group them by department.

5. **One thing you would change** if this had to serve 100x the data.

`answer :-` I would maintain daily summaries and update them whenever punch-in, punch-out, or corrections happen. Analytics would read summaries instead of processing raw attendance logs every time.
