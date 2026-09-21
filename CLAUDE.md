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

It reads the Zoom attendee reports for a week's live sessions, marks
Present/Absent into a copy of the Master Batch Rosters, and publishes the data a
Streamlit app shows. Since 2026-09-10 it runs when a human asks it to — the owner
uploads the week's exports on the dashboard's **Add data** tab and that
dispatches the job — and it marks only the sessions that are not marked yet
(§4g). There is no schedule.

## 2. The architecture that matters

The heavy work runs **off** the Streamlit server. This is the single most
important thing to understand:

```
 app "Add data" tab                    GitHub Actions              private Shared Drive        Streamlit app
  upload Zoom exports  ──► Drive  ──►  refresh.yml (dispatch)  ──►  attendance.duckdb    ──►  attendance_app.py
  + dispatch the run                    runs pipeline.py             (+ marked .xlsx,          downloads & renders
                                        fetch → mark → build          which is next week's     ~15-30s, no marking
                                                                      BASE — see §4g)
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
| `pipeline.py` | the job: fetch → mark → build store → **render `site/`** → upload. Flags: `--incremental` (mark only what is unmarked — §4g), `--no-upload`, `--no-site`, `--allow-partial`, `--mode`, `--no-cache`, `--cache-file` (§4f). |
| `attendance_app.py` | the Streamlit app (tabs: Dashboard, **Sessions** (Browse / This week / Trainers), **Weekend Recap**, Roster, Forecast, **Add data**). Reads the store; does not compute. |
| `attendance_core.py` | the marker engine: parses Zoom reports + L2, writes Present/Absent into the workbook. |
| `dashboard_core.py` | `compute()` (per batch × session) and `roster_grid()` (per-student grid). |
| `data.py` | pure data layer → the `DATA`/`summary` objects the dashboard renders. No I/O, unit-tested. |
| `dash_view.py` | Plotly + Streamlit rendering. Its figure builders and HTML fragments are **pure** so `site_build.py` can reuse them. |
| `forecast.py` | predicted attendance for sessions that have not run yet: decay curve x batch offset x pod multiplier. Pure, no I/O, unit-tested. See §4c. |
| `derived_cache.py` | the per-file parse memo that makes the weekly run incremental. Pure, unit-tested. See §4f. |
| `live_data.py` | all Google Drive I/O + the disk caches. |
| `sheets.py` | L2 webinar→topic lookup. |
| `polls.py` | Zoom poll exports -> session/trainer/recommend ratings, the 1-5 histograms and NPS. `nps_from_dist` is the ONLY place the promoter/detractor split is written down. `parse_responses` + `split_by_roster` divide a shared webinar's poll between its batches (§4e). |
| `sessionmeta.py` | duration, peak, the per-minute retention curve and stickiness, swept from the attendee report's own join/leave times. Pure, unit-tested. See §4e. |
| `recap.py` | the week just gone, scored as a RESIDUAL against the decay curve. Pure, unit-tested. See §4e. |
| `trainers.py` | per-trainer rollups + identity resolution (91 L2 spellings -> 63 people). Pure, unit-tested. See §4e. |
| `carryforward.py` | last week's marked columns carried onto this week's roster export — the base workbook for `--incremental`. Pure, unit-tested. See §4g. |
| `ingest.py` | the **Add data** tab's engine: reads the uploaded file names, checks them against L2, puts them on Drive, dispatches the workflow. Pure parts unit-tested. |
| `lms_client.py` | the 10xstats LMS API: `/batches`, `/customers`, retry + backoff, key handling. No pagination and 504s under load — see §4h. |
| `lms_roster.py` | builds the roster workbook from that API instead of the Sheet, retaining everyone already in the marked workbook. Pure, unit-tested. **Opt-in** via `roster_source` — see §4h. |
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

### 4b. BSIAI — detected and excluded (programme removed 2026-09-22)

The BSIAI programme's own dashboard is **gone**: `bsiai.py`, `tests/test_bsiai.py`,
the `BSIAI_ROSTER_ID` config, `live_data.fetch_track_attendees`, the pipeline's
`[5b]` step, the store's `bsiai` section and the `BSIAI_Roster` archive snapshot
were all deleted on 2026-09-22.

What REMAINS is deliberate. BSIAI still shares the L2 schedule and the attendee
Shared Drives with AI CAP, so the code must go on recognising it — not to report
it, but to keep it OUT of AI CAP's numbers. Do not "simplify" any of this away:

1. **`_track()` must classify BSIAI before CAP.** Its folders read `BSI B1`,
   `bsi b2`, `BSIAI B1`; L2 reads `BSIAI B1`. Without the BSIAI branch every one
   of them parses to `('CAP', 1)` — the same key as AI CAP B1. Verified: adding
   the track moved exactly 25 of 1,188 L2 webinars, all to BSIAI, none away.
   `tests/test_extract_batches.py` is the guarantee, and it covers BOTH paths:
   `TestProgrammes` pins the L2/folder label, and `TestTrackNamed` pins
   `_track_named` itself, which is what `lms_roster` reads a roster TAB with.
   Delete the branch and three of those fail. (`test_lms_roster.
   test_bsiai_tabs_are_not_cap` does NOT guard it - measured 2026-09-22, it
   passes with the branch removed.) A tab whose track is unrecognised defaults
   to CAP, so dropping the branch would invent phantom AI CAP batches.
2. **One label can name two programmes.** `AI CAP B40 - Common + BSIAI
   Accelerator B1` is an AI CAP session sharing a room with a BSIAI batch.
   `extract_batches` reads each item separately and returns BOTH `('CAP', 40)`
   and `('BSIAI', 1)`; folding them together put the whole segment under BSIAI,
   invented a `BSIAI B40` that does not exist and lost AI CAP B40.
   `polls.batch_label` is what names the other room `BSIAI B1` on the shared
   -session view. (The COMPLEMENT rule - an unlabelled room being the batch
   minus that day's PODs rather than the whole batch - is a different thing
   entirely, lives in `data.build_batch`, and is about POD rooms inside ONE
   AI CAP batch, not about sharing with another programme.)
3. **Folder names are lowercase on one drive.** The word-boundary batch-number
   pattern was case-sensitive in both `extract_batches` and `_folder_batches`;
   both now pass `re.I`. Verified across all 1,048 top-level folders: exactly 7
   changed, all `bsi b2`.
4. **The same session sits on both Shared Drives** ("Weekly Sessions Files" as
   `BSI B1`, "Zoom extracts" as `BSIAI B1`) and sometimes twice on one of them.
   `live_data.dedupe_by_webinar` keys on webinar id and keeps the copy naming the
   most people — the copies differ by a row or two.

`MM-AI B1` is a third programme again, distinct from both; it has no session
folders on Drive at all.

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
renders that week verbatim — attendance, forecast and roster all as
they stood. A yellow banner sits on the PAGE, not just the sidebar, because
someone screenshotting a number from a historical week must not be able to
mistake it for today. Two things to know:

- Snapshots load through `_load_snapshot`, deliberately NOT `_load_store`. They
  are immutable so they cache permanently rather than on the live store's
  5-minute TTL, and they must never be written to `_STORE_PATH` — that would
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

**The retention curve is a concurrency sweep, and it ends one minute early on
purpose** (`sessionmeta.measure`). The last leave always lands in the final
bucket, so the raw sweep always finished on an empty minute — measured
2026-09-07, 521 of 521 curves in the live store, exactly one zero each. That
minute is an artefact of the bucketing, not a minute anyone sat through, and
counting it divided every stickiness tail by one minute more than existed:
**−5.4 points on the 10-minute figure and −2.5 on the 30-minute**, every session,
in the same direction. The trim is pinned by a test. Any future statistic taken
off the tail of `retention` inherits this — check the last value before you
average it.

**The poll marker is measured, not logged.** Zoom's poll export timestamps every
individual response (`polls.submission_times`; present on 1,105 of 1,113 cached
files), so the Weekend Recap curve marks *when the room began answering* rather
than when a moderator says they circulated it. `pipeline.py` turns it into
`poll_at_min` = first submission − the curve's `t0`, where **`t0` is the first
JOIN, not the host's start** — using Zoom's `Actual Start Time` would slide the
marker several minutes. A marker that lands outside the curve is dropped rather
than clamped: an early bird answering a poll left open from the previous session
produces a negative minute, and minute 0 is a different claim from "no marker".

**One room, several batches: the poll is split per batch and the session is
counted once (added 2026-09-09).** From B35 a POD webinar is shared by every
batch at that point in the curriculum — L2 says `AI CAP B35 , B36 , B37 -
Finance` — and older batches shared whole rooms too (`AI CAP B17 + B21 11AM`).
Attendance is rightly one row per batch, but the poll used to be COPIED onto
each: B35, B36 and B37 all showed Finance 4.30 / 220 responses, and every rollup
counted it three times (46 such groups on the live store, `rating_n` 6.6% high,
35 of 70 trainers with inflated session counts). Two rules now, in two places:

1. **A batch row carries ITS students' answers.** `pipeline.py` step `[5a.1]`
   re-reads each shared poll per respondent (`polls.parse_responses`) and
   divides it between the sharing batches by roster email
   (`polls.split_by_roster`, `data.roster_emails`; the same `_cell_email` rule
   the marker uses). The row's `rating*` fields become that slice; the whole
   room lives once in `rating_shared.joint`, with `unmatched` (an email on no
   sharing roster — in the room's figure, in no batch's) and `multi` (enrolled
   twice — counted in each). Measured 30 Aug: ~85-90% of respondents match a
   roster. Polls carry email only, never phone. A long-form export names nobody,
   and **an anonymous Zoom poll exports `anonymous` in every email cell** (the
   6 Sep 2026 Finance poll: 220 answers, not one identity) — both keep the joint
   figure on every batch with `split: False, reason: 'no-emails'`. Ask the hosts
   to run the feedback poll non-anonymously, or the per-batch view cannot exist.
   Per-respondent rows are PII and roster-dependent — they are **never
   memoised** (§4f refuses them) and the bytes are re-fetched each run through
   `fetch_stream`'s disk cache.
2. **`recap.session_key` / `group_sessions` decide what one session is** —
   `(date, pod, shared_batches)`, where `shared_batches` comes from the SAME L2
   cell as `l2_batch` (data.py), so it holds whether or not a poll ran.
   `recap._agg`, `_awards` (except Beat the curve, a per-batch claim),
   `trainers.build`, the Sessions→Browse headline and the Weekend Recap
   breakdown all count rooms and take the poll from `joint_rating` — the joint
   figure when present, else the fullest identical copy, never a sum. Attendance
   stays over the batch rows. Fixtures that mean "several sessions" must vary
   the date: two rows on one (date, pod) in one batch ARE one session.

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

### 4f. The derived-facts memo (added 2026-09-08)

**The weekly run used to re-parse the entire Zoom corpus every Monday** — 1,290
attendee reports and 1,235 poll exports, ~246 MB — to recompute facts that had
not changed since the week they were first computed. `.cache/` is deliberately
not restored in CI (§6), so the runner started cold every time.

Two changes fixed it. Neither touches a gate, and neither caches anything that
depends on the roster, L2, or another file.

**(A) `derived_cache.py` memoises the PER-FILE parse**, in one
`derived_facts.json.gz` (~0.6 MB) in the store folder root on the private Shared
Drive. Only the output of functions pure in one file's bytes:
`sessionmeta.parse_header + measure` and `polls.parse_one`. Measured with a warm
disk, the PARSE alone — never mind the download — is `measure` 215.4s,
`parse_header` 16.1s, `submission_times` 22.1s, `parse` 12.9s, against 1.4s of
disk read. So caching the *bytes* would have missed the point; the cost is the
parse, which is why the memo holds the derived fact instead.

**(B) `dashboard_core.compute` and `roster_grid` take `tabs=`.** `build_store`
already materialises the whole marked workbook to build DATA, then handed the
raw bytes to both, each of which re-streamed the same 80k rows out of the zip.
Measured on the real 7.9 MB workbook: **43.6s → 0.8s and 47.3s → 1.7s, both
`.equals()`-identical**, pinned by `tests/test_dashboard_core_tabs.py`. Not a
cache at all — pure de-duplication.

**Five things that would each break this silently, and what stops them:**

1. **The file id is NOT a sufficient key.** Drive's "Manage versions → Upload
   new version" keeps the id, and that is the documented remedy for a bad Zoom
   export (§7b.1). `live_data`'s byte cache keyed on the bare id on the strength
   of a *comment* — a claim, not a check, and falsified by `upload_to_folder` in
   the same module. The memo keys on `id:md5-or-modifiedTime:name`, and the byte
   cache is now signature-addressed too, so a replaced file misses both.
2. **Never memoise anything derived from the file NAME.** `sessionmeta` reaches
   a session through `_mm` and polls through `(wid, mm)`, both parsed out of the
   filename. `derived_cache` REFUSES a row containing them (never strips it —
   stripping would store cleanly and then have every hit dropped by
   `lookup_by_session`, blanking duration, peak, retention and stickiness on
   every session while the run stayed green). The pipeline re-derives `_mm` from
   the live listing on hit and miss alike.
3. **The rule that produced a fact is part of the fact.** Each namespace stores
   `sha256` of its producing module's whole SOURCE and is discarded entire when
   that changes — the whole module, never one function, because `measure`
   depends on `_ts`, `_TIME_FMTS` and `MAX_MINUTES`. Two changes last month
   would each have poisoned a naive cache: the phone fix (+8.4% on every
   re-marked session) and the curve trim (−5.4 points on stick10 corpus-wide).
   It fails CLOSED — an unreadable source yields a value that cannot match.
4. **Values are validated, not just key names.** A name allow-list would happily
   pass `{"topic": "<every attendee's email>"}`. `_validate` checks shapes and
   ranges on the way in AND on the way out, and treats a bad row as a miss
   rather than trusting it — the memo is a *mutable* input to published numbers.
5. **Ordering.** Both dedupe tie-breaks keep the incumbent, and **119 of 320
   duplicated webinars TIE** on `unique_viewers`. Cached and freshly-parsed rows
   are therefore sorted back into listing order before `collect()`/`dedupe_rows`
   run, or a tie would resolve differently week to week.

**GATE 5 (`_previous_coverage`) is the new safety net.** `[5a]` and `[5a2]` are
the only steps with no gate above them and a blanket `except` inside them, so a
failure there used to cost one column and still go green. That was fine while
those columns were recomputed from the exports weekly; it is not fine now a
memoised fact can reach a published number. The run now refuses to publish if
sessions-with-duration or sessions-with-a-rating fall below `COVERAGE_FLOOR`
(0.8) of last week's store. **Sessions do not vanish from the past** — the
corpus only grows — so a real drop is a bug upstream, never a fact about the
week.

**What is NOT cached, and must never be.** The whole-drive listing (it is the
change detector — a cache hit skips a download, never the question of what
exists). The marking, all 429 columns. Every denominator. `REQUIRE_L2`. Both L2
joins. `forecast.fit_curve` and its backtest. `recap`. `trainers`.
**Measured total cost of that entire model layer: 0.18 seconds.** There is
nothing to save there and everything to lose — recomputing it weekly is exactly
what let the phone fix reach three months of past sessions.

Escape hatches, all reaching a correct ~6-minute cold run: delete the file,
`--no-cache`, the `no_cache` workflow input, a schema bump, a rule-hash
mismatch, or any read error at all. `--cache-file <path>` runs the whole memo
offline, which is how the cold-vs-warm store diff is tested — otherwise the warm
path's first execution would also be its first publish. `--allow-partial` skips
the write, so a knowingly incomplete week leaves no residue. Entries carry
`first_seen` and expire after 90 days, so ~8% of the corpus is re-derived from
scratch every week with nobody remembering to do anything.

### 4g. The freeze: sessions are marked once (added 2026-09-10)

**The Monday cron is gone and nothing is rebuilt automatically.** The owner
supplies each week's Zoom exports by hand, the dashboard's **Add data** tab puts
them on Drive and dispatches the workflow, and `pipeline.py --incremental` marks
ONLY the sessions that are not marked yet. Every other column is copied forward
untouched. This was decided twice, with the cost stated both times, and it is a
deliberate reversal of the rule in §5.11.

**What it costs.** A counting fix no longer reaches the past. The last-10-digit
phone fix (§5.3) lifted three months of sessions by 8.4% precisely because
everything was re-marked weekly; under the freeze it would have repaired only
the week it shipped. B29+ will accumulate frozen columns exactly as B17-B28
already carry 133 of them — the mechanism is now intended rather than accidental.
**The remedy is one command, and a human has to choose it:** dispatch the
workflow with `incremental` unticked (or run `python pipeline.py` with no flag)
to re-mark every session from the Zoom exports.

**The marked workbook is now the system of record.**
`Master_Batch_Rosters_marked.xlsx` in the store folder is not just an output any
more — it is next week's base. Three consequences:

1. It is uploaded at step `[8a]`, *after* every gate, not at `[5/8]`. Publishing
   it earlier froze sessions the store never published, and the next incremental
   run would skip them because their columns were already in the base.
2. `--incremental --allow-partial` deliberately does NOT update it. That flag
   means the week is knowingly incomplete; freezing it would make a one-off into
   permanent history.
3. `archive/` (§4d) is the only route back. **Restore:** download
   `archive/Master_Batch_Rosters_marked_<date>.xlsx`, upload it over
   `Master_Batch_Rosters_marked.xlsx` in the store folder (Drive → Manage
   versions → Upload new version, so the file id the store points at survives),
   then dispatch a run. Every session between that date and now whose Zoom
   folder is still on Drive is re-fetched automatically, because its column is
   absent from the restored base. What cannot be repaired is the archived stores
   for the bad weeks: they are immutable by design.

**`carryforward.merge_marks` is the whole design**, and its two rules are what
make it faithful to what a full re-mark would have produced:

- **Identity is the marker's identity** — registered email, then the whole
  registered number, then its last ten digits. Not the row number: rows move
  every week. Measured 2026-09-10: 61,865 of 61,865 students matched.
- **A student who joined after a session is `Absent` for it, but only in scope.**
  A full re-mark writes `Absent` for any enrolled student not in that session's
  attendee list, so this keeps every historical number identical when the mode
  changes. Published percentages are `present ÷ today's strength` either way, so
  blank and `Absent` publish the same figures; what `Absent` protects is
  `data.build_batch`'s "marked > 30% of strength" validity gate, which drops a
  column silently. On a POD column only that POD's students are filled — the
  marker leaves everyone else empty, and filling them would put ten false
  absences against every student on an eleven-POD day.

