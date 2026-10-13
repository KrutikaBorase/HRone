# Employee Attendance & Analytics API

FastAPI service backed by MongoDB 6.0 or newer. It implements employee management, punch-in/out, audited attendance corrections, monthly and department analytics, daily trends, and query-plan inspection. Required indexes are created automatically at startup.

From this directory, install dependencies and configure the MongoDB connection:

```powershell
py -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
py sample_seed.py
py -m uvicorn app.main:app --port 8000
```

The app also reads `MONGO_URI` and `MONGO_DB` from the process environment or a local `.env` file. No credentials or `.env` file belong in the submission.
