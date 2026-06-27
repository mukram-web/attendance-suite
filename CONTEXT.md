# CONTEXT — Be10X AI CAP Attendance Suite (handoff to Claude Code)

> **Read this first.** This is a handoff from a long Claude.ai chat. It catches you up
> on what's built, how the data is shaped, what's deployed, and the one open task
> (making the dashboard *live*). The person you're helping is **new to coding and
> deployment** — explain simply, go step by step, and verify current install/deploy
> commands on docs.claude.com / official docs when relevant, since UIs change.

---

## 1. What this project is
Two related tools for **Be10X** (House of EdTech), for the **AI CAP** live-cohort
batches **B17–B28**:

- **AttendaSync (the "marker")** — reads Zoom attendee exports and marks
  Present/Absent into the Master Roster. Files: `app.py`, `attendance_core.py`.
- **Attendance Dashboard** — reads the (already-marked) Master Roster and shows
  batch-wise attendance % with filters/charts. Files: `dashboard.py`, `dashboard_core.py`.

**Current focus = the Dashboard, and specifically making it LIVE (see §8).**

---

## 2. The three data sources
All live in the **House of EdTech Google Workspace**, but in a **different Google
account** than the one connected in the chat (`mayank.soni@houseofedtech.in`). The user
has **view-only** access to the originals and intended to make **copies** into an account
they control.

1. **L2 schedule** (Google Sheet) — session list. Monthly tabs. Row-1 headers:
   `B`=Batch Name, `C`=Topic, `M`=Webinar ID, `A`=date (carried down). Webinar ID
   matches the attendee filenames. *Only the marker needs this.*
2. **Master Roster** (Google Sheet) — all enrolled students + attendance marks.
   *Both tools' core file; the dashboard needs ONLY this.* (Layout in §4.)
3. **Attendee folder** (Drive folder) — Zoom exports dropped after each session,
   files like `YYYY-MM-DD - <batch> - <topic>/attendee_<webinarid>_<date>.csv`.
   *Only the marker needs this.*

---

## 3. File inventory (this folder)
- `app.py` / `attendance_core.py` — AttendaSync marker (UI / engine)
- `dashboard.py` / `dashboard_core.py` — Dashboard (UI / engine)
- `requirements.txt` — unified deps: streamlit, openpyxl, pandas, altair
- `Dockerfile`, `marker_README.md` — for Cloud Run deploy of the marker
- `previews/` — sample dashboard chart images (bar, heatmap)
- `sample_data/Copy_of_Master_Batch_Rosters.xlsx` — a **real roster snapshot** for local
  testing. ⚠️ Contains real student PII (emails/phones) — it is **gitignored; never commit/push it.**
- `.gitignore` — excludes data, secrets, keys

To also test the marker locally, drop the L2 sheet and the attendee `.zip` into
`sample_data/` (the user has both from the original chat).

---

## 4. Roster layout (important — don't re-derive)
- Sheets named `AI CAP B17` … `AI CAP B28`. **Ignore** any sheet whose name contains
  "Att" (e.g. `AICAP B17 Att`) — those are Zoom helper sheets.
- Standard columns: `A`=Country code, `B`=Registered Number, `C`=Registered mail,
  `D`=Country code, `E`=WhatsApp Number, `F`=broadcast mail, `G`=batch name, `H`=amount,
  `I`=Payment, `J`=Closing Type, then **`K` onward = session columns**.
  - **B20 is shifted** (extra empty column): always **detect columns by header name,
    not fixed letter**. The engines already do this.
- **Header row varies**: it's row 2 for B17,B18,B21,B22,B23,B24; row 1 for
  B19,B20,B25,B26,B27,B28. Detect the row containing "Registered Number"/"Payment".
  The **session date label is always in row 1**; the **topic** (when present) is in row 2.
- Session cells contain `Present` / `Absent` / blank.
- **"Active" definition** (the dashboard denominator) comes from the **Payment** column:
  NOT active = value containing `refund` / `undifined` / `unidentif` / `undefined`, or blank.
  Active = Full Paid / Full / Booking / Partial / Partial Paid.
- Topics repeat in the same order across batches, so "Session N" ≈ same topic across
  batches (the heatmap/trend rely on this).

---