**`attendance_core.session_key` is the ONE definition of what a session column
is**, shared by the marker's `dmap`, the carry-forward and the "already marked,
do not download again" check. It is `(date, POD)`, with the full date when the
header carries a year and the year-blind `MM_DD` only for legacy hand-typed
B17-B28 headers. Three copies of this rule drifted apart within a day of each
other:

- keying year-blind collided `9th May` (2025) with `2026_05_09`, and
  `setdefault` dropped one of them — on a freeze, a session gone for good;
- keying without the POD meant marking any one POD suppressed the download of
  the other ten on that date, which is a whole day missing from a green run;
- `pods.from_folder` could not read the unspaced `AI CAP B37 8PM-Techis` /
  `AICAPB35, B36-Generalist` folder forms, so 53 of 609 already-marked sessions
  were re-downloaded and re-marked every run — they were not frozen at all.

**Measured against a full run, 2026-09-10.** Built both workbooks from the
same 611 attendee files (full = all of them onto the pristine roster;
incremental = the 58 newest onto the carried base) and compared every session
column cell by cell:

| | |
|---|---|
| session columns | **568 in both, none missing either way** |
| students matched by the carry | **61,865 of 61,865** — 0 new, 0 dropped |
| marks carried | 1,169,292 across 383 columns on 23 tabs |
| folders the fetch skipped as already marked | 606 of 613 |
| cells that disagree | **4**, on two B35 POD sessions of 23 Aug |

