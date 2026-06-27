# AttendaSync — Bulk Attendance Marker

Marks Zoom webinar attendance into a **Master Batch Roster** automatically.

You give it three things:

1. **Master Batch Roster** (`.xlsx`) — one sheet per batch (`AI CAP B17` … `B28`),
   columns A–J = student data. The app finds **Registered mail** and **Registered
   Number** by header name, so shifted layouts work.
2. **L2 Weekly Sessions** (`.xlsx`) — the schedule. Read from every month tab:
   **Batch Name**, **Topic Name**, **Webinar ID**.
3. **Zoom Attendee Reports** (`.zip`) — the raw `attendee_<id>_<date>.csv` exports
   (sub-folders fine).

It links each attendee file to its **batch(es)**, date and topic via the **Webinar ID**
(falling back to the folder name if a webinar isn't in L2 yet), then in each covered batch
sheet it re-marks the matching date column or adds a new one — **Present** if the student's
Registered mail or Registered Number matches the attendee list, else **Absent**. Blank
cells never match. Sessions with no file are left untouched.

**Shared sessions are marked in every batch.** A webinar listed for multiple batches
(`AI CAP B2 + B3 + B4`, or a range `AI CAP B1 - AI CAP B22`) is marked in each batch that
has a sheet in your roster. If a session maps to a batch with no sheet in the uploaded
roster, it's reported as a warning so you can add that sheet.

### Matching rules
- **Exact** (default, matches the auditing guide): Registered mail == Email,
  Registered Number == Phone, exact after sanitising (lower-case email; digits-only
  phone with trailing `.0` removed).
- **Inclusive**: also checks WhatsApp / broadcast columns and matches phones on the
  last 10 digits (more forgiving of country-code differences).

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```
Open the URL it prints (usually http://localhost:8501).

## Deploy to Google Cloud Run
```bash
gcloud run deploy attendasync --source . --region asia-southeast1 --allow-unauthenticated
```
(The included `Dockerfile` binds to `$PORT` and raises the upload limit to 400 MB.)

## Files
- `app.py` — Streamlit UI
- `attendance_core.py` — the engine (importable / testable on its own)
- `requirements.txt`, `Dockerfile`
