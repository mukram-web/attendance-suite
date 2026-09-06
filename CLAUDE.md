# Be10X — AI CAP Attendance Suite

Read this before touching anything. `CONTEXT.md` describes an **earlier design**
and `README.md` is partly stale (its tab list and its "live fetch is the normal
path" section predate the pipeline) — where they disagree with this file, this
file is right.

Claims here were fact-checked against the code on 2026-08-12. Anything about
external state (what is deployed, what secrets exist) is a **snapshot** — verify
it rather than trusting it.

---

## 1. What this does, in one line

Every Monday it reads the Zoom attendee reports for the week's live sessions,
marks Present/Absent into a copy of the Master Batch Rosters, and publishes the
data a Streamlit app shows.

## 2. The architecture that matters

The heavy work runs **off** the Streamlit server. This is the single most
important thing to understand:

```
GitHub Actions (Mon 06:00 IST)          private Shared Drive         Streamlit app
  .github/workflows/refresh.yml   ──►   attendance.duckdb      ──►   attendance_app.py
  runs pipeline.py                      (+ marked .xlsx)             downloads & renders
  fetch → mark → analyse → build                                     ~15-30s, no marking
```

**Never move fetching or marking back into the app.** It was there originally and
made the app take ~3 minutes to start and crash on Streamlit Cloud's 1 GB instance
(the raw attendee CSVs are 100+ MB in memory).

The app picks a mode in this order (`attendance_app.py`, search `_store_available`):

1. **store mode** — engages when `drive.store_folder_id` is set in secrets **OR**
   when a `.cache/attendance.duckdb` file merely exists locally. That second case
   is a real trap: a leftover file from `python pipeline.py --no-upload` puts the
   app in store mode with nothing to refresh from, so it serves stale data and
   "Refresh from Google" cannot fix it. Delete the file to get out.
2. **legacy live mode** — fetch + mark in-process. Slow; the thing we moved away from.
3. **manual upload** — no Google at all; the user uploads the roster `.xlsx`.

## 3. Files

| File | Role |
|---|---|
| `pipeline.py` | the weekly job: fetch → mark → day-1 analysis → build store → **render `site/`** → upload. Flags: `--no-upload`, `--no-site`, `--allow-partial`, `--mode`. |
| `attendance_app.py` | the Streamlit app (tabs: Dashboard, **Sessions** (Browse / This week / Trainers), Roster, Day-1 analysis, Forecast, BSIAI). Reads the store; does not compute. |
| `attendance_core.py` | the marker engine: parses Zoom reports + L2, writes Present/Absent into the workbook. |
| `dashboard_core.py` | `compute()` (per batch × session) and `roster_grid()` (per-student grid). |
| `data.py` | pure data layer → the `DATA`/`summary` objects the dashboard renders. No I/O, unit-tested. |
| `dash_view.py` | Plotly + Streamlit rendering. Its figure builders and HTML fragments are **pure** so `site_build.py` can reuse them. |
| `day1_analysis.py` | day-one / latest-session cut of payment and close type for the newest `DAY1_BATCHES` (4) batches. |
| `forecast.py` | predicted attendance for sessions that have not run yet: decay curve x batch offset x pod multiplier. Pure, no I/O, unit-tested. See §4c. |
| `day1_template.html` | **used by the live app** to render the Day-1 tab (and by the static site). Not dormant — do not delete. |
| `live_data.py` | all Google Drive I/O + the disk caches. |
| `sheets.py` | L2 webinar→topic lookup. |
| `bsiai.py` | the **BSIAI programme** — its own roster Sheet, its own batch numbers, no session columns. Computes attendance straight from the attendee reports. |
| `polls.py` | Zoom poll exports -> session/trainer/recommend ratings, the 1-5 histograms and NPS. `nps_from_dist` is the ONLY place the promoter/detractor split is written down. |
| `recap.py` | the week just gone, scored as a RESIDUAL against the decay curve. Pure, unit-tested. See §4e. |
| `trainers.py` | per-trainer rollups + identity resolution (91 L2 spellings -> 63 people). Pure, unit-tested. See §4e. |
| `archive.py` | dated snapshots of the four source Sheets, the store, **and the marked workbook** into `archive/` on the private Shared Drive. Runs as pipeline step `[8b]`. See §4d. |
| `site_build.py`, `site_templates/` | the static-website build. See §7 — it is **wired to deploy**, not inert. |

## 4. Data sources

- **Master Batch Rosters** (Sheet) — one tab per batch, `AI CAP B<n>`.
  Typical layout is country code / registered number / registered mail / whatsapp /
  broadcast mail / batch / amount / **Payment** / **Close Type**, but **the column
  letters are NOT stable**: on most tabs Payment/Close Type are I/J, on `AI CAP B29`
  and `AI CAP B20` they are J/K, and the header row is row 1 on some tabs and row 2
  on others. Always locate columns by header text (§5.1).
- **L2 Weekly Live Sessions** (Sheet) — the schedule; maps Webinar ID → batch +
  topic + **Mentor** (who taught it; shown as the dashboard's Trainer column).
  The Mentor column exists only on the recent monthly tabs, and is matched on the
  header text **exactly** — the same row also carries "Mentor's email" and
  "In-house/Freelancer mentor", and a substring test would bind to the wrong one.
  Unlike the topic, the mentor has **no date-only fallback**: borrowing another
  batch's trainer would put a real person's name against a session they did not
  teach, so an unmatched session shows blank. Measured 2026-09-03: 1,230 of 1,235
  L2 webinars carry a mentor, and all 447 dashboard sessions resolved one.
- **Weekly Sessions Files** (Shared Drive) — folders named
  `YYYY-MM-DD - AI CAP B<n> - <topic>` holding `attendee_<webinarid>_<date>.csv`.
- **A second Shared Drive** — where the pipeline writes `attendance.duckdb` and the
  marked `.xlsx`. Its id, and every other id, lives in the repo/app **secrets**, not here.

**Session columns:** older batch tabs carry Present/Absent columns in the Sheet
itself. Newer batches (B29+) do not — the marker **appends** them to its own copy
of the workbook and they are never written back to the Sheet. So "the roster has no
attendance columns" is true of the Sheet and false of the marked store.

### 4b. BSIAI — the second programme (added 2026-08-28)

BSIAI is **not** AI CAP with different numbers; it is a separate programme that
shares only the L2 schedule and the attendee drives.

| | AI CAP | BSIAI |
|---|---|---|
| roster | Master Batch Rosters, one tab per batch | its own Sheet (`BSIAI_ROSTER_ID`), batch read from a **`Batch` column**, not the tab name |
| status columns | `Payment` + `Close Type` | **only `Refund`** — so no closing-type panel, and Active = not refunded |
| session columns | in the workbook / appended by the marker | **none, ever** — attendance is recomputed from the reports each run |
| session gate | **L2 only** for display since 2026-08-29 (§5.6); still falls back to the folder name when deciding what to *mark* | **L2 only** everywhere — no L2 row, no session |

Things that will trip you up:

1. **`_track()` must classify BSIAI before CAP.** Its folders read `BSI B1`,
   `bsi b2`, `BSIAI B1`; L2 reads `BSIAI B1`. Without the BSIAI branch every one
   of them parses to `('CAP', 1)` — the same key as AI CAP B1. Verified: adding
   the track moved exactly 25 of 1,188 L2 webinars, all to BSIAI, none away.
2. **Batch numbers carry group suffixes in the roster only** — `BSIAI B1GA`,
   `BSI B2W1/W2/W3`. `bsiai.batch_num` folds them into the parent batch; the
   shared `extract_batches` cannot read them and is not asked to.
3. **Folder names are lowercase on one drive.** The word-boundary batch-number pattern was case-sensitive
   in both `extract_batches` and `_folder_batches`; both now pass `re.I`.
   Verified across all 1,048 top-level folders: exactly 7 changed, all `bsi b2`.
4. **The same session sits on both Shared Drives** ("Weekly Sessions Files" as
   `BSI B1`, "Zoom extracts" as `BSIAI B1`) and sometimes twice on one of them.
   `sessions_from_files` keys on webinar id and keeps the copy with the most
   attendees — the copies differ by a row or two.
5. **`MM-AI B1` is a third programme**, not BSIAI. `batch_num` returns None for
   it. It has no session folders on Drive at all.
6. **Three tabs are excluded on purpose** (owner-confirmed 2026-08-29), listed in
   `bsiai._IGNORED_TABS`: `failed BSI B1` (a Q&A group split), `Sheet8` (~466
   contacts with no Batch column) and `NEXT BATCH 15K MMAI`. They are skipped
   **silently**; every other unusable tab still warns, which is what stops a real
   roster tab from disappearing unnoticed. Revisit `Sheet8` if a Batch column is
   ever added - those rows would then join a denominator and move the numbers.

Config: `BSIAI_ROSTER_ID` (repo secret) or `bsiai_roster_id` under `[drive]`.
Absent → the pipeline skips the section entirely and the tab says so; the AI CAP
refresh is never affected. A BSIAI failure is caught and reported in the tab
rather than failing the run.


### 4c. The Forecast tab (added 2026-08-31)

Predicts attendance for sessions that have **not run yet**, from the schedule in
the **Master Curriculum Schedule** Sheet (`CURRICULUM_ID`) — one tab per pod,
columns `Date | Day | AI CAP B<n> | Trainer | ...`. Built inside `build_store()`
because it needs the finished `DATA` and nothing else, so it can never disagree
with the dashboard beside it. Aggregates only — safe to publish.

    predicted = group size x curve(week) x batch_offset x pod_multiplier

- **curve(week)** — attendance against weeks-since-batch-start, pooled over every
  batch. The strong part: B17-B38 all open at 46-59% and fall ~10%/week.
- **batch_offset** — that batch's own level vs the curve. This is what spots a
  cold batch (B36 sat at x0.863 on 2026-08-30).
- **pod_multiplier** — a pod's draw vs its batch on the SAME day, so batch level
  and week both cancel. Data x1.13 / Ops x1.12 highest, Students x0.89 lowest.

**Measured, not asserted.** `accuracy` is re-derived every run by rolling-origin
backtest: 9.3% MAPE on headcount at 1-4 weeks against 32.1% for carrying the last
rate forward. The app shows both, so a model that rots in the field says so.

Things that will trip you up:

1. **The pod split began 2026-08-23.** Before that a date is ONE whole-batch
   session; after it, up to 11 pod sessions with their own denominators. Which
   one a scheduled date is comes from `_split_kind`: >= `SPLIT_MIN_TOPICS` (5)
   distinct normalised titles across the pod tabs. Whole-batch dates show 1-3
   (pure spelling variants like "Prompt Engineering 2026" vs "... in 2026"), real
   splits show 8-12, so the threshold sits in a wide gap.
2. **Neither the roster nor the curriculum sheet carries a YEAR** — both say just
   "30 Aug". `_walk_years` attaches years by walking the (known chronological)
   list and advancing when the month goes backwards. Without it a schedule
   crossing new year sorts December after January.
3. **The pod layer is the weak one and is deliberately separable.** It rests on a
   few dozen sessions, so multipliers are shrunk toward 1.0 by `POD_SHRINK_K`.
   Do not "improve" the fit by removing the shrinkage.
4. **A week beyond any batch's observed lifetime is NOT forecast.** There is no
   curve point, and extrapolating an exponential tail invents numbers. Those
   sessions are dropped with a warning instead.
5. **Batches that have not started are assumed** — strength and pod shares from
   the `RECENT_BATCHES` (4) mean. Pod shares are genuinely unstable (Generalist
   was 18.9% of B37 and 48.6% of B38), so every such row carries
   `assumed_strength: True` and the UI marks it. Never let those read as measured.
6. **`CURRICULUM_ID` is owned outside the roster team.** Sharing it with the
   service account is a separate step: as of 2026-08-31 the robot got **403** on
   it. Absent or unreadable, the forecast is skipped and the tab says which —
   `None` = never configured, a section with warnings = configured but broken.

### 4d. The archive (added 2026-09-05)

**The store is not a backup.** `pipeline.py` REPLACES `attendance.duckdb` in place
every week, so nothing in this system retained history until now: a tab deleted on
Tuesday was gone from the dashboard by the next Monday with no way back.

The exposure is real — measured 2026-09-04, the four source Sheets are owned by
**four different individuals**, none of them the robot, and one of them a personal
gmail rather than a company account. (Run the owner check in `archive.py`'s
docstring to see who currently holds each; the names are deliberately not written
down here, since this repo is public.) Any one of them can delete a tab,
restructure a column, or bin the file.

L2 is the worst case: `REQUIRE_L2` (§5.6) means a session with no L2 row is
**hidden**, not merely unlabelled, so losing L2 blanks the dashboard rather than
staling it.

Step `[8b]` copies each Sheet as `<Label>_<YYYY-MM-DD>.xlsx` into an `archive/`
folder beside the store, plus that week's `attendance_store_<date>.duckdb` and
`Master_Batch_Rosters_marked_<date>.xlsx`. About 27 MB a week.

**The marked workbook is archived separately from the source roster**, and both
matter. The source snapshot is the sheet as its owners keep it; the marked one is
that sheet with every Present/Absent column written in — the artefact people
actually download and pass around, and the one the pipeline overwrites on Drive
each Monday. `MARKED_LABEL` keeps the two names apart.

- **Snapshots are immutable.** A name that already exists is skipped, never
  replaced — re-running on the same day must not overwrite the morning's copy
  with one taken after somebody edited the sheet at lunchtime.
- **It runs AFTER the upload and never raises.** The dashboard refreshing is what
  people depend on; a Drive hiccup in the archive must not cost them that.
  Failures print a WARNING and the run stays green.
- **These files are PII** — the roster snapshot is every student's name, email and
  phone. They live on the private Shared Drive under the same access list as the
  store. Never a local path that could be committed, never the repo.
- The folder is found **by name**, so moving or renaming it yields a fresh empty
  archive rather than a crash. There is no id to keep in secrets.
- **Nothing is ever pruned. Owner's decision, 2026-09-05.** There is deliberately
  no retention policy, no thinning of old snapshots, no "keep monthlies after 90
  days". The whole point is that a snapshot exists for a week nobody thought to
  check, and any pruning rule eventually deletes exactly the one that mattered.
  At ~18 MB a week this is ~1 GB a year, which is cheap against re-deriving a
  deleted roster. **Do not add cleanup here** — if the Shared Drive ever runs
  short, raise the quota or move old snapshots to cold storage, but keep them.

To restore a sheet: download the dated `.xlsx` and either re-upload it as the
Sheet, or point the relevant `*_ID` at a copy of it and re-run the pipeline.

**Past weeks are readable in the app**, not just recoverable from Drive. The
sidebar's **Week** selector lists every archived store newest-first; picking one
renders that week verbatim — attendance, day-1, forecast, BSIAI and roster all as
they stood. A yellow banner sits on the PAGE, not just the sidebar, because
someone screenshotting a number from a historical week must not be able to
mistake it for today. Two things to know:

- Snapshots load through `_load_snapshot`, deliberately NOT `_load_store`. They
  are immutable so they cache permanently rather than on the live store's
  30-minute TTL, and they must never be written to `_STORE_PATH` — that would
  leave last month's data where the live loader expects today's.
- **The marked .xlsx download follows the week you are viewing.** An archived
  week's store still carries `marked_xlsx_file_id`, but that id points at the
  LIVE `Master_Batch_Rosters_marked.xlsx`, which the pipeline replaces every
  Monday — following it would hand over today's workbook labelled as that week's.
  `_archived_marked()` looks up the dated copy instead. Weeks refreshed before
  2026-09-05 have none, and the app says so rather than serving the wrong file.
- The selector only appears when `store_folder_id` is configured AND at least one
  snapshot exists, so a fresh install shows nothing rather than an empty control.

### 4e. Sessions / Recap / Trainers / Students (added 2026-09-06)

Four views built from data already in the store — **no new fetch**. All four are
pure modules the pipeline calls, so the app stays a reader.

**Rank on the RESIDUAL, never on raw attendance.** Attendance falls ~10% a week
over a cohort's life, so a raw league table answers "which batch is youngest?".
B38 averages 48.2% at two sessions old and B17 22.6% at thirty-eight, yet B17 was
*ahead* at every comparable point (56.1% vs 52.1% at session one). `recap.py`
scores each session against `forecast.py`'s curve x batch offset x pod
multiplier, so 1.00 = exactly on curve. On the live store the weekly index sits
at 0.95-1.05 while raw attendance "falls" 31% -> 21%. **Caveat, pinned by a
test:** the curve is fitted including the session being scored, so an outlier
partly raises its own expectation. Never quote the index as out-of-sample.

**NPS is top-box on the 1-5 recommend question** — promoter 5, passive 4,
detractor 1-3 — and the scale is NEVER inferred from the values. The obvious
"if any answer > 5 it must be 0-10" rule inverts the worst sessions: a genuine
0-10 poll where nobody scored above 5 is -100 but reads as +100. Measured over
1,113 poll files, Be10x has never asked a 0-10 recommend question. Corpus NPS
+59.5 over 316,006 answers. Combining sessions **sums the histograms**; averaging
per-session percentages weights a 13-response pod like a 316-response one.

**Trainer identity: 91 L2 spellings are 63 people.** Email is used where L2 has
it, but is NOT proof — `kaladipti0@gmail.com` is typed against 'Dipti',
'Dipti Kala', 'Vansh Agrawal' AND 'Abhishek Raj Pramani', so names under one
address are split into plausibly-same-person groups first. Otherwise a short name
joins a longer one only as an unambiguous PREFIX; grouping on the first token
would merge 'Ravi Kumar' with 'Ravi Sharma'. Single letters are initials, not
tokens. Delivery notes are matched on a PREFIX (`simul*`) because they get
misspelled — 'Simuliive', one doubled letter, split one trainer into three.
**In-house/Freelance IS available** (L2 column 4), blank on 82% of rows, but the
classification belongs to the PERSON: filling it per person lifts coverage from
17.5% of rows to 405 of 484 sessions.

**A per-student view was built and REMOVED (owner's call, 2026-09-06).**
`students.py` and its tests are gone; recover them from git if it is ever wanted.
Four traps cost real bugs there and apply to ANY future per-student work, so they
are worth keeping in mind:
1. Match session columns with `_mmdd`, not an ISO regex — older tabs carry
   '9th May', and an anchored pattern dropped 59 real columns.
2. Canonicalise the roster's pod cell with `pods.from_roster_cell`. The header
   says `Techies`, the cell says `Techies - Ai Career Accelerator Program B35`;
   comparing raw matched ZERO of 3,236 students and dropped every pod session.
3. Exclude the sessions `REQUIRE_L2` hides — `roster_grid` keeps their columns,
   and counting them deflated attendance 13-22 points below the Dashboard's own
   figure for the same batch.
4. A student with no pod is whole-batch only, never a member of every pod.

## 5. Invariants — break these and the numbers go silently wrong

1. **Locate roster columns by HEADER TEXT, never by fixed letter** (see §4).
   Note `dashboard_core.py` is the weakest implementation: it matches header names
   by *exact* equality and falls back to a hardcoded index if "payment" is missing.
   Prefer the substring matching used in `attendance_core.py` / `data.py`.
2. **Phones from openpyxl are floats** (`919704189186.0`). Strip the `.0` BEFORE
   removing non-digits, or every phone shifts a digit and phone matching dies
   silently. This bug made day-1 attendance read 53.9% instead of 58.0%.
3. **Match attendees on email OR phone, comparing the LAST 10 DIGITS.**
   `attendance_core._phone_hit` and `day1_analysis.norm_phone` now agree; they
   did not until 2026-09-06, when `exact` mode compared the whole digit string.
   That made the phone half of the rule dead in production: the roster is 99.4%
   12-digit (91 prefix) and Zoom is 78.8% bare 10-digit, so it added 1-6 students
   per session where last-10 adds 126-145. Fixing it raised every re-marked
   session by ~8.4% (78,392 -> 85,007 present over B30-B39). Verified safe: of
   61,862 roster numbers, ZERO share a last-10 with a different full number.
   Email alone still under-counts badly (B34 day 1: 1,594 vs 1,753).
   ⚠️ **B17-B28 still carry 2-20 frozen Present/Absent columns in the roster
   Sheet**, which the marker skips, so those sessions keep their pre-fix values
   while everything re-marked gains ~8.4%. `mark_all` is NOT plumbed through
   `pipeline.py` (it only reaches the legacy Streamlit path), and it would have
   to stay on permanently — the frozen columns live in the Sheet, which the
   pipeline never writes back to.
4. **Column I/J vocabulary drifts per batch** — `Full Paid`/`Full paid`/`Full`,
   `BDA Closing`/`Bda closing`/`BDA Closimg`, eight spellings of
   unidentified-refunded. Canonicalise on meaning. Beware: **three independent
   vocabularies exist** — `day1_analysis._canon_payment/_canon_close` (Day-1 tab),
   `data._REFUND_TOKENS`/`normalize_closing` (Dashboard), and
   `dashboard_core._REFUND_HINTS`. They do not agree (e.g. "Cancelled" counts as
   inactive in `data.py` but active in `dashboard_core.py`). Unifying them is
   worthwhile; until then, know which module governs the number you are changing.
5. **Day one = the earliest session the L2 schedule registers by webinar id.**
   Marketing walkthroughs share the batch folder naming but are never in L2.
   A title guard may FLAG but must never VETO (real topics contain "onboarding").
   Verified across B31–B35, 28 sessions, zero false negatives.
6. **L2 is the register of what ran — the dashboard shows nothing else.**
   `data.REQUIRE_L2` (owner's rule, 2026-08-29) drops any session column whose
   webinar has no L2 row. It applies to **both** programmes: BSIAI never admitted
   one, and AI CAP stopped falling back to the folder name for display. The
   columns are still MARKED into the workbook — the roster download keeps the
   full record — they are just not counted or shown, and the sessions panel says
   how many are hidden. It engages **only when a non-empty L2 lookup exists**: with
   no schedule loaded every session would look unregistered and the page would
   blank, so an absent L2 means "cannot tell", not "nothing ran". Measured on
   2026-08-29: 29 of 440 AI CAP sessions were unregistered, and hiding them moved
   B35 27.1→39.9%, B36 25.5→40.3%, B37 28.6→43.1%. **To restore a wrongly hidden
   session, add its row to L2 — never by turning the flag off.**
7. **Google's Sheet→xlsx export is non-deterministic** — the same unchanged sheet
   exports to different bytes each time, so `live_data.fetch_sheet_cached` keys on
   Drive `modifiedTime`. The app's own `_disk_memo` caches still hash bytes; they
   are only safe because they sit downstream of that sheet cache.
8. **Service accounts have zero Drive storage.** They can only upload into a
   **Shared Drive** (as Content Manager). A My Drive folder shared as Editor fails
   with `storageQuotaExceeded`, whatever role you grant. The SA also cannot create
   a Shared Drive itself.
9. **Never `next(ws.iter_rows(...))` unguarded** — people add empty scratch tabs
   (a "Pivot Table 2" tab once crashed the whole build).
10. **The pipeline refuses to publish partial data.** A failed folder listing or
   download, or a day-1 analysis that produced nothing, exits non-zero and leaves
   last week's store intact. `--allow-partial` overrides it; the workflow
   deliberately never passes that flag. Keep it this way — a green run that
   silently drops a weekend's session is worse than a red one.

## 6. Security — THIS REPOSITORY IS PUBLIC

**The app is behind a shared password** (added 2026-09-01). `attendance_app.py`
calls `_password_ok()` immediately after `set_page_config`, before the store is
downloaded — so an unauthenticated visitor never causes the PII to be fetched,
let alone rendered. It reads `app_password` from secrets and **fails CLOSED**: no
secret, no access, because an unset value far more likely means "not set up yet"
than "deliberately public".

It is a deterrent, not authentication: one shared string cannot tell users apart,
cannot be revoked for one person, and is only as private as the least careful
person holding it. For real access control use Streamlit Cloud's viewer
allow-list (Manage app -> Settings -> Sharing), which authenticates against real
Google accounts; the two compose fine.

Set it in **two** places, they are separate stores:
  - deployed: Streamlit Cloud -> Manage app -> Settings -> Secrets
  - local:    `.streamlit/secrets.toml`, as a TOML **top-level** key — it must sit
              ABOVE the first `[section]` header or it silently becomes part of
              that section and `st.secrets.get("app_password")` returns "".

- The roster, the attendee CSVs and `attendance.duckdb` all contain **student
  emails and phone numbers**. None of it may be committed.
- `.gitignore` covers `*.json` (with an exception for the aggregates-only
  `intro_attendance.json`), `*.duckdb`, `.cache/`, `site/`, `*.xlsx`, `*.csv`,
  `.streamlit/secrets.toml`.
- **Do not add `actions/cache` for `.cache/`** in the workflow. Actions caches are
  restorable by fork PRs on a public repo — it would publish the whole roster.
- Repo secrets, and what each one does:

  | Secret | Effect |
  |---|---|
  | `GDRIVE_SERVICE_ACCOUNT_JSON` | the robot's Google key |
  | `ROSTER_ID`, `L2_ID`, `ATTENDEE_FOLDER_ID` | what to read |
  | `STORE_FOLDER_ID` | the Shared Drive to publish the store into |
  | `CURRICULUM_ID` | optional — the Master Curriculum Schedule, drives the Forecast tab (§4c) |
  | **`PUBLISH_ROSTER`** | ⚠️ when truthy, `site_build.py` writes **every student's email and phone** into `site/roster/data/*.json` |
  | **`VERCEL_TOKEN`** + `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` | ⚠️ presence of the token makes every Monday run **deploy the site publicly** |

  Setting `PUBLISH_ROSTER` **and** `VERCEL_TOKEN` together publishes student contact
  data to the internet, protected only by the Basic-Auth middleware in
  `site_build.py`. Verify that gate in a private window before ever doing so.

## 7. Deployment state — snapshot, 2026-08-12, verify before relying on it

- **Streamlit Community Cloud**: **https://attendance-suite-houseofedtech.streamlit.app/**
  — repo `mukram-web/attendance-suite`, branch `main`, main file `attendance_app.py`.
  **Confirmed in store mode 2026-08-12** by loading it signed-out: caption read
  "🟢 Prebuilt data · data as of 12 Aug 2026, 01:28 IST" (the pipeline's build time,
  not its own crawl) and the Day-1 tab rendered 20 tiles / 104 bars for B32–B35.
  ⚠️ **It is PUBLIC — it loads with no login at all**, and its Roster tab has a
  "Show full contact details" checkbox over student emails and phones. Restrict via
  the app's Settings → Sharing if that is not intended. An older `attendance-marker`
  app from the previous design is also still deployed and should be retired.
- **The store lives on a Shared Drive** ("Attendance suit") with the service account
  as Content Manager, holding `attendance.duckdb` (~7.4 MB) and
  `Master_Batch_Rosters_marked.xlsx` (~8.5 MB). A full run **with upload** was
  verified on GitHub 2026-08-11: all 8 steps green, both files landed, and the app
  downloaded the store after the local copy was deleted.
- **GitHub Actions**: `Refresh dashboard data`, Mondays 06:00 IST
  (cron `30 0 * * 1`), plus manual "Run workflow" with a `skip_upload` dry-run input.
  Verified end-to-end 2026-08-11.
- **The website is built every run but only published if `VERCEL_TOKEN` exists.**
  At the time of writing that secret was not set, so `site/` is discarded on the
  runner — but the deploy step is live code, so adding the secret is all it takes.
  The owner decided (2026-08-10) not to host it: Vercel's free Hobby plan forbids
  commercial use, so hosting means Vercel Pro or a port to Cloudflare Pages, which
  is free and permits commercial use. `SETUP_PIPELINE.md` documents the Vercel path.

## 7b. Open threads — as of 2026-08-12 (delete each line once it is done)

1. ~~**The Zoom extractor emits inconsistent formats.**~~ **FIXED 2026-09-06**
   (`065c0ab`). Both parsers now refuse a file that yields nobody, and
   `pipeline.py` refuses to publish rather than marking a batch absent —
   overridable with `--allow-partial`. The gate keys on the PARSE RESULT, not the
   shape, so a tab-delimited or UTF-16 report is caught too. `is_attendee_report`
   only chooses which cause to name. Re-measured the same day: all 628 cached
   attendee reports parse and carry phone, and 0 flat lists remain.
   ⚠️ **The gate makes an unsupported format expensive.** A format nobody has
   taught the parser now fails the whole weekly run instead of losing one
   session. The other Be10x app (`jvbhatt18-tech/weekly-sessions-analysis`)
   parses a second shape from an internal RTK tool
   (`Participant ID, Name, Joined at, Left at`); this repo does not, and 0 such
   files are on the drives today. If one lands, expect a red run.

2. **Decide whether the app should stay publicly readable** (§7). It currently is.
3. **Retire the old `attendance-marker` Streamlit app** — previous design, still
   deployed, causes confusion.
4. **Combined-batch sessions are missing from L2.** e.g. `AI CAP B1 - AI CAP B22`
   (19 Jun) and `AI CAP B10+B17` (23 Jul) have no L2 entry. Since 2026-08-29 they
   no longer drag averages down — `REQUIRE_L2` hides them (§5.6) — but they are
   still **absent from the dashboard**, which is only right if they genuinely did
   not run for that batch. Adding the missing L2 rows is still the real fix.
5. **`intro_attendance.json` stops at B31**, so B32–B35 have no "Intro call" row.
   Refreshing it means re-pulling from the "L2 customer" sheet.
6. **The roster is ~8.2 MB and Google refuses to export a Sheet over 10 MB as
   .xlsx.** When it crosses, step 1 fails every week with a clear message and old
   batches must be archived into a separate sheet. Watch the size in the log line
   `roster N bytes`.
7. **GitHub disables scheduled workflows after ~60 days of repo inactivity.** The
   only symptom is the "Data as of" date silently freezing — no failure email.
   Any commit, or a manual "Run workflow", resets the clock.

## 7c. Sheet data quality — real, and visible in the dashboards

These are data-entry issues, **not** parsing bugs (column detection was verified):

- **B32's `Close Type` column is empty** — 3,300 blanks + 436 `LWB_Resume`, no BDA
  values at all, so its "BDA closed" tile reads 0.0%.
- **B32's `Payment`** shows only **5** `Full Paid` against 2,619 `Partially Paid`
  (with ₹26k amounts) — recorded differently from every other batch.
- **B35 has 249 rows with the number `0`** in `Close Type`, bucketed as `(blank)`.

Unknown buckets are deliberately kept as their own bar rather than dropped, so
these show up on the page instead of vanishing.

## 8. Working on this

```bash
# run the app
streamlit run attendance_app.py

# rebuild the data locally, exactly like the CI dry run
python pipeline.py --no-upload --no-site

# tests — stdlib unittest; pytest is NOT in requirements.txt
python -m unittest tests.test_data
```

Use `--no-site` locally unless you mean to rebuild the site: `site_build` starts by
**deleting the whole `site/` directory**, and with `PUBLISH_ROSTER` set it writes
student contact data to disk.

Run the real job on GitHub (needs `gh auth login` once):

```bash
gh workflow run "Refresh dashboard data" --repo mukram-web/attendance-suite
gh workflow run "Refresh dashboard data" --repo mukram-web/attendance-suite -f skip_upload=true
gh run watch <id> --repo mukram-web/attendance-suite
```

**Health check, 5 seconds:** open the app and read the caption under the title.
`🟢 Prebuilt data · data as of <date>` = healthy. `🟢 Live from Google Drive` = it
fell back to the slow path — `store_folder_id` is missing from that environment's
secrets, or the store could not be downloaded.

**What is inside `attendance.duckdb`:** `meta(key, value)` holding JSON blobs
(`DATA`, `summary`, `report`, `warnings`, `source`, `generated_at`,
`generated_at_iso`, `batches`, `sheet_map`, `marked_xlsx_file_id`, `stamps`,
`day1`, `forecast`, `recap`, `trainers`, `sessions`), the `compute` table (per batch × session), and one `grid_<batch>` table per
batch. The `grid_*` tables carry emails and phones — that is why the store is
PII and lives in a private Shared Drive. The app caches it with a **30-minute TTL**,
so Monday's rebuild reaches viewers on its own; 🔄 Refresh forces it immediately.

Local runs read `.streamlit/secrets.toml` and the service-account `.json` key next
to the code; CI uses env vars instead. `.cache/` holds the attendee byte cache and
the exported sheets — safe to delete, just slower afterwards.

When a new batch starts, **nothing needs changing**: the batch tab appears in the
roster, and the next Monday run picks it up in both the dashboard and the day-1
analysis (which always covers the newest `DAY1_BATCHES` batches).