The four are not lost data. Both remaining residuals have the same cause: **one
session can sit in TWO folders whose names disagree**, because the two Shared
Drives name the same session differently — `2026-08-23 - AI CAP B35 - Educators
- Blueprint to Launch...` on one and `2026-08-23 - AI CAP B35 - Blueprint to
Launch: Designing Courses with AI` on the other. Only the first names the POD,
so the POD-aware skip suppresses that one and queues the other; the two modes
therefore mark the column from *different copies of the same Zoom export*, and
those copies genuinely differ by a row or two (§4b.4). Same for the one column
that exists only in the incremental workbook — webinar 99261543123, which is
**not in L2 at all**, so `REQUIRE_L2` hides it from the dashboard either way.

The exact fix, if those four cells ever matter: resolve a folder's POD through
L2 by webinar id rather than from the folder name, so the skip decision and the
marking use the identical rule. It costs listing the contents of all ~613
folders every run (no downloads) and would also remove the ~39 already-marked
sessions that are currently re-fetched and re-marked because their folder name
spells the POD in a way `pods.from_folder` still cannot read. Both are cost and
noise, not error — and the direction is safe: nothing is ever skipped that has
not been marked (`this-week-but-not-queued: 0` on every run measured).

**GATE 6 is the freeze's only detector.** A carry-forward that mismatches
students writes `Absent` over real Presents and nothing would ever correct it.
The one observable symptom is a past session's present count going DOWN, which
cannot happen legitimately, so any drop on a session last week's store published
refuses the publish. A session that DISAPPEARS is reported, not gated: an L2 row
being corrected legitimately hides one (§5.6), and gating that would go red
every time somebody fixed the schedule.

