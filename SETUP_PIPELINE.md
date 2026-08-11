# Fast online dashboard — one-time setup

The app no longer fetches and marks on Streamlit's server. Instead:

```
GitHub Actions (Mon 06:00 IST)  ──►  private Drive folder  ──►  Streamlit app
fetch → mark → build store           attendance.duckdb          downloads it,
(pipeline.py, ~3 min, free)          + marked .xlsx             opens in seconds
```

Three one-time steps make it live:

## 1. Create the private Drive folder

1. In Google Drive, create a folder, e.g. **"Attendance dashboard data"** (any
   private location is fine).
2. Share it with the robot account as **Editor**:
   `attendance-robot@fresh-delight-500710-g6.iam.gserviceaccount.com`
3. Open the folder and copy its ID from the address bar —
   `https://drive.google.com/drive/folders/`**`<THIS PART>`**

## 2. Add the GitHub secrets

In the GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**, add these five:

| Secret name | Value |
|---|---|
| `GDRIVE_SERVICE_ACCOUNT_JSON` | the whole service-account `.json` key file content |
| `ROSTER_ID` | the Master Batch Rosters sheet id |
| `L2_ID` | the L2 schedule sheet id |
| `ATTENDEE_FOLDER_ID` | the Weekly Sessions shared-drive id |
| `STORE_FOLDER_ID` | the folder id from step 1 |

(The first four values are the same ones already in
`.streamlit/secrets.toml` under `[drive]` — copy them from there.)

Then go to the **Actions** tab → "Refresh dashboard data" → **Run workflow** to
do the first build now instead of waiting for Monday. It takes ~3 minutes; when
it goes green, `attendance.duckdb` is in your Drive folder.

## 3. Point the deployed app at the folder

In the Streamlit Cloud app's **Settings → Secrets**, add one line inside the
existing `[drive]` section:

```toml
[drive]
# ...existing ids stay...
store_folder_id = "<the folder id from step 1>"
```

Do the same in the local `.streamlit/secrets.toml` if you run the app on your
machine.

That's it. From then on:

- The app opens in seconds for everyone, with **"Data as of …"** shown in the
  sidebar.
- Every **Monday 06:00 IST** the data refreshes itself (new weekend sessions
  included automatically).
- **🔄 Refresh from Google** in the app re-downloads the latest build.
- Need fresher data mid-week? GitHub → Actions → "Refresh dashboard data" →
  **Run workflow**, wait for green, then click 🔄 in the app.

## 4. (Optional) The website on Vercel

The pipeline also renders `site/` — the same dashboard as a **static website**
that opens instantly and never sleeps. Three pages: `/` dashboard, `/day1.html`
day-1 analysis, `/roster/` behind a login.

1. **Create the project.** On vercel.com → *Add New… → Project → skip the git
   import → deploy an empty project* (or run `npx vercel` once locally from
   `attendance-suite/site` and follow the prompts). Note its **Project ID**
   (Project → Settings → General) and your **Team/Org ID** (Account/Team →
   Settings → General).
2. **Create a token**: Account Settings → Tokens → *Create*, scope it to that
   team, copy the value.
3. **Add three GitHub secrets** (same place as the others):
   `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`.
   Until `VERCEL_TOKEN` exists the deploy step is skipped, so nothing breaks.
4. **Set the roster login** in Vercel → Project → Settings → Environment
   Variables (Production):
   `ROSTER_USER` = the shared id, `ROSTER_PASS` = the shared password.
   These are the credentials the browser asks for on `/roster/`.
   ⚠️ Changing them later **only takes effect on the next deploy** — after
   editing, re-run the workflow (or hit *Redeploy* in Vercel).
5. Run the workflow once and open the deployment URL.

### ⚠️ Prove the lock works before publishing any student data

**The first deploy contains no roster data on purpose.** `PUBLISH_ROSTER` is
off by default, so `/roster/` renders an empty page and no student contact
details are uploaded anywhere.

Verify the gate, in a **private/incognito window**:

1. Open `<site>/roster/canary.txt` — it **must** ask for a username and
   password. If it instead shows a line of text, **the gate is not working**:
   do not enable the roster, and tell me — the fix is to serve it from a Vercel
   Function instead of a static file.
2. Open `<site>/roster/` — same prompt. Cancel it; you should get 401, not the page.
3. Log in once and confirm both load.

Only then turn the roster on: add a GitHub secret `PUBLISH_ROSTER` = `1` and
re-run the workflow. From that point the per-student grid is on the site,
behind the login.

Notes:
- `site/` is **never committed** — the roster JSON holds student contact
  details. It is uploaded straight to Vercel by the Action.
- The dashboard and day-1 pages carry **aggregates only**, so the public URL
  exposes no student data. They are `noindex`, so search engines skip them.
- Vercel's free Hobby plan is for non-commercial use; a Be10x deployment should
  be on a paid plan.

## What to commit

`.gitignore` ignores `*.json`, so these need to go in together with the code —
push all of them or the CI build silently loses data:

```bash
git add .github pipeline.py day1_analysis.py day1_template.html SETUP_PIPELINE.md \
        requirements.txt .gitignore attendance_app.py live_data.py dashboard_core.py \
        dash_view.py site_build.py site_templates intro_attendance.json
```

`intro_attendance.json` holds **aggregates only** (no student contact details), so
it is safe in the public repo. If it is missing at build time the pipeline logs a
WARNING and the store has no "Intro call" rows.

## Notes

- **This repo is public.** The store, the roster export and the Zoom CSVs all
  contain student emails/phones, so: the Drive store folder must stay **private**,
  and the workflow deliberately has **no `actions/cache`** step (Actions caches
  are restorable from fork PRs — caching `.cache/` would leak the roster). Don't
  add one.
- **Scheduled workflows get disabled after ~60 days of repo inactivity.** The
  symptom is the sidebar's "Data as of" date quietly stopping. Re-enable in the
  Actions tab; any commit or a manual "Run workflow" resets the clock.
- A run **refuses to publish partial data** — if a session folder can't be listed
  or a file fails to download, it exits red and leaves last week's store intact.
  Just re-run it (successful downloads are cached within the run). `--allow-partial`
  overrides.
- **Roster size limit:** Google won't export a Sheet as .xlsx past **10 MB** and
  the roster is already ~8 MB. When it crosses that line the pipeline fails with a
  clear message and old batches need archiving into a separate sheet.
- `pipeline.py` also runs on a laptop: `python pipeline.py` (uses
  `.streamlit/secrets.toml` + the local key file; `--no-upload` builds without
  uploading). Don't run it while the local app is open — the app holds the store
  file; if a build is interrupted, delete `.cache/attendance.duckdb` and rebuild.
- If the roster gains a new batch tab, nothing to do — the next pipeline run
  picks it up automatically, in the main dashboard **and** in the Day-1 analysis
  (which always covers the newest `DAY1_BATCHES` batches — 4 by default; change
  that constant in `pipeline.py`).
- **Day one is auto-detected** as the earliest session the L2 schedule registers
  as a real class for that batch, so marketing walkthroughs are never mistaken for
  class 1. That means **a session missing from L2 is invisible to this tab** — if
  a batch shows "no session registered in L2 yet", the fix is to add the session's
  Webinar ID to the L2 schedule.
- The app re-checks Drive for a newer store every 30 minutes, so Monday's rebuild
  reaches viewers on its own; 🔄 Refresh forces it immediately.
