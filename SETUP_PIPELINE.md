# Fast online dashboard — one-time setup

The app no longer fetches and marks on Streamlit's server. Instead:

```
GitHub Actions (Mon 06:00 IST)  ──►  private Drive folder  ──►  Streamlit app
fetch → mark → build store           attendance.duckdb          downloads it,
(pipeline.py, ~3 min, free)          + marked .xlsx             opens in seconds
```

Three one-time steps make it live:

## 1. Create the folder — it MUST be on a Shared Drive

> **This is the step that trips people up.** A normal folder in someone's *My
> Drive* does **not** work, whatever role you grant. When the service account
> uploads, it becomes the file's owner, and service accounts have **zero storage
> quota** — the upload fails with `storageQuotaExceeded`. On a **Shared Drive**
> the *drive* owns the files, so it works. (Verified 2026-08-11: a My Drive
> folder shared as Editor failed; the SA also cannot create a Shared Drive
> itself, nor write to one where it is only a Viewer.)

1. In Google Drive: left sidebar → **Shared drives** → **+ New** →
   name it e.g. **Attendance Dashboard**. (Or reuse an existing shared drive.)
2. Open it → **Manage members** → add
   `attendance-robot@fresh-delight-500710-g6.iam.gserviceaccount.com`
   as **Content Manager** (Viewer/Commenter cannot upload).
3. Copy the ID from the address bar —
   `https://drive.google.com/drive/folders/`**`<THIS PART>`**
   (the shared drive's own ID works; a subfolder inside it works too).

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
that opens instantly and never sleeps. Two pages: `/` dashboard and `/roster/`
behind a login.

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
- The dashboard page carries **aggregates only**, so the public URL exposes no
  student data. It is `noindex`, so search engines skip it.
- Vercel's free Hobby plan is for non-commercial use; a Be10x deployment should
  be on a paid plan.

## What to commit

`.gitignore` ignores `*.json`, so these need to go in together with the code —
push all of them or the CI build silently loses data:

```bash
git add .github pipeline.py SETUP_PIPELINE.md \
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
- ### Adding a week's data (the normal path since 2026-09-10)

1. Open the dashboard, go to **➕ Add data**, enter the data-entry password.
2. Drop in the week's Zoom exports — the attendee report for each session and its
   poll export, or a `.zip` of them. Names must be Zoom's own
   (`attendee_<webinar>_<YYYY>_<MM>_<DD>.csv`, `poll_...csv`).
3. Read the table. Every row must say **In L2: yes** — a session the L2 schedule
   does not list is HIDDEN by the dashboard even after a perfect run, so the page
   refuses to upload it and names the webinar id to add to L2.
4. Press **Upload and refresh the dashboard**. The files go to the Zoom extracts
   Shared Drive, GitHub runs the pipeline, and the page shows each step. Two to
   three minutes.
5. When it says done, every tab is showing the new data. Anyone else sees it on
   their next refresh.

If it fails, **nothing is published** — the pipeline refuses to overwrite a good
dashboard with a partial one, so the numbers on screen are still the previous
ones. The files are already on Drive, so re-running costs nothing.

Sessions already marked are never re-marked (§4g in CLAUDE.md). To re-mark
everything — after fixing a counting rule, say — run the workflow from GitHub's
Actions tab with **incremental** unticked.

`pipeline.py` also runs on a laptop: `python pipeline.py` (uses
  `.streamlit/secrets.toml` + the local key file; `--no-upload` builds without
  uploading). Don't run it while the local app is open — the app holds the store
  file; if a build is interrupted, delete `.cache/attendance.duckdb` and rebuild.
- If the roster gains a new batch tab, nothing to do — the next pipeline run
  picks it up automatically.
- **A session missing from L2 is invisible to the dashboard** (`data.REQUIRE_L2`).
  If a session ran but does not appear, add its Webinar ID to the L2 schedule.
- The app re-checks Drive for a newer store every 30 minutes, so Monday's rebuild
  reaches viewers on its own; 🔄 Refresh forces it immediately.