**Known, accepted, not yet solved:**

- **A carried column whose Zoom export is later deleted from Drive loses its L2
  topic** and is then hidden by `REQUIRE_L2` — `sheets.webinar_topic_lookup` is
  keyed off the attendee file names still present on Drive. The carried workbook
  is not self-sufficient. Persisting the webinar id per session column would fix
  it.
- **A pod switcher's old mark in their former pod's column stops being counted**
  (`data.build_batch` counts only rows currently in that POD), so historical pod
  attendance erodes with churn. Pre-existing and identical before the freeze —
  the marker never cleared those cells either — but worth knowing, because it is
  a number that moves with nobody touching the data. Measured pod churn over the
  week to 2026-09-10: zero across all 48 pod/batch pairs.
- **A SECOND session on a date+POD a batch already has** is skipped forever
  rather than merged, because the fetch sees that key as marked. Today's marker
  would have merged both into one column, so this only differs for two webinars
  on one day for one batch and pod.

### 4h. The roster from the LMS API (added 2026-09-16)

**Status: built, tested, OFF.** `roster_source` defaults to `sheet`. Turning it on
is one environment variable (`ROSTER_SOURCE=lms`), and turning it off again is the
same variable — no code change, no redeploy.

**Why.** The Sheet is the last hand-maintained input and it is ~8.2 MB against
Google's 10 MB export ceiling (§7b.6). The 10xstats LMS API
(`https://10xstats.com/api/v1/lms`, `X-API-Key` header) has the same enrolment
data and no ceiling.