## 5. Matching logic (AttendaSync) — exact mode (the chosen default)
A roster student is **Present** for a session if their **Registered mail** equals an
attendee email **OR** their **Registered Number** equals an attendee phone. Exact match.
Email = lowercased, whitespace removed. Phone = digits only, trailing `.0` removed.
Blank cells never match. Use **only** Registered mail + Registered Number (not
WhatsApp/broadcast). Combined/multi-batch sessions are handled: expands `+` lists, ranges
like `AI CAP B1 - AI CAP B22` → 1..22, commas, and `CAP`/`US` groups; falls back to the
folder name for batch identity when a webinar isn't in L2. (An "inclusive" mode also
exists as a toggle; **exact is the default**.)

---

## 6. Dashboard logic
Reads the roster's Present/Absent marks **directly** (does NOT re-run matching). Per
batch × session it computes present (active), active strength, and %. Views: KPIs,
**bar** (% by batch — overall or per session), **heatmap** (batch × session), **trend**
line; filters: batches, session focus, and an **Active-only vs All-enrolled** toggle.
**Only the roster file is needed.** Engine entry point: `dashboard_core.compute(roster_bytes) -> DataFrame`.

---

## 7. Deployment state
- GitHub repo: **`mukram-web/attendance-marker`** (Public), deployed on **Streamlit
  Community Cloud**, marker main file = `app.py`.
- ⚠️ When uploaded via the browser, files first landed as `app (1).py` and
  `attendance_core (1).py`; we renamed them to `app.py` / `attendance_core.py`.
  **Verify the rename actually saved in the repo** — the import `import attendance_core as ac`
  requires the file to be exactly `attendance_core.py`.
- Dashboard not deployed yet. To add it as a 2nd app from the same repo: add
  `dashboard.py` + `dashboard_core.py`, use the unified `requirements.txt` (adds
  `altair`), then on share.streamlit.io create a new app with main file `dashboard.py`.

---

## 8. THE OPEN TASK → make the dashboard LIVE (advise, then implement)
The user picked **"let Claude Code advise me."** They want the dashboard to refresh from
the live roster on a button click.

**Key fact to explain:** a deployed Streamlit app runs on **Streamlit's servers**, so it
needs **its own Google credential** — the chat's Drive access and the user's browser
view-access do **not** transfer to it. Good news: the dashboard needs only the **roster**
(one file), so the access story is small.

Approaches to advise between:

- **A. Service account (robot Google login) + Streamlit** — create a service account in
  Google Cloud, enable Drive + Sheets API, download a JSON key, store it in the app's
  **Streamlit secrets**, and **share the roster with the robot's email** (Viewer). App
  reads live via `gspread`/Sheets API; add a Refresh button.
  - ✅ Best for "the team just opens a link" (no per-user login).
  - ➖ Most setup (Google Cloud), but one-time (~20 min).
  - Works despite the user's view-only access **as long as whoever can share the file
    shares it with the robot** — easiest to use the **user's own copy**, which they own.
- **B. Apps Script inside the Sheet** — a bound script on the roster (Extensions → Apps
  Script) runs **as the user**, so it reads the live roster with **no keys**; add a custom
  menu/button to recompute into a results tab and/or feed **Looker Studio**.
  - ✅ Simplest, no hosting, no keys; the team has built Apps Script before.
  - ➖ It becomes a Sheets/Looker dashboard, not the Streamlit one.
- **C. Streamlit + per-user Google OAuth** (`st.login`, OIDC) — viewers sign in with
  Google; the app reads via their access.
  - ✅ No file-sharing needed if the viewer already has access.
  - ➖ Medium OAuth setup; per-user login.

**Suggested recommendation:** if they want a shareable team dashboard and are OK with a
one-time Google Cloud setup → **A**. If they want the least setup and are happy living in
Google/Looker → **B**. Decide *with* the user, then implement. Either way you'll need the
**roster's Drive file ID**, and for **A** the roster shared with the service-account email.

---

## 9. Decisions already locked
- Denominator = **active only** (exclude refunds), with an All-enrolled toggle.
- Refresh = **on button click** (not scheduled).
- Match = **exact** email/phone.
- Dashboard target = **Streamlit** (unless the user picks Apps Script after your advice).
- % = active present ÷ active strength × 100.

---

## 10. Working style + safety
- User is **new to coding/deployment**, communicates briefly (occasional typos), wants
  live, working results. Be warm, concise, step-by-step.
