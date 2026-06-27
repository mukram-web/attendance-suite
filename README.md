# Be10X — AI CAP Attendance Suite

One app that **marks attendance automatically** from the Zoom exports and shows
you the **marked roster + a full dashboard** in one place — for the AI CAP
live-cohort batches (B17–B28).

## 🚀 The one app to run: `attendance_app.py`

```bash
pip install -r requirements.txt
streamlit run attendance_app.py
```

It has **three tabs**:

| Tab | What you see |
|---|---|
| **📊 Dashboard** | KPIs (active learners, avg %, top/weakest batch), a **% by batch** bar chart, a **batch × session heatmap**, a **trend** line, and a per-session table you can download. |
| **📋 Roster (marked attendance)** | The marked sheet, student-by-student, with **Present/Absent** colour-coded per session — like the spreadsheet. Contacts are masked by default; full marked `.xlsx` is downloadable. |
| **🪵 Marking log** | What got marked this run (new vs re-marked columns) and any warnings. |

### Two ways it gets data

1. **🟢 Live (automatic)** — reads the roster + attendee `.zip` straight from
   **Google Drive**, marks on its own, and refreshes on open / the **🔄 Refresh**
   button. This is the "set it once, then just open the link" mode.
   👉 **Set it up with [`SETUP_LIVE.md`](SETUP_LIVE.md)** (or the visual
   [`GO_LIVE_GUIDE.html`](GO_LIVE_GUIDE.html)).
2. **📤 Manual upload** — if Google isn't connected yet, upload the roster (and
   optionally the L2 schedule + Zoom `.zip`) in the sidebar. Works immediately.

Either way, if you give it fresh attendee data it **auto-marks** (no button); if
you give it only a roster that's already marked, it just shows the dashboard.

## How marking works (unchanged, trusted logic)

For each Zoom `attendee_<id>_<date>.csv`, the app reads the **Webinar ID** from the
filename, looks it up in the **L2 schedule** to find the batch(es)/date/topic
(falling back to the folder name), and marks each student **Present** if their
**Registered mail** or **Registered Number** matches an attendee — else **Absent**.
Shared/combined sessions are marked in every batch. Default match is **exact**.

"Active" learners (the dashboard denominator) = everyone whose **Payment** isn't a
refund / unidentified / blank. Toggle **Active only ↔ All enrolled** in the sidebar.

## Files

| File | Role |
|---|---|
| **`attendance_app.py`** | ⭐ the unified app (dashboard + roster + auto-marking + live/upload) |
| `attendance_core.py` | marking engine — `process()` (zip) and `process_files()` (any source) |
| `dashboard_core.py` | dashboard compute + the per-student `roster_grid()` |
| `live_data.py` | reads roster / L2 / attendee zip from Google Drive (live mode) |
| `app.py`, `dashboard.py` | the original standalone marker / dashboard (still work) |
| `.streamlit/secrets.toml.example` | template for the Google credentials (live mode) |
| `SETUP_LIVE.md`, `GO_LIVE_GUIDE.html` | how to turn on live Google mode |
| `requirements.txt`, `Dockerfile` | deps / container build |
| `sample_data/` | a real roster snapshot for local testing — **gitignored (student PII)** |

## ⚠️ Privacy & secrets

- `sample_data/`, any `.xlsx`/`.csv`/`.zip`, and `secrets.toml`/`*.json` keys are
  **gitignored** — never commit student PII or credentials to the public repo.
- The roster tab masks emails/phones by default. Secrets live in **Streamlit**, not GitHub.

## Deploy

Streamlit Community Cloud (recommended): push to GitHub, then at share.streamlit.io
create an app with **Main file path = `attendance_app.py`**, Python **3.12**, and
paste your secrets under **Advanced settings**. Full steps in `SETUP_LIVE.md`.
