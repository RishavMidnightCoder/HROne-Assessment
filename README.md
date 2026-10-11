# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # create a .env file with MONGO_URI and MONGO_DB
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.


## Endpoints Built

## 1. POST /attendance/punch-out
This endpoint is used to punch out an employee. It finds the employee's latest open attendance record and checks whether the punch-out time is valid. If the time is invalid, it returns an error. It updates the record only if it has not already been punched out. After that, it calculates work hours, overtime, and half-day status.

## 2. PATCH /attendance/{emp_code}/{date}
This endpoint is used to correct an existing attendance record. After the correction, it recalculates late minutes, overtime, and half-day status. It stores only the fields that actually changed in the history. If another request updates the record first, it returns a conflict instead of overwriting the other change.

## 3. GET /analytics/employees/{emp_code}/monthly
This endpoint gives the monthly attendance summary for an employee. It groups the attendance records for the selected month and calculates present days, late minutes, and work hours. Working days are calculated based on the employee's joining date. A half-day counts as 0.5 present days. Weekend late minutes and overtime are counted, but weekends do not increase present days.

## 4. GET /analytics/departments/summary
This endpoint gives the attendance summary for each department. It starts from the employees collection so employees without attendance records are also counted. It then uses $lookup to attach attendance records and calculates the department headcount, total work hours, and average work hours.

## 5. GET /analytics/leaderboard/late
This endpoint finds the employees with the highest late minutes. It calculates the total late minutes for each employee, attaches employee details using $lookup, and filters by department when required. It then uses $rank to create the ranking. If employees are tied at the cutoff, all tied employees are returned.

## 6. GET /analytics/departments/{department}/trend
This endpoint gives the daily attendance trend for a department. It calculates attendance for each day and uses $densify to create rows for days where there are no attendance records. It then calculates the daily attendance rate and uses $setWindowFields to calculate the seven-day moving average.

## 7. GET /admin/explain/{endpoint}
This endpoint checks whether the MongoDB queries are using indexes efficiently. It uses MongoDB's explain command with executionStats to show query execution details. IXSCAN means an index is being used, while COLLSCAN means MongoDB scanned the collection. I tested the queries with more than 100,000 records and verified that the analytics queries used IXSCAN without COLLSCAN.
