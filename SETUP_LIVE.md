# Make the dashboard LIVE (automatic from Google Drive)

This turns the app from "upload files each time" into **"open the link, see the
latest attendance."** You do this **once** (~20 minutes). After that, every time
you replace the attendee `.zip` in Drive, the app re-marks and updates on refresh.

> 💡 There's also a friendlier, colour-coded version of this guide:
> open **`GO_LIVE_GUIDE.html`** in your browser (double-click it).

**How it works:** a deployed app runs on Streamlit's servers, so it needs its
*own* Google login — a **service account** ("robot"). You create the robot, share
your three Drive items with it (Viewer is enough), and paste the robot's key into
the app's secrets. The app then reads the roster + attendee zip live.

You only need to touch **one Google account: your own.** Use a **copy of the
roster that you own** so you're allowed to share it with the robot.

---

## Part 1 — Create the robot (Google Cloud Console)

> ⚠️ **Use your personal Gmail** (e.g. your `@gmail.com`) for this, **not** a
> work/Workspace account. Workspace organisations created on/after **3 May 2024**
> block downloading robot keys by default (you'd hit *"Key creation is not allowed
> on this service account"*). A personal Gmail has no organisation, so it just works.

1. Go to **console.cloud.google.com** and sign in.
2. **Create a project:** top bar project picker → **NEW PROJECT** → name it
   `attendance` → **Create**. Wait ~1 min, then make sure it's selected in the top bar.
3. **Turn on the two APIs** (open each link with your project selected, click **Enable**):
   - Drive API → https://console.cloud.google.com/apis/library/drive.googleapis.com
   - Sheets API → https://console.cloud.google.com/apis/library/sheets.googleapis.com
4. **Create the robot:** Menu (☰) → **IAM & Admin → Service Accounts** →
   **+ CREATE SERVICE ACCOUNT**. Name it `attendance-robot` → **CREATE AND CONTINUE**
   → the "Grant access (role)" step is **optional, skip it** → **CONTINUE** → **DONE**.
5. **Download its key:** click the robot's email in the list → **KEYS** tab →
   **ADD KEY → Create new key → JSON → CREATE**. A `.json` file downloads.
   > 🔐 This is the **only copy** — it can't be re-downloaded. Keep it private; never
   > put it in GitHub. (If lost, just delete that key and make a new one.)
6. **Copy the robot's email** — it looks like
   `attendance-robot@your-project.iam.gserviceaccount.com` (it's also the
   `client_email` inside the JSON). You'll share files with this address next.

---

## Part 2 — Share your 3 files with the robot + get their IDs

Do this for the **roster Sheet**, the **L2 schedule Sheet**, and the **attendee `.zip`**.

1. **Share:** open each item in Drive → **Share** → paste the robot email →
   set role to **Viewer** → (untick "Notify people" — the robot has no inbox) → **Share/Send**.
   - A robot only sees what you explicitly share with it. Sharing a **folder** shares everything inside it.
2. **Get each file's ID** — it's the long code in the URL:
   - A Google Sheet URL looks like `https://docs.google.com/spreadsheets/d/`**`THIS_LONG_ID`**`/edit` → copy the bold part.
   - A Drive file (the zip) URL looks like `https://drive.google.com/file/d/`**`THIS_LONG_ID`**`/view` → copy the bold part.

Keep the three IDs handy: `roster_id`, `l2_id`, `attendee_zip_id`.

> 🔁 **Weekly tip:** when you have new Zoom exports, **don't delete and re-upload** the
> zip (that changes its ID and breaks the link). Instead, right-click the zip in Drive →
> **Manage versions → Upload new version**. Same ID, new contents — the app just picks it up.

---

## Part 3 — Put the key + IDs into the app's secrets

The app reads these from Streamlit secrets. The template is in
[`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example). Fill it in like this:

```toml
[gcp_service_account]
# Copy each value from your downloaded JSON key file.
type = "service_account"
project_id = "your-project-id"
private_key_id = "..."
# EASIEST + safest: triple-quote and paste the key with its real line breaks:
private_key = """-----BEGIN PRIVATE KEY-----
MIIEvQ...        (many lines)
...
-----END PRIVATE KEY-----
"""
client_email = "attendance-robot@your-project.iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/..."

[drive]
roster_id       = "PASTE_ROSTER_SHEET_ID"
l2_id           = "PASTE_L2_SHEET_ID"
attendee_zip_id = "PASTE_ATTENDEE_ZIP_ID"
```

> ⚠️ **The `private_key` is the #1 thing people get wrong.** Use the triple-quote
> (`"""`) form above and paste the key with its real line breaks. Don't turn the
> line breaks into `\\n`, and don't use single quotes. If you instead keep it on one
> line, you must keep the `\n` exactly as in the JSON inside **double** quotes.

- **Testing on your own computer:** save the filled file as `.streamlit/secrets.toml`
  (no `.example`). It's gitignored, so it won't be committed.
- **On the deployed app:** go to **share.streamlit.io** → your app → **⋮ → Settings →
  Secrets**, paste the same content, **Save**. (Secrets live in Streamlit, **never** in GitHub.)

---

## Part 4 — Deploy / redeploy and confirm

- **First-time deploy:** at share.streamlit.io → **Create app** → pick this repo, branch,
  and set **Main file path** to `attendance_app.py` → **Advanced settings** → set Python
  to **3.12** and paste your secrets → **Deploy**.
- **Already deployed:** after saving secrets, **reboot** the app (app menu → Reboot) so
  it picks them up.

**You're live when:** the top of the app shows **🟢 Live from Google Drive**, the
**🔄 Refresh from Google** button is enabled, and the dashboard fills in by itself with
no uploading. Each week: update the zip's contents in Drive, open the app, hit **Refresh**.

---

## If you get stuck

| What you see | Fix |
|---|---|
| *"Key creation is not allowed on this service account"* | You used a Workspace account with the May-2024 org policy. Create the project under a **personal Gmail** instead. |
| *"Could not deserialize key data" / "No key could be detected"* | The `private_key` is malformed. Re-paste using the **triple-quote** form with real line breaks. |
| App shows **📤 Manual upload**, not 🟢 Live | Secrets missing/typo'd, or `roster_id` empty. Recheck the `[drive]` table and the `[gcp_service_account]` table names. |
| *"File not found" / empty data* | The robot can't see the file — make sure you **shared each item** with the robot email as Viewer. |
| Roster export error mentioning a size limit | Google Sheets export to `.xlsx` is capped at 10 MB. Workaround: upload the roster to Drive as an **`.xlsx` file** (not a native Sheet) and use that file's ID — downloads have no size cap. |
| App is asleep ("get this app back up!") | Normal after 12 h idle. Anyone can click to wake it; opening the link wakes it. |

Sources for these steps (verified 2026): Google Cloud IAM docs, Google Drive API
docs, and docs.streamlit.io. See `GO_LIVE_GUIDE.html` for the cited, visual version.
