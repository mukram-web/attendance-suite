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
| **📊 Dashboard** | KPIs (enrolled, active, batches, sessions logged) → a **cross-batch comparison bar** → a **batch selector**; pick a batch to drill into metric cards, an **attendance-by-date line chart**, a **closing-types panel** (share + per-channel attendance), and a **sessions table**. Attendance = **present ÷ total batch strength**; colour bands ≥45 / 30–45 / <30. |
| **📋 Roster (marked attendance)** | The marked sheet, student-by-student, with **Present/Absent** colour-coded per session — like the spreadsheet. Contacts are masked by default; full marked `.xlsx` is downloadable. |
| **🪵 Marking log** | What got marked this run (new vs re-marked columns) and any warnings. |

### The Dashboard tab (new, data-driven)
The Dashboard shows the **same updated roster the app fetches** from your links —
the one you can download — so *download = dashboard*. Each session is labelled with
its real topic via a **Webinar-ID join** to the **L2 schedule** (attendee filename
⋈ L2 on Webinar ID — the same key the marker uses; resolves ~100% of sessions),
falling back to L2-by-date then the roster header. It's fully **dynamic** —
batches, sessions, and closing-type values are all discovered at read time, so a
new `AI CAP B29` tab or a new session column shows up after a **Refresh** with no
code change. Topics with no L2 match are flagged ("no L2 match") to surface
schedule gaps. The logic lives in a pure, unit-tested layer:

| Module | Role |
|---|---|
| `data.py` | pure transforms → per-batch metrics (no Streamlit; unit-tested) |
| `sheets.py` | gspread (live) + xlsx (dev fallback) source adapters + L2 topic lookup |
| `dash_view.py` | the Plotly drill-down UI |
| `tests/test_data.py` | `python -m unittest tests.test_data` — 14 tests, incl. dynamic-growth |

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
| `data.py` · `sheets.py` · `dash_view.py` | the new Dashboard: pure metrics · gspread/xlsx source · Plotly UI |
| `tests/test_data.py` | unit tests for `data.py` (`python -m unittest tests.test_data`) |
| `attendance_core.py` | marking engine — `process()` (zip) and `process_files()` (any source) |
| `dashboard_core.py` | per-student `roster_grid()` for the Roster tab |
| `live_data.py` | reads roster / L2 / attendee zip from Google Drive (marker live mode) |
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
