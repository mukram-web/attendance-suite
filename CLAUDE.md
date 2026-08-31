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
| `attendance_app.py` | the Streamlit app (tabs: Dashboard, Roster, Day-1 analysis). Reads the store; does not compute. |
| `attendance_core.py` | the marker engine: parses Zoom reports + L2, writes Present/Absent into the workbook. |
| `dashboard_core.py` | `compute()` (per batch × session) and `roster_grid()` (per-student grid). |
| `data.py` | pure data layer → the `DATA`/`summary` objects the dashboard renders. No I/O, unit-tested. |
| `dash_view.py` | Plotly + Streamlit rendering. Its figure builders and HTML fragments are **pure** so `site_build.py` can reuse them. |
| `day1_analysis.py` | day-one / latest-session cut of payment and close type for the newest `DAY1_BATCHES` (4) batches. |
| `day1_template.html` | **used by the live app** to render the Day-1 tab (and by the static site). Not dormant — do not delete. |
| `live_data.py` | all Google Drive I/O + the disk caches. |
| `sheets.py` | L2 webinar→topic lookup. |
| `bsiai.py` | the **BSIAI programme** — its own roster Sheet, its own batch numbers, no session columns. Computes attendance straight from the attendee reports. |
| `site_build.py`, `site_templates/` | the static-website build. See §7 — it is **wired to deploy**, not inert. |

## 4. Data sources

- **Master Batch Rosters** (Sheet) — one tab per batch, `AI CAP B<n>`.
  Typical layout is country code / registered number / registered mail / whatsapp /
  broadcast mail / batch / amount / **Payment** / **Close Type**, but **the column
  letters are NOT stable**: on most tabs Payment/Close Type are I/J, on `AI CAP B29`
  and `AI CAP B20` they are J/K, and the header row is row 1 on some tabs and row 2
  on others. Always locate columns by header text (§5.1).
- **L2 Weekly Live Sessions** (Sheet) — the schedule; maps Webinar ID → batch + topic.
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


## 5. Invariants — break these and the numbers go silently wrong

1. **Locate roster columns by HEADER TEXT, never by fixed letter** (see §4).
   Note `dashboard_core.py` is the weakest implementation: it matches header names
   by *exact* equality and falls back to a hardcoded index if "payment" is missing.
   Prefer the substring matching used in `attendance_core.py` / `data.py`.
2. **Phones from openpyxl are floats** (`919704189186.0`). Strip the `.0` BEFORE
   removing non-digits, or every phone shifts a digit and phone matching dies
   silently. This bug made day-1 attendance read 53.9% instead of 58.0%.
3. **Match attendees on email OR phone** — but the two matchers differ, so the
   Dashboard and Day-1 tabs can legitimately disagree on the same session:
   `attendance_core.py` in its default `exact` mode compares the **full** digit
   string, while `day1_analysis.norm_phone` compares the **last 10** digits.
   Email alone under-counts badly (B34 day 1: 1,594 vs 1,753).
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

1. **The Zoom extractor emits inconsistent formats.** A separate tool (built in
   another session, NOT this repo) writes to
   `C:\Users\user\OneDrive\Desktop\Zoom extracts\output\<YYYY-MM-DD>\<folder>\`.
   Measured:
   | session | format | phone | parses to |
   |---|---|---|---|
   | B25 · 9 Aug (`95638821501`) | ✅ Attendee Report | 100% | 487 |
   | B23 · 8 Aug (`99685353701`) | ✅ Attendee Report | 59.7% | 447 |
   | B17+B19 · 9 Aug (`91695866411`) | ❌ flat 8-column list | none | **0** |

   The flat format has `Name (original name), Email, Join time, Leave time,
   Duration (minutes), Guest, …` — no `Attended`/`First Name`, so
   `attendance_core.parse_attendees` returns **empty without raising**. If such a
   file reaches the drive, that session marks everyone absent and the run still
   goes green. Two sessions on the SAME day differ, so it is per-session (likely
   webinars without registration), not a version change.
   **Not yet fixed.** Options: fix the extractor to always pull the Attendee
   Report; and/or teach both parsers to read either shape **plus** fail loudly
   when a file parses to zero attendees.
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
`day1`), the `compute` table (per batch × session), and one `grid_<batch>` table per
batch. The `grid_*` tables carry emails and phones — that is why the store is
PII and lives in a private Shared Drive. The app caches it with a **30-minute TTL**,
so Monday's rebuild reaches viewers on its own; 🔄 Refresh forces it immediately.

Local runs read `.streamlit/secrets.toml` and the service-account `.json` key next
to the code; CI uses env vars instead. `.cache/` holds the attendee byte cache and
the exported sheets — safe to delete, just slower afterwards.

When a new batch starts, **nothing needs changing**: the batch tab appears in the
roster, and the next Monday run picks it up in both the dashboard and the day-1
analysis (which always covers the newest `DAY1_BATCHES` batches).