- Verify current Claude Code / Streamlit / Google Cloud steps in official docs (they drift).
- **Never commit student PII or any key/secret to the public repo.** Keep `sample_data/`,
  spreadsheets, CSVs, zips, and service-account JSON out of git (see `.gitignore`).

---

## 11. Good first moves for Claude Code
1. `pip install -r requirements.txt` and run `streamlit run dashboard.py`; upload
   `sample_data/Copy_of_Master_Batch_Rosters.xlsx` to confirm the dashboard works locally.
2. Verify/fix the repo file naming from §7.
3. Walk the user through choosing A/B/C for "live," then implement it (add Refresh + live
   roster read), and deploy.

---

## 12. UPDATE (2026-06-27) — unified auto app built ✅
The open task (§8) is implemented. New entry point **`attendance_app.py`** merges the
marker + dashboard into ONE app: it **auto-marks** (no button) and shows a **Dashboard**
tab + a **Roster (marked)** tab + a **Marking log** tab. It reads **live from Google
Drive** when secrets are configured (approach **A**, service account), and **falls back to
manual upload** otherwise — so it runs with or without Google.

- New/changed code: `attendance_core.process_files()` (mark from any source, not just a
  zip), `dashboard_core.roster_grid()` + `batch_sheet_map()` (per-student grid),
  `live_data.py` (Drive reader: export Sheet→xlsx, download zip, list folder).
- Live setup (the only thing the user must do themselves): `SETUP_LIVE.md` /
  `GO_LIVE_GUIDE.html`, template in `.streamlit/secrets.toml.example`. Needs the roster
  Sheet ID, L2 Sheet ID, attendee-zip file ID, all shared with the service-account email.
- Verified locally on the real roster snapshot: dashboard numbers match the `previews/`
  (avg 42%, top B26 50%, weakest B20 30%); engines + full app render with no errors.
- Decisions kept: exact match, active-only denominator (with toggle), refresh on
  open/button. Dashboard **displays** marked data (does not write back into the Sheet) —
  safer for the live roster; the marked `.xlsx` is downloadable. Deploy main file =
  `attendance_app.py`.

### 12a. Live integration findings (verified against the real Drive, 2026-06-27)
Real IDs: roster `1CzASgM5yrbASE5vIxSrKBB-yQSRtQxh_iCVS9GTpfgg`, L2
`1nlQPkg1l_cNHMtN2HKvqinmU_n0bLsC9RkkMfeqvPlM`, attendee **Shared Drive**
`0ADZkkxHLwZa9Uk9PVA`. Service account `attendance-robot@fresh-delight-500710-g6
.iam.gserviceaccount.com` (personal-Gmail project, no org → key download allowed).
Three real issues found and fixed:
1. **Shared Drive** (id starts `0A`): `files.list/get` need `supportsAllDrives` +
   `includeItemsFromAllDrives`. Done in `live_data.py`.
2. **Performance**: the Drive holds the FULL company history (517 dated folders, 538
   files); a full walk took ~15 min. Fix: `live_data.fetch_new_attendees()` reads the
   roster's existing session dates and pulls ONLY folders whose (batch, mm_dd) isn't
   already marked, with parallel downloads. Live `load_live()` ≈ 42s, cached. Roster
   already carries all history (117 cols), so the dashboard shows everything instantly
   and only NEW weekend sessions get marked. `mark_all=true` forces a full rebuild.
3. **Formulas**: batches B17,B21,B22,B23,B24 store attendance as FORMULAS; loading with
   `data_only=False` then saving dropped cached values (117→86 cols). Fix: marker now
   takes `values_only=` — the app calls `process_files(..., values_only=True)` so
   formula cells load as cached values and survive. Standalone `app.py` keeps the old
   formula-preserving default.
   Also: `AI CAP B18` has ~499 J:K merged cells in the body → marker now unmerges
   body ranges before writing.
- Live end-to-end verified: 42s load, +16 new columns, 133 marked cols, dashboard renders.
- ⚠️ Security TODO: the first downloaded SA key was exposed in a tool log during setup —
  rotate it (delete key in Cloud console → new JSON → re-run `make_secrets.py` → update
  Streamlit secrets) before/at deploy.
- Helper `make_secrets.py` builds `.streamlit/secrets.toml` from the JSON key (triple-
  quoted private_key) without the user hand-editing TOML.