**Why it is only a read-side swap.** Since B29 the pipeline has never written
marks back to the Sheet. They live in `Master_Batch_Rosters_marked.xlsx` and
`carryforward` re-attaches them by IDENTITY, never row order (§4g.4). So the Sheet
was only ever an enrolment-and-attribute source. `lms_roster.build_from` produces
the same workbook shape and everything downstream — marker, freeze, GATE 6, store —
is untouched.

**The three layers, and why layer 1 is not optional.** Each tab is retained rows
(everyone in last week's marked workbook) + an API refresh for the live cohorts +
an API seed for brand-new batches. Measured 2026-09-15: **9,017 people across
B17-B39 are in the roster but NOT in their own API batch** (3,355 are really in a
different CAP batch, 556 only in non-CAP workshops, 5,038 nowhere). Build the
workbook from the API alone and `carryforward` counts them `unmatched_prev` and
bins their marks. The gap is era-dependent: ~500 per batch on B23-B28 (~20%), but
only 113 on B38 and 117 on B39.

**Refresh policy (owner's rule).** Extract once, then each week refresh only the
current cohort and the previous one; everything below is frozen and costs no API
call. This is the §4g philosophy applied to enrolment, and it also stops a refund
logged today flipping a student's Active flag on a session from three months ago.
It matters for cost too: a full sweep is ~95 batch records against an endpoint
that 504s under load.

**⚠️ `closingType` is NARROWER than the Sheet's and this is visible in production.**
The API returns only `unknown` / `bda_collection` / `system` / `l3_purchased`. It
has no **BDA Closing** and no **Old Customer**, and collapses both into `unknown`.
B40 was pasted in from the API by hand on 14 Sep and reads **Unknown 58%**, against
B39's Sheet-sourced BDA Collection 39% / System 28% / Old Customer 27% / BDA
Closing 6% / Unknown 0%. So `lms_roster._apply` takes the API's closing type ONLY
when it maps to something real: a retained `BDA Closing` is never overwritten with
a blank. New students the Sheet never saw still land in Unknown — **that is an
upstream gap to raise with the LMS owner, not something this code can fix.**

**Other API facts worth not rediscovering.** No `amount` field —
`transactionLedger` is a LIFETIME history across every product (one account: 1,359
entries, ₹34M), not this batch's deal value; nothing reads the roster's Amount
column anyway, and retained rows keep theirs. No pagination: `?limit`/`?page` are
accepted and ignored. No CORS header, so no browser can ever call it. From B35 the
LMS stores one batch record PER POD, named byte-identically to the roster's own POD
cell, so POD comes free through `pods.canon` — including the `Enterpreneurs` typo.
Match on the full name `AI Career Accelerator Program B<n>`, **never the bare
number**: numbers are reused across programme generations and 2025's `AICA IC B19`
shares zero of 1,161 people with 2026's B19.

**Files.** `lms_client.py` (retry/backoff, key handling), `lms_roster.py` (the
builder), `tests/test_lms_roster.py` (45 tests, mostly pinning the workbook shape
because every mismatch there fails silently), `tools/lms_cutover_diff.py` (the
before/after gate — run it before switching; `Lost` must be 0),
`tools/make_lms_sheet.py` (builds/refreshes the Google Sheet that REPLACES the
roster — note it refreshes EVERY batch, unlike the weekly freeze), and
`tools/extract_roster_facts.py` (freezes the Sheet-only facts, above all Closing
Type, before the Sheet stops being read).

**Config keys** `pipeline.load_config` actually reads (env name, then the
`[drive]` key): `ROSTER_SOURCE`/`roster_source` (`sheet`|`lms`, default `sheet`),
`LMS_API_KEY`/`lms_api_key`, `LMS_LIVE_BATCHES`/`lms_live_batches` (override for
which cohorts refresh), `LMS_CACHE_DIR`/`lms_cache_dir` (local payload cache —
leave unset in the weekly job, it must see live enrolment) and
`LMS_PURE`/`lms_pure` (API only, NO retention — side-by-side datasets only,
never the workbook that becomes next week's base). Plus the `STORE_OUT`
environment variable, which redirects the store filename and **refuses a plain
upload**, because `[8/8]` and `[8b]` both publish under `attendance.duckdb`.
Pair it with `--no-upload` to keep the parallel store local, or with
`--publish-parallel` to upload it to the store folder **under its own name** so
the deployed app can offer it as a second data set. `--publish-parallel` takes
an early return at `[8/8]`: it never writes next week's base (`[8a]`) and never
archives (`[8b]`), because those belong to the weekly record alone.
`pipeline.parallel_store_name` is the single place that decision is made, and
`tests/test_parallel_publish.py` pins every combination.

The app decides whether to OFFER the second data set differently in each
deployment, and getting this wrong is why the control was missing on Streamlit
Cloud entirely: locally the store folder is unset and the file is an artefact on
disk, so `_LMS_STORE_PATH.exists()` is the truth; deployed, `.cache/` and
`*.duckdb` are both gitignored so nothing is ever on disk, and the truth is
`live_data.store_exists` — a Drive **metadata probe, never a download**, since
the app asks on every page load.

**GATE 0**, in `pipeline.py` INSIDE the `--incremental` block at `[1c]`,
refuses to publish when `carry["unmatched_prev"]` is non-zero under
`roster_source=lms`. It therefore does NOT apply to a full run: without
`--incremental` there is no carry-forward and no `unmatched_prev` to read. It is
also skipped under `lms_pure`, where dropping people is the point. It exists because
**GATE 6 is structurally blind to this migration**: `lost_marks` restricts its
comparison to students present in BOTH weeks' rosters, so a student who disappears
from the roster entirely is invisible to it. `unmatched_prev` is the only signal
that sees them.

**Verified end to end, 2026-09-16** (`ROSTER_SOURCE=lms pipeline.py --incremental
--no-upload --no-site`): 24 tabs frozen and 2 seeded, 596 columns carried,
1,304,589 marks, 62,428 students matched, **`unmatched_prev` 0**. Every batch's
`strength` and `active` identical to the live store (65,484 enrolled / 63,635
active); session counts only rose, from that run's 21 newly marked sessions.

**⚠️ The one thing it surfaced, and it is NOT an LMS problem.** The run was stopped
by GATE 6 on `B20 2026_06_14: 3 → 385 present`. `AI CAP B20` has **two** columns
headed `2026_06_14` (workbook indexes 23 and 25, with `2026_06_13` between them):
the real marked one has 385 Present, a stray duplicate has 3, and they do not
overlap. `carryforward._sheet_session_cols` keys on `session_key` and correctly
keeps the first, so the LMS path publishes 385 — but `dashboard_core.roster_grid`
builds each student as a **dict** (`rec[label] = mark`), so two columns sharing a
header collapse and the LAST one wins. **The live store has therefore been
publishing 3 for a session where 385 attended.** The Sheet path hides this because
its fresh export still carries both columns. **Swept 2026-09-17: this is the ONLY
duplicate session key in the whole marked workbook** — one column, one batch, one
date. Fix the grid before switching sources.

## 5. Invariants — break these and the numbers go silently wrong

1. **Locate roster columns by HEADER TEXT, never by fixed letter** (see §4).
   Note `dashboard_core.py` is the weakest implementation: it matches header names
   by *exact* equality and falls back to a hardcoded index if "payment" is missing.
   Prefer the substring matching used in `attendance_core.py` / `data.py`.
2. **Phones from openpyxl are floats** (`919704189186.0`). Strip the `.0` BEFORE
   removing non-digits, or every phone shifts a digit and phone matching dies
   silently. This bug once made a day-one reading come out 53.9%, not 58.0%.
3. **Match attendees on email OR phone, comparing the LAST 10 DIGITS.**
   The last-10 comparison applies in BOTH modes — via
   `attendance_core._phone_hit` in `exact`, and inlined in the `inclusive`
   branch, which has NO full-string fallback for sub-10-digit numbers. It did
   not until 2026-09-06, when `exact` mode compared the whole digit string.
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
   unidentified-refunded. Canonicalise on meaning. Beware: **two independent
   vocabularies exist** — `data._REFUND_TOKENS`/`normalize_closing` (Dashboard)
   and `dashboard_core._REFUND_HINTS`. They do not agree (e.g. "Cancelled" counts as
   inactive in `data.py` but active in `dashboard_core.py`). Unifying them is
   worthwhile; until then, know which module governs the number you are changing.
5. **Marketing walkthroughs are never in L2.** They share the batch folder
   naming, so a folder name alone cannot tell a real class from a walkthrough —
   L2 membership can. A title guard may FLAG but must never VETO (real topics
   contain "onboarding"). Verified across B31–B35, 28 sessions, zero false
   negatives.
6. **L2 is the register of what ran — the dashboard shows nothing else.**
   `data.REQUIRE_L2` (owner's rule, 2026-08-29) drops any session column whose
   webinar has no L2 row — AI CAP stopped falling back to the folder name for
   display. The
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
10. **The pipeline refuses to publish partial data.** A failed folder listing
   or download, an attendee export that parsed to nobody (`ZERO_ATTENDEE_TAG`),
   a roster with no `AI CAP B<n>` tab to split shared polls by, a coverage
   collapse (GATE 5) or a lost mark (GATE 6) each exit non-zero and leave last
   week's store intact. `--allow-partial` overrides it; the workflow
   deliberately never passes that flag. Keep it this way — a green run that
   silently drops a weekend's session is worse than a red one.
11. **Cache PRE-join per-file facts only, and never the file's NAME.** Anything
   that touches the roster, L2, a denominator or another file is recomputed every
   run — measured at 0.18s for the whole model layer, so there is nothing to save
   and everything to lose (§4f). The line is exactly at `lookup_by_session`: the
   memo holds what one file's bytes say, and the joins, the tie-breaks and every
   fit run over the full set weekly. This is what lets a fix reach the past —
   the phone fix (§5.3) repaired three months of sessions precisely because they
   were being re-marked, and the 133 frozen B17–B28 columns are the standing
   counter-example of what a cache that is really a freeze does to your numbers.
   ⚠️ **Since 2026-09-10 this applies to DERIVED numbers only.** The owner chose
   to freeze the MARKS: `--incremental` carries them forward and never recomputes
   them (§4g). The model layer — ratings, recap, trainers, forecast — is
   still rebuilt from scratch every run, so a fix there still reaches everything.
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
  not its own crawl).
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
   `roster N bytes`. **§4h is the way out of this one** — the LMS API has no such
   ceiling — but it is opt-in and not switched on yet.
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

# the same, but marking ONLY unmarked sessions (what the Add data tab triggers).
# Needs store_folder_id configured, since that is where last week's marked
# workbook — the base it carries forward — lives.
python pipeline.py --no-upload --no-site --incremental

# tests — stdlib unittest; pytest is NOT in requirements.txt
python -m unittest tests.test_data

# build the roster from the LMS API instead of the Sheet (§4h). Opt-in, and the
# same variable set back to "sheet" is the rollback.
ROSTER_SOURCE=lms python pipeline.py --no-upload --no-site --incremental

# all of them - 476 as of 2026-09-22, ~45 s. `discover` DOES work from the repo
# root (do not pass `-t .`), and it is the only form that cannot silently skip a
# new test file, which a hand-maintained module list has done twice.
# test_dashboard_core_tabs is the slow one: it proves the bytes and tabs= paths
# agree by running the SLOW path too, which is the point of it.
python -m unittest discover tests
```

**Before switching `roster_source` to `lms`, run the cutover diff** (§4h). It
builds the roster both ways for the same week and reports, per batch, who is lost
or gained and how the Active count and closing-type mix move. `Lost` must be **0**
— those students' marks would be dropped by `carryforward`, and GATE 6 cannot see
it because `lost_marks` only compares students present in BOTH rosters. Needs
Drive and the LMS API; `--cache` makes a second run instant.

```bash
python tools/lms_cutover_diff.py --out cutover_diff.xlsx --cache .cache/lms
```

**The equivalence check is the one to run after touching the carry-forward,
`session_key`, `pods.from_folder` or the fetch's skip rule.** It builds a full
and an incremental workbook from the same corpus and diffs every session column
cell by cell — the only thing that can catch a freeze quietly rewriting history,
since there is no rebuild to correct it. It needs Drive and takes ~5 minutes;
the shape of it is in §4g, and the numbers it produced on 2026-09-10 are there
to compare against.

The cold-vs-warm proof for the derived-facts memo (§4f) is not part of the
suite — it needs Drive and takes ~13 minutes. Run it after touching anything the
memo feeds; the two stores must agree on every memoised field:

```bash
python pipeline.py --no-upload --no-site --no-cache --cache-file .cache/memo.json.gz
python pipeline.py --no-upload --no-site            --cache-file .cache/memo.json.gz
```

Use `--no-site` locally unless you mean to rebuild the site: `site_build` starts by
**deleting the whole `site/` directory**, and with `PUBLISH_ROSTER` set it writes
student contact data to disk.

Run the real job on GitHub (needs `gh auth login` once):

```bash
# incremental is the DEFAULT; pass it explicitly for a full re-mark of history
gh workflow run "Refresh dashboard data" --repo mukram-web/attendance-suite
gh workflow run "Refresh dashboard data" --repo mukram-web/attendance-suite -f incremental=false
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
`forecast`, `recap`, `trainers`, `sessions`, `cache`), the `compute` table (per batch × session), and one `grid_<batch>` table per
batch. The `grid_*` tables carry emails and phones — that is why the store is
PII and lives in a private Shared Drive. The app caches it with a **5-minute TTL**,
so an upload reaches other viewers on its own; 🔄 Refresh forces it immediately.

Local runs read `.streamlit/secrets.toml` and the service-account `.json` key next
to the code; CI uses env vars instead. `.cache/` holds the attendee byte cache and
the exported sheets — safe to delete, just slower afterwards.

When a new batch starts, **nothing needs changing**: the batch tab appears in the
roster, and the next run picks it up in the dashboard.
