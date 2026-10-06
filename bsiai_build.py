"""BSIAI ("Build Side Income Using AI") — roster extract + attendance store.

A SECOND, STANDALONE build of the same dashboard the AI CAP app draws, for the
BSIAI programme. Nothing here touches `pipeline.py`, the production store or
the main app; it reuses their pure modules so the two dashboards cannot drift
in how they count:

    lms_client / lms_roster   the roster rows, in the Master-Batch-Roster layout
    attendance_core           L2 parsing, dates, batch keys, phone/email rules
    data.build_batch          one batch's dashboard object (same code as AI CAP)
    recap / trainers          weekly recap, residual index, trainer rollups
    sessionmeta / polls       duration, peak, retention; feedback polls
    dashboard_core            the roster grid and the compute table

Run:  .venv\\Scripts\\python.exe bsiai_build.py            (fetch everything, build)
      .venv\\Scripts\\python.exe bsiai_build.py --skip-fetch  (reuse the last downloads)
      .venv\\Scripts\\python.exe bsiai_build.py --upload       (...and publish to the Drive store folder)

Outputs (deliverables go to F:, the store stays in .cache/ -- both gitignored):
    F:\\BSIAI_<n>_batches_roster_format_<date>.xlsx    the roster extract
    F:\\BSIAI_marked_attendance_<date>.xlsx          roster + Present/Absent per session
    F:\\attendance_store\\bsiai_<date>.duckdb         the store the BSIAI pages read
    .cache\\bsiai.duckdb                             the same store, where both apps look first
With `--upload`, `bsiai.duckdb` and the two workbooks also go to the private
Drive folder the AI CAP store lives in (`STORE_FOLDER_ID` / `store_folder_id`),
which is how the deployed app's BSIAI tab gets them.

THE RULES THAT MAKE THE NUMBERS WHAT THEY ARE (owner rulings, 2026-09-28,
all via AskUserQuestion; see the memory note `bsiai-local-dashboard`):

  1. Sessions come from the L2 schedule. L2 registers 77 of the 78 (webinar,
     date) pairs found on the drives; the one it does not is ruling 4's drop.
     A report L2 does not know is reported, never marked (the same L2 gate the
     main pipeline applies since 2026-09-28).
  2. Accelerator rooms are assigned by their AI CAP PEER: a room labelled
     `AI CAP B41 - Common , Build Side Income Using AI Accelerator B41` is
     `Accelerator B41`. The CAP number wins over the BSIAI number in the label
     because L2 and the Drive folders have called the same cohort B1, B2, B41
     and B42 at different times. Accelerator B43 ran its OWN room (no CAP
     sharing) and is read from its own label.
  3. Accelerator B41's weekends count EITHER room: its students also sat in
     the AI CAP B41 Techies room (78 of 303 on 19 Sep). The Techies report is
     unioned into the same day's session, so nobody is counted twice.
  4. Webinar 95403362407 (8 Aug, folder `bsi b2 - The AI-Powered Brand Studio`,
     183 attendees, 3 on the B2 roster) is a mislabelled folder — dropped.
  5. Attended == Yes only. Zoom lists registrants who never joined with
     Attended = No; they are not present.

And two conventions inherited from the main app rather than decided here:
  * The denominator is batch STRENGTH (everyone enrolled with an email), as
    `data.build_batch` computes it — not the active count the 28 Sep local
    build used. Percentages are therefore a little lower than that build's.
  * Accelerator batches are counted PER DAY (owner's ruling, 2026-10-05): each
    Saturday and Sunday is its own session, as the 28 Sep local build counted
    them. The AI CAP app's Sat+Sun weekend view is available with
    `--weekend-view` but is not the default.

Batch codes are SHORT in the dashboard (`B1`, `B3-A`, `Accelerator B41`) and
the workbook tabs carry the programme (`BSIAI B1`, `BSIAI Accelerator B41`).
Never key anything on the bare number: cohort B1 and Accelerator B1 (= LMS
B40) are different people, and `attendance_core.extract_batches` collapses
`B3 - A` and `B3 - B` to the same key.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import pathlib
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import attendance_core as ac          # noqa: E402
import dashboard_core as dc           # noqa: E402
import data as ddata                  # noqa: E402
import lms_client                     # noqa: E402
import lms_roster as lr               # noqa: E402
import polls as _polls                # noqa: E402
import recap as _recap                # noqa: E402
import sessionmeta as _smeta          # noqa: E402
import trainers as _trainers          # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))

# ── the eight batches ────────────────────────────────────────────────────────
# (dashboard code, workbook tab, LMS batch id). Resolved by ID, not by name:
# the LMS renamed B3-A/B3-B between 28 Sep and 5 Oct ('...AI-B3-A' ->
# '...AI B3-A'), and three product lines share batch numbers.
BATCHES = [
    ("B1",              "BSIAI B1",              "8aaca16b-eac7-46f7-9558-6d51ad71d24d"),
    ("B2",              "BSIAI B2",              "50080d12-7222-47cb-afea-df63f21ee1cb"),
    ("B3-A",            "BSIAI B3 - A",          "472e80ce-608c-40f2-8936-0179b2760161"),
    ("B3-B",            "BSIAI B3 - B",          "88ab8596-3970-47b2-90de-30f21fdee388"),
    # LMS "Accelerator B40" = the team's older "Accelerator B1". Added 2026-10-06
    # on the owner's ask ("BSIAI B40 is missing"); it shares AI CAP B40's rooms.
    ("Accelerator B40", "BSIAI Accelerator B40", "8fb68dfd-3ac1-4f83-9765-b11a0e0e66a4"),
    ("Accelerator B41", "BSIAI Accelerator B41", "2e3a985d-7fdc-44a0-9a36-ea52e2f91fe2"),
    ("Accelerator B42", "BSIAI Accelerator B42", "e438d449-64fd-4d83-b116-921c90c3e0c2"),
    ("Accelerator B43", "BSIAI Accelerator B43", "6e62d435-a32a-4d15-a8e2-8d4422951b98"),
]
CODES = [c for c, _t, _i in BATCHES]
TAB_OF = {c: t for c, t, _i in BATCHES}
CODE_OF_TAB = {t: c for c, t, _i in BATCHES}
LMS_ID = {c: i for c, _t, i in BATCHES}

# Ruling 2: a shared AI CAP room -> the Accelerator cohort of that CAP batch.
# B40's rooms are labelled 'AI CAP B40 - Common , Build Side Income Using AI
# Accelerator B40' (12-20 Sep) and 'AI CAP B39 , B40 - Generalist, Build Side
# Income Using AI Accelerator B40' (4 Oct); both resolve to Accelerator B40.
ACCEL_BY_CAP = {40: "Accelerator B40", 41: "Accelerator B41",
                42: "Accelerator B42", 43: "Accelerator B43"}
# Ruling 4.
DROP_WIDS = frozenset({"95403362407"})
# Ruling 3: {code: CAP batch whose Techies room is unioned into the same day}.
CROSS_ROOM = {"Accelerator B41": 41}
# The cohorts that run Sat+Sun pairs. The AI CAP app's weekend view
# (data.paired: one session per weekend, each person counted once over both
# days) is ON for them — the owner's ruling of 2026-10-06 ("the unique thing we
# have for AI CAP B41-B43 should also apply"), reversing the per-day ruling of
# 2026-10-05. `--per-day` turns it off for a comparison build.
PAIRED = frozenset({"Accelerator B40", "Accelerator B41", "Accelerator B42", "Accelerator B43"})
WEEKEND_VIEW = {"on": True}

RULINGS = [
    "Sessions: the L2 schedule is the register. A report L2 does not know is "
    "listed under warnings, never marked (owner, 2026-09-28; L2 gate).",
    "Accelerator rooms are assigned by their AI CAP peer batch (AI CAP B41 - "
    "Common -> Accelerator B41). B43 ran its own room and is read from its own "
    "L2 label (owner, 2026-09-28).",
    "Accelerator B41 weekends count either room: the AI CAP B41 Techies report "
    "is unioned into the same day's session (owner, 2026-09-28).",
    "Webinar 95403362407 (8 Aug, 'bsi b2 - The AI-Powered Brand Studio') is a "
    "mislabelled folder and is dropped (owner, 2026-09-28).",
    "Attended = Yes only (owner, 2026-09-28).",
    "Denominator = batch strength (everyone enrolled with an email), exactly as "
    "the AI CAP dashboard counts; the 28 Sep local build divided by active only.",
    "Accelerator batches are counted per WEEKEND, like AI CAP B41 onward: Saturday "
    "and Sunday run the same class, each learner is counted once across both days "
    "(owner, 2026-10-06, reversing the per-day ruling of 2026-10-05).",
    "A feedback poll in a shared AI CAP room is divided by roster: the batch's "
    "own figure is its own students' answers; 'Whole room' is everyone who answered.",
]

# ── paths ────────────────────────────────────────────────────────────────────
TEMP = pathlib.Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".")
ATT_DIR = TEMP / "bsiai_att"          # attendee reports from every BSIAI folder
POLL_DIR = TEMP / "bsiai_polls"       # their poll exports
TECH_DIR = TEMP / "bsiai_tech"        # AI CAP Techies rooms (ruling 3)
CACHE = HERE / ".cache"
STORE_NAME = "bsiai.duckdb"
STORE_LOCAL = CACHE / STORE_NAME
MARKED_NAME = "BSIAI_marked_attendance.xlsx"
ROSTER_NAME = "BSIAI_roster_format.xlsx"
LMS_CACHE = HERE / ".lms_cache" / "bsiai"
F_ROOT = pathlib.Path(os.environ.get("BSIAI_OUT_DIR") or r"F:\\")
F_STORE_DIR = F_ROOT / "attendance_store"

_ATT_RE = re.compile(r"(?i)attendee_(\d+)_((?:20\d\d)_\d{2}_\d{2})")

warnings: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


# ═════════════════════════════ 1. roster ═════════════════════════════════════
def fetch_lms(refresh: bool = True) -> dict:
    """{code: {lms_name, batch_id, claimed, customers, fetched_at}} from the API.

    Cached per batch id under .lms_cache/bsiai/ so a rebuild that only changes
    the marking does not re-download 4,500 rows; `refresh` forces a fresh pull.
    """
    key = lms_client.api_key()
    LMS_CACHE.mkdir(parents=True, exist_ok=True)
    listing = lms_client.fetch_batches(key)
    by_id = {b.get("id"): b for b in listing}
    side = [b for b in listing if "side income" in str(b.get("name") or "").lower()]
    log(f"   LMS: {len(listing)} batches, {len(side)} name the product")
    out = {}
    for code in CODES:
        bid = LMS_ID[code]
        rec = by_id.get(bid)
        if rec is None:
            raise RuntimeError(f"{code}: batch id {bid} is no longer in the LMS listing")
        cache_f = LMS_CACHE / f"customers_{bid}.json"
        if not refresh and cache_f.exists():
            cs = json.loads(cache_f.read_text(encoding="utf-8"))
        else:
            cs = lms_client.fetch_customers(key, bid, log=log)
            cache_f.write_text(json.dumps(cs), encoding="utf-8")
        claimed = (rec.get("_count") or {}).get("customers")
        out[code] = {"lms_name": rec.get("name"), "batch_id": bid, "claimed": claimed,
                     "customers": cs, "fetched_at": datetime.now(IST).isoformat()}
        log(f"   {code:<16} {str(rec.get('name'))[:46]:<46} {len(cs):>5} rows (listing says {claimed})")
    return out


def roster_rows(raw: dict) -> dict:
    """{code: [10-column roster rows]} — deduped the way lms_roster dedupes.

    `customer_row` writes 'AI CAP <code>' into the batch cell; it is replaced
    with the BSIAI tab name so a reader of the workbook sees the programme.
    """
    out = {}
    for code in CODES:
        rows = []
        for c in raw[code]["customers"]:
            r = lr.customer_row(c, code, "")
            r[lr.I_BATCH] = TAB_OF[code]
            rows.append(r[:10])
        out[code] = lr._dedupe(rows)
    return out


def _pdate(s):
    try:
        return datetime.strptime(str(s).strip(), "%d/%m/%Y, %I:%M %p")
    except Exception:
        return None


def write_roster_workbook(rows: dict, raw: dict, path: pathlib.Path, stamp: str) -> None:
    """The house 'roster format': Read me, Summary, one tab per batch, a combined
    tab, API reference, Overlap. Ten columns (no POD Prefrence — BSIAI has no
    PODs), phones stored as text, totals as literal values."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    HDR10 = lr.HEADERS[:10]
    fill, font = PatternFill("solid", fgColor="1F3864"), Font(bold=True, color="FFFFFF")

    def style(ws, n, row=1):
        for c in range(1, n + 1):
            cell = ws.cell(row=row, column=c)
            cell.fill, cell.font = fill, font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.freeze_panes = f"A{row + 1}"         # the STRING form; the cell form materialises row 2

    def widths(ws, ws_w):
        for i, w in enumerate(ws_w, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def text_phones(ws):
        for c in (lr.I_CC + 1, lr.I_NUM + 1, lr.I_WACC + 1, lr.I_WA + 1):
            for col in ws.iter_cols(min_col=c, max_col=c, min_row=2):
                for one in col:
                    one.number_format = "@"

    total = sum(len(rows[c]) for c in CODES)
    rawtot = sum(len(raw[c]["customers"]) for c in CODES)
    ident = {c: _identities(rows[c]) for c in CODES}
    distinct = len(set().union(*ident.values()))
    staff = sum(1 for c in CODES for r in rows[c] if "houseofedtech" in str(r[lr.I_MAIL]).lower())

    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Read me")
    lines = [
        f"The {len(BATCHES)} BSIAI batches in the Master-Batch-Roster column layout. Extracted {stamp} from the LMS API.",
        f"{total:,} roster rows across {len(BATCHES)} tabs ({rawtot:,} raw API rows before dedupe); {distinct:,} distinct people "
        f"— {total - distinct} re-enrolled into a second batch, so do not sum the tabs for a population.",
        "Rows built with attendance-suite/lms_roster.py customer_row(), so the phone concatenation and the",
        "Payment / Closing Type spellings are identical to what the pipeline writes. Deduped per batch on the",
        "lms_roster identity rule (email, else last-10 phone). Resolved by LMS batch ID, not name — the LMS",
        "renamed B3-A/B3-B between 28 Sep and 5 Oct.",
        "",
        "*** READ THIS BEFORE ANY BATCH-LEVEL ANALYSIS ***",
        "attendance_core.extract_batches cannot tell these batches apart: 'BSIAI B2' and 'BSIAI Accelerator B2'",
        "both key to ('BSIAI', 2), and 'B3 - A' / 'B3 - B' both key to ('BSIAI', 3). Group on the full 'batch",
        "name' column, never on a number.",
        "",
        "*** WHICH 'ACCELERATOR B40 / B41 / B42 / B43' THIS IS ***",
        "LMS-native numbering: 'Build Side Income Using AI Accelerator B40/B41/B42/B43'. The team's older",
        "'Accelerator B1 / B2' vocabulary means LMS B40 / B41. B40 was added on 6 Oct 2026.",
        "L2 and the Drive folders now carry the LMS numbering too ('AI CAP B41 - Common , Build Side",
        "Income Using AI Accelerator B41'), so the three naming systems finally agree.",
        "",
        "WHAT THIS DATA CANNOT SUPPORT: 'amount' is blank — the API has no per-batch deal value. The",
        "'API reference' tab carries ledger_total, which is LIFETIME spend across every product, not this",
        "batch's revenue. There is no name column — names exist only in the Zoom attendee reports.",
        "POD Prefrence is omitted (10 columns): BSIAI has no POD records in the LMS.",
        f"{staff} rows are internal @houseofedtech.in logins — filter them out of any percentage.",
        "",
        "Non-91 country codes have 8-9 digit national numbers; a last-10 phone match misreads them.",
        "The attendance marker (bsiai_build.py) falls back to the last 9 digits for those rows.",
        "",
        "PASTING INTO GOOGLE SHEETS: some 'broadcast mail' cells hold the LMS's +<phone>@10xpay.in",
        "placeholders; a USER_ENTERED write turns them into #ERROR!. Paste as RAW / plain text.",
        "READING IN PANDAS: pass dtype=str, or the one row with no country code loses its leading zero.",
        "PII: student emails and phone numbers. Handle as you would the roster sheet.",
    ]
    for ln in lines:
        ws.append([ln])
    for r in ws.iter_rows(min_col=1, max_col=1):
        if str(r[0].value or "").startswith(("***", "The 7 BSIAI")):
            r[0].font = Font(bold=True)
    widths(ws, [112])

    ws = wb.create_sheet("Summary")
    head = ["Batch (tab)", "Dashboard code", "LMS batch name (exact)", "LMS batch id",
            "Listing claimed", "Raw API rows", "Roster rows (deduped)",
            "Full Paid", "Booking Amount", "Partially Paid", "Unidentified/Refunded", "Payment blank",
            "BDA Closing", "BDA Collection", "System", "L3 Purchased", "Closing blank",
            "Has alt email", "Has alt phone", "Non-91 country code", "Staff logins"]
    ws.append(head)
    style(ws, len(head))
    for code in CODES:
        rs, rp = rows[code], raw[code]
        pay, clo = Counter(r[lr.I_PAY] for r in rs), Counter(r[lr.I_CLOSE] for r in rs)
        ws.append([TAB_OF[code], code, rp["lms_name"], rp["batch_id"], rp["claimed"],
                   len(rp["customers"]), len(rs),
                   pay.get("Full Paid", 0), pay.get("Booking Amount", 0), pay.get("Partially Paid", 0),
                   pay.get("Unidentified/Refunded", 0), pay.get("", 0),
                   clo.get("BDA Closing", 0), clo.get("BDA Collection", 0), clo.get("System", 0),
                   clo.get("L3 Purchased", 0), clo.get("", 0),
                   sum(1 for r in rs if r[lr.I_BCAST]), sum(1 for r in rs if r[lr.I_WA]),
                   sum(1 for r in rs if r[lr.I_CC] and r[lr.I_CC] not in ("91", "+91")),
                   sum(1 for r in rs if "houseofedtech" in str(r[lr.I_MAIL]).lower())])
    last = ws.max_row
    ws.append(["TOTAL", "", "", ""] + [sum(ws.cell(row=r, column=c).value or 0 for r in range(2, last + 1))
                                       for c in range(5, len(head) + 1)])
    for c in range(1, len(head) + 1):
        ws.cell(row=last + 1, column=c).font = Font(bold=True)
    widths(ws, [22, 16, 44, 38] + [14] * (len(head) - 4))

    col_w = [11, 18, 36, 11, 20, 36, 22, 10, 20, 16]
    for code in CODES:
        ws = wb.create_sheet(TAB_OF[code])
        ws.append(HDR10)
        style(ws, 10)
        for r in rows[code]:
            ws.append(r[:10])
        text_phones(ws)
        widths(ws, col_w)
        ws.auto_filter.ref = f"A1:J{ws.max_row}"
    ws = wb.create_sheet("All 7 (combined)")
    ws.append(HDR10)
    style(ws, 10)
    for code in CODES:
        for r in rows[code]:
            ws.append(r[:10])
    text_phones(ws)
    widths(ws, col_w)
    ws.auto_filter.ref = f"A1:J{ws.max_row}"

    ws = wb.create_sheet("API reference")
    head = ["Registered mail", "batch name", "POD (canonical)", "api paymentStatus", "api closingType",
            "emailVerified", "ledger_txns", "ledger_total", "ledger_first", "ledger_last", "unique_learner_id"]
    ws.append(head)
    style(ws, len(head))
    for code in CODES:
        seen = set()
        for c in raw[code]["customers"]:
            k = ac._cell_email(c.get("email"))
            if not k:
                p = ac._cell_phone(c.get("number"))
                k = f"p:{p[-10:]}" if len(p) >= 10 else ""
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            led = c.get("transactionLedger") or []
            ds = sorted(d for d in (_pdate(e.get("date")) for e in led) if d)
            ws.append([c.get("email"), TAB_OF[code], "", c.get("paymentStatus"), c.get("closingType"),
                       c.get("emailVerified"), len(led),
                       sum(float(e.get("amount") or 0) for e in led) or None,
                       ds[0].strftime("%Y-%m-%d") if ds else None,
                       ds[-1].strftime("%Y-%m-%d") if ds else None, c.get("unique_learner_id")])
    widths(ws, [36, 22, 16, 18, 18, 14, 12, 14, 13, 13, 40])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"

    ws = wb.create_sheet("Overlap")
    ws.append(["People appearing in more than one of these batches (email, else last-10 phone)"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=12)
    ws.append([])
    ws.append([""] + [TAB_OF[c] for c in CODES])
    style(ws, len(CODES) + 1, row=3)
    for a in CODES:
        ws.append([TAB_OF[a]] + [len(ident[a] & ident[b]) if a != b else len(ident[a]) for b in CODES])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append([])
    ws.append(["Diagonal = the batch's own size. Off-diagonal = people in both."])
    widths(ws, [24] + [22] * len(CODES))

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    # Read back: every tab must hold exactly its rows (the freeze_panes trap).
    from openpyxl import load_workbook
    chk = load_workbook(path, read_only=True)
    for code in CODES:
        n = chk[TAB_OF[code]].max_row - 1
        if n != len(rows[code]):
            raise RuntimeError(f"{TAB_OF[code]}: wrote {len(rows[code])} rows, read back {n}")
    chk.close()


def _identities(rows) -> set:
    out = set()
    for r in rows:
        e = ac._cell_email(r[lr.I_MAIL])
        p = ac._cell_phone(r[lr.I_NUM])
        k = e or (f"p:{p[-10:]}" if len(p) >= 10 else "")
        if k:
            out.add(k)
    return out


# ═════════════════════════════ 2. Drive ══════════════════════════════════════
def _drive():
    import live_data
    import pipeline
    cfg = pipeline.load_config()
    live_data.set_service_account(cfg["sa_info"], scopes=pipeline.RW_SCOPES)
    return live_data._drive_service(), cfg


def _list_folders(svc, q: str) -> list:
    out, tok = [], None
    while True:
        res = svc.files().list(
            q=q + " and trashed=false and mimeType='application/vnd.google-apps.folder'",
            corpora="allDrives", includeItemsFromAllDrives=True, supportsAllDrives=True,
            pageSize=500, pageToken=tok,
            fields="nextPageToken,files(id,name,driveId,createdTime)").execute()
        out += res.get("files", [])
        tok = res.get("nextPageToken")
        if not tok:
            return out


def _download(svc, k: dict, local: pathlib.Path) -> bool:
    """One Drive file to disk. A report Drive converted to a Google Sheet is
    exported as CSV (`get_media` 403s on those)."""
    from googleapiclient.http import MediaIoBaseDownload
    if local.exists():
        return True
    try:
        if k["mimeType"] == "application/vnd.google-apps.spreadsheet":
            data = svc.files().export(fileId=k["id"], mimeType="text/csv").execute()
            local.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        else:
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=k["id"], supportsAllDrives=True))
            done = False
            while not done:
                _, done = dl.next_chunk()
            local.write_bytes(buf.getvalue())
        return True
    except Exception as e:                       # noqa: BLE001 — one bad file must not stop the pull
        warnings.append(f"download failed: {k.get('name')} ({type(e).__name__})")
        return False


def fetch_drive(l2_bytes: bytes) -> tuple[list, list, list]:
    """Pull attendee reports + poll exports from every folder naming BSIAI on
    either Shared Drive, plus the Techies rooms ruling 3 needs. Files already on
    disk are kept. Returns (attendee index, poll index, techies index)."""
    svc, _cfg = _drive()
    for d in (ATT_DIR, POLL_DIR, TECH_DIR):
        d.mkdir(parents=True, exist_ok=True)
    folders = {f["id"]: f for f in _list_folders(svc, "name contains 'BSI'")}
    for f in _list_folders(svc, "name contains 'Side Income'"):
        folders.setdefault(f["id"], f)
    log(f"   {len(folders)} Drive folders name BSIAI / Side Income")
    att_idx, poll_idx = [], []
    for i, f in enumerate(sorted(folders.values(), key=lambda x: x["name"]), 1):
        kids = svc.files().list(q=f"'{f['id']}' in parents and trashed=false",
                                includeItemsFromAllDrives=True, supportsAllDrives=True, pageSize=200,
                                fields="files(id,name,size,mimeType)").execute().get("files", [])
        for k in kids:
            if _ATT_RE.match(k["name"]):
                local = ATT_DIR / (f["id"][:8] + "__" + k["name"])
                if _download(svc, k, local):
                    att_idx.append({"folder": f["name"], "folder_id": f["id"], "drive": f.get("driveId"),
                                    "file": k["name"], "local": str(local), "size": k.get("size")})
            elif re.search(r"(?i)poll", k["name"]):
                local = POLL_DIR / (f["id"][:8] + "__" + re.sub(r"[^\w.\-]", "_", k["name"]))
                if _download(svc, k, local):
                    poll_idx.append({"folder": f["name"], "folder_id": f["id"], "file": k["name"],
                                     "local": str(local)})
        if i % 25 == 0 or i == len(folders):
            log(f"   [{i}/{len(folders)}] folders scanned")
    # Techies rooms of the CAP peers named in CROSS_ROOM, found by webinar id.
    l2, labels, _m = ac.parse_l2(l2_bytes, with_labels=True, with_mentors=True)
    tech_idx = []
    for code, cap in CROSS_ROOM.items():
        pat = re.compile(rf"(?i)AI\s*CAP\s*B{cap}\s*-\s*Tech")
        for wid in (w for w, lab in labels.items() if pat.search(lab)):
            res = svc.files().list(q=f"name contains 'attendee_{wid}_' and trashed=false",
                                   corpora="allDrives", includeItemsFromAllDrives=True,
                                   supportsAllDrives=True,
                                   fields="files(id,name,size,mimeType)").execute().get("files", [])
            for k in res:
                local = TECH_DIR / (k["id"][:8] + "__" + k["name"])
                if _download(svc, k, local):
                    tech_idx.append({"code": code, "wid": wid, "label": labels[wid], "file": k["name"],
                                     "local": str(local), "size": k.get("size")})
    (ATT_DIR / "index.json").write_text(json.dumps(att_idx, indent=1), encoding="utf-8")
    (POLL_DIR / "index.json").write_text(json.dumps(poll_idx, indent=1), encoding="utf-8")
    (TECH_DIR / "index.json").write_text(json.dumps(tech_idx, indent=1), encoding="utf-8")
    log(f"   {len(att_idx)} attendee reports, {len(poll_idx)} poll exports, {len(tech_idx)} Techies reports")
    return att_idx, poll_idx, tech_idx


def load_indexes() -> tuple[list, list, list]:
    def rd(d):
        p = d / "index.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    return rd(ATT_DIR), rd(POLL_DIR), rd(TECH_DIR)


def fetch_l2() -> tuple[bytes, str]:
    import live_data
    svc, cfg = _drive()
    return live_data.fetch_sheet_cached(svc, cfg["l2_id"])


# ═════════════════════════════ 3. sessions ═══════════════════════════════════
_B3A = re.compile(r"(?i)b\s*3\s*-?\s*a\b")
_B3B = re.compile(r"(?i)b\s*3\s*-?\s*b\b")


def assign(label_text: str) -> list[str]:
    """An L2 label (or Drive folder name) -> the dashboard code(s) it belongs to.

    Ruling 2 first: a label naming an AI CAP batch is a shared Common room and
    belongs to that CAP batch's Accelerator cohort — the BSIAI number in the
    same label is ignored. Otherwise the BSIAI number names the cohort, with
    43 (own room) as the Accelerator and a plain 'B3' (12-13 Sep, before the
    A/B split) marked against BOTH halves.
    """
    keys = ac.extract_batches(label_text or "")
    cap = sorted(n for t, n in keys if t == "CAP")
    bsi = sorted(n for t, n in keys if t == "BSIAI")
    if not bsi:
        return []
    if cap:
        return [ACCEL_BY_CAP[n] for n in cap if n in ACCEL_BY_CAP]
    out: list[str] = []
    for n in bsi:
        if n == 1:
            out.append("B1")
        elif n == 2:
            out.append("B2")
        elif n == 3:
            if _B3A.search(label_text):
                out.append("B3-A")
            elif _B3B.search(label_text):
                out.append("B3-B")
            else:
                out += ["B3-A", "B3-B"]
        elif n in ACCEL_BY_CAP:
            out.append(ACCEL_BY_CAP[n])
    return out


def parse_attended_yes(text: str) -> tuple[set, set, set, int]:
    """Ruling 5: like attendance_core.parse_attendees, but ONLY Attended == Yes.
    Returns (emails, full phones, last-10 phones, rows with Attended = No)."""
    rows = list(csv.reader(io.StringIO(text)))
    hidx = next((i for i, r in enumerate(rows)
                 if r and r[0].strip() == "Attended" and "First Name" in r), None)
    emails, ph_full, ph10 = set(), set(), set()
    if hidx is None:
        return emails, ph_full, ph10, 0
    h = rows[hidx]
    ei = h.index("Email") if "Email" in h else None
    pi = h.index("Phone") if "Phone" in h else None
    n_no = 0
    for r in rows[hidx + 1:]:
        if not r:
            continue
        if r[0].strip() == "Attended":
            break
        if r[0].strip().lower() != "yes":
            n_no += 1
            continue
        e = re.sub(r"\s", "", (r[ei] or "")).lower() if ei is not None and len(r) > ei else ""
        p = ac._digits(re.sub(r"\.0$", "", (r[pi] or "").strip())) if pi is not None and len(r) > pi else ""
        if e:
            emails.add(e)
        if p:
            ph_full.add(p)
            if len(p) >= 10:
                ph10.add(p[-10:])
    return emails, ph_full, ph10, n_no


def pick_l2_date(stamp: str, l2_dates) -> str:
    """The L2 date for a report stamped `stamp` (YYYY_MM_DD).

    The Zoom extractor stamps some exports a day early, and L2 is the schedule
    of record, so L2's date wins. A webinar id reused on two dates (B3-B's
    Gemini walkthrough, 24 Sep and 1 Oct) takes the L2 date nearest the stamp.
    """
    if not isinstance(l2_dates, set) or not l2_dates:
        return stamp
    def _d(s):
        return datetime.strptime(s, "%Y_%m_%d")
    return min(l2_dates, key=lambda d: abs((_d(d) - _d(stamp)).days))


def _read(local: str) -> str:
    return pathlib.Path(local).read_bytes().decode("utf-8-sig", errors="replace")


def collect_sessions(att_idx: list, tech_idx: list, l2_bytes: bytes) -> dict:
    """{code: {ymd: session}} with the people who attended each.

    session = {wids, topic, label, mentor, emails, ph_full, ph10, texts,
               n_registered_no, copies, extra_rooms}
    Copies of one (webinar, date) are collapsed by keeping the one naming the
    most people; two webinars of one batch on one day are unioned into that
    day (the marker's own rule: one column per date).
    """
    l2, labels, mentors = ac.parse_l2(l2_bytes, with_labels=True, with_mentors=True)
    l2_dates = ac.l2_dates(l2_bytes, with_ffa=False)

    # best copy per (wid, stamp)
    best: dict = {}
    for it in att_idx:
        m = _ATT_RE.match(it["file"])
        if not m:
            continue
        wid, stamp = m.group(1), m.group(2)
        if wid in DROP_WIDS:
            continue
        text = _read(it["local"])
        em, pf, p10, n_no = parse_attended_yes(text)
        n = len(em | p10)
        if n == 0:
            warnings.append(f"{it['file']} ({it['folder'][:50]}): {ac.ZERO_ATTENDEE_TAG} — "
                            f"{ac.zero_attendee_reason(text)}")
            continue
        prev = best.get((wid, stamp))
        if prev and prev["n"] >= n:
            prev["copies"] += 1
            continue
        best[(wid, stamp)] = {"wid": wid, "stamp": stamp, "folder": it["folder"], "text": text,
                              "emails": em, "ph_full": pf, "ph10": p10, "n": n, "n_no": n_no,
                              "copies": (prev["copies"] + 1) if prev else 1}

    out: dict = {c: {} for c in CODES}
    for (wid, stamp), rep in sorted(best.items()):
        if wid not in l2:
            warnings.append(f"not in L2, not marked: webinar {wid} stamped {stamp} "
                            f"({rep['folder'][:60]})")
            continue
        keys, topic = l2[wid]
        label = labels.get(wid, "")
        codes = assign(label) or assign(rep["folder"])
        if not codes:
            # An AI CAP class whose TOPIC mentions side income is not BSIAI at
            # all; a BSIAI room for a cohort outside these eight (Accelerator
            # B44, B45…) is, and deserves a line.
            if any(t == "BSIAI" for t, _n in keys) or re.search(r"(?i)bsi", label):
                warnings.append(f"webinar {wid} ({label[:55]}, {stamp}): a BSIAI cohort this dashboard "
                                f"does not track — skipped")
            continue
        ymd = pick_l2_date(stamp, l2_dates.get(wid))
        if ymd != stamp:
            warnings.append(f"webinar {wid}: export stamped {stamp}, L2 says {ymd} — L2's date used")
        for code in codes:
            s = out[code].setdefault(ymd, {
                "wids": [], "topics": [], "labels": [], "mentors": [], "emails": set(),
                "ph_full": set(), "ph10": set(), "texts": {}, "n_registered_no": 0,
                "copies": 0, "extra_rooms": []})
            s["wids"].append(wid)
            s["topics"].append(topic or f"Session {wid}")
            s["labels"].append(ddata.clean_l2_label(label) if label else rep["folder"])
            s["mentors"].append(str(mentors.get(wid) or "").strip())
            s["emails"] |= rep["emails"]
            s["ph_full"] |= rep["ph_full"]
            s["ph10"] |= rep["ph10"]
            s["texts"][wid] = rep["text"]
            s["n_registered_no"] += rep["n_no"]
            s["copies"] += rep["copies"]

    # Ruling 3: union the CAP peer's Techies room into the same day's session.
    tech_best: dict = {}
    for it in tech_idx:
        m = _ATT_RE.match(it["file"])
        if not m:
            continue
        wid, stamp = m.group(1), m.group(2)
        text = _read(it["local"])
        em, pf, p10, _n = parse_attended_yes(text)
        n = len(em | p10)
        prev = tech_best.get((it["code"], wid, stamp))
        if n and (not prev or prev["n"] < n):
            tech_best[(it["code"], wid, stamp)] = {"emails": em, "ph_full": pf, "ph10": p10, "n": n,
                                                   "text": text, "label": it["label"]}
    for (code, wid, stamp), rep in tech_best.items():
        ymd = pick_l2_date(stamp, l2_dates.get(wid))
        s = out.get(code, {}).get(ymd)
        if not s:
            warnings.append(f"{code}: Techies room {wid} on {ymd} has no Common-room session to join — ignored")
            continue
        if code not in CROSS_ROOM:
            # Not a ruling for this cohort: MEASURED against the roster in
            # `mark` and reported, never merged.
            s.setdefault("candidate_rooms", []).append(
                {"wid": wid, "label": rep["label"], "emails": rep["emails"],
                 "ph_full": rep["ph_full"], "ph10": rep["ph10"]})
            continue
        before = len(s["emails"] | s["ph10"])
        s["emails"] |= rep["emails"]
        s["ph_full"] |= rep["ph_full"]
        s["ph10"] |= rep["ph10"]
        s["extra_rooms"].append({"wid": wid, "label": rep["label"], "added_people": len(s["emails"] | s["ph10"]) - before})

    # L2 BSIAI webinars that have no report anywhere — say so.
    have = {w for c in out for s in out[c].values() for w in s["wids"]}
    for wid, (keys, topic) in l2.items():
        if any(t == "BSIAI" for t, _n in keys) and wid not in have and wid not in DROP_WIDS:
            codes = assign(labels.get(wid, ""))
            if codes:
                ds = l2_dates.get(wid)
                ds = ", ".join(sorted(ds)) if isinstance(ds, set) else "?"
                warnings.append(f"L2 registers webinar {wid} ({', '.join(codes)}, {ds}, "
                                f"{topic[:40]}) but no attendee report exists on either drive")
    return out


# ═════════════════════════════ 4. marking ════════════════════════════════════
def _student(row: list) -> dict:
    alts = [ac._cell_email(x) for x in str(row[lr.I_BCAST] or "").split(",")]
    return {
        "email": ac._cell_email(row[lr.I_MAIL]),
        "alt_emails": [a for a in alts if a and not a.startswith("+")],   # 10xpay placeholders
        "phone": ac._cell_phone(row[lr.I_NUM]),
        "alt_phones": [ac._cell_phone(x) for x in str(row[lr.I_WA] or "").split(",") if ac._cell_phone(x)],
        "cc": str(row[lr.I_CC] or "").strip(),
    }


def hit(st: dict, sess: dict) -> bool:
    """Email OR phone, exactly as the main marker matches — plus a 9-digit
    fallback for non-91 numbers, whose national part is 8-9 digits and whose
    last ten would swallow part of the country code."""
    for e in [st["email"]] + st["alt_emails"]:
        if e and e in sess["emails"]:
            return True
    for p in [st["phone"]] + st["alt_phones"]:
        if not p:
            continue
        if p in sess["ph_full"] or (len(p) >= 10 and p[-10:] in sess["ph10"]):
            return True
        if st["cc"] and st["cc"] != "91" and len(p) >= 9:
            tail9 = {x[-9:] for x in sess["ph_full"] if len(x) >= 9}
            if p[-9:] in tail9:
                return True
    return False


def mark(rows: dict, sessions: dict) -> tuple[dict, list]:
    """{code: [row + marks]} and the per-session report.

    Every enrolled row gets Present/Absent for every session column — the
    same shape the main marker writes, so `data.build_batch` and
    `dashboard_core.roster_grid` read it unchanged.
    """
    marked, report = {}, []
    for code in CODES:
        dates = sorted(sessions.get(code, {}))
        out_rows = []
        students = [(_student(r), r) for r in rows[code]]
        counts = Counter()
        cross = defaultdict(Counter)        # ymd -> {other room wid: students there but not here}
        for st, r in students:
            marks = []
            for ymd in dates:
                sess = sessions[code][ymd]
                is_in = hit(st, sess)
                marks.append("Present" if is_in else "Absent")
                counts[ymd] += is_in
                if not is_in:
                    for room in sess.get("candidate_rooms") or ():
                        if hit(st, room):
                            cross[ymd][room["wid"]] += 1
            out_rows.append(list(r[:10]) + marks)
        marked[code] = {"dates": dates, "rows": out_rows}
        for ymd in dates:
            s = sessions[code][ymd]
            unmerged = [{"wid": rm["wid"], "label": rm["label"], "students_only_there": cross[ymd][rm["wid"]]}
                        for rm in s.get("candidate_rooms") or ()]
            for u in unmerged:
                if u["students_only_there"]:
                    warnings.append(f"{code} {ymd}: {u['students_only_there']} student(s) sat only in "
                                    f"{u['label']} ({u['wid']}) — measured, NOT merged (no ruling for this cohort)")
            report.append({"batch": code, "date": ymd, "topic": " · ".join(dict.fromkeys(s["topics"])),
                           "label": " · ".join(dict.fromkeys(s["labels"])), "webinars": s["wids"],
                           "present": counts[ymd], "total": len(out_rows),
                           "in_room": len(s["emails"] | s["ph10"]),
                           "registered_no": s["n_registered_no"], "copies": s["copies"],
                           "extra_rooms": s["extra_rooms"], "unmerged_rooms": unmerged})
    return marked, report


def marked_workbook(marked: dict) -> tuple[bytes, dict]:
    """The marked roster as xlsx bytes + {tab: rows} (header first)."""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    tabs = {}
    for code in CODES:
        tab = TAB_OF[code]
        hdr = lr.HEADERS[:10] + marked[code]["dates"]
        ws = wb.create_sheet(tab)
        ws.append(hdr)
        for r in marked[code]["rows"]:
            ws.append(r)
        for c in (lr.I_CC + 1, lr.I_NUM + 1, lr.I_WACC + 1, lr.I_WA + 1):
            for col in ws.iter_cols(min_col=c, max_col=c, min_row=2):
                for one in col:
                    one.number_format = "@"
        ws.freeze_panes = "D2"
        tabs[tab] = [hdr] + [list(r) for r in marked[code]["rows"]]
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), tabs


# ═════════════════════════════ 5. polls + meta ═══════════════════════════════
def poll_ratings(poll_idx: list, sessions: dict, rows: dict, l2_bytes: bytes) -> dict:
    """{(code, mm, ''): ratings} for every session that has a feedback poll.

    One export per webinar wins (most responses). In a shared AI CAP room the
    poll is divided by roster: the batch's own figure is its own students'
    answers, and `shared` carries the whole room — the same split the main
    pipeline applies to AI CAP batches ([5a.1]).
    """
    l2, labels, _m = ac.parse_l2(l2_bytes, with_labels=True, with_mentors=True)
    parsed = []
    for it in poll_idx:
        try:
            blob = pathlib.Path(it["local"]).read_bytes()
        except OSError:
            continue
        got = _polls.parse_one(blob)
        if got:
            parsed.append((it["file"], got, blob))
    by_wid: dict = {}
    for name, got, blob in parsed:
        k = _polls.name_key(name)
        if not k:
            continue
        prev = by_wid.get(k[0])
        if prev and prev[0]["responses"] >= got["responses"]:
            continue
        by_wid[k[0]] = (got, blob)
    roster_emails = {code: {ac._cell_email(r[lr.I_MAIL]) for r in rows[code]} - {""} for code in CODES}
    out: dict = {}
    for code in CODES:
        for ymd, s in sessions[code].items():
            mm = ymd[5:]
            for wid in s["wids"]:
                if wid not in by_wid:
                    continue
                got, blob = by_wid[wid]
                rt = dict(got)
                label = labels.get(wid, "")
                shared_with = ddata.shared_batches(label) or (
                    [_polls.batch_label(t, n) for t, n in sorted(ac.extract_batches(label)) if t == "CAP"])
                if shared_with:
                    room = {k: got.get(k) for k in _polls._JOINT_KEYS}
                    shared = {"batches": sorted(set(shared_with) | {code}), "joint": room, "room": room,
                              "split": False, "reason": "error"}
                    try:
                        resp = _polls.parse_responses(blob.decode("utf-8-sig", errors="replace"))
                        if any(r.get("email") for r in resp):
                            parts, matched = _polls._divide(resp, {code: roster_emails[code]})
                            rt.update(parts[code])
                            shared.update(split=True, unmatched=parts["_unmatched"], multi=parts["_multi"])
                            shared.pop("reason", None)
                        else:
                            shared["reason"] = "no-emails"
                    except Exception:            # noqa: BLE001 — keep the room figure, labelled
                        shared["reason"] = "error"
                    rt["shared"] = shared
                prev = out.get((code, mm, ""))
                if prev and (prev.get("responses") or 0) >= (rt.get("responses") or 0):
                    continue
                out[(code, mm, "")] = rt
    return out


def session_meta(sessions: dict) -> dict:
    """{(code, mm, ''): duration / peak / retention}, from the chosen report."""
    out = {}
    for code in CODES:
        for ymd, s in sessions[code].items():
            best = None
            for wid, text in s["texts"].items():
                h = _smeta.parse_header(text) or {}
                m = _smeta.measure(text)
                cand = {**h, **m}
                if best is None or (cand.get("timed_rows") or 0) > (best.get("timed_rows") or 0):
                    best = cand
            if best:
                out[(code, ymd[5:], "")] = best
    return out


# ═════════════════════════════ 6. DATA + store ═══════════════════════════════
def build_data(tabs: dict, sessions: dict, ratings: dict) -> tuple[dict, dict]:
    """DATA/summary through `data.build_batch` — the AI CAP dashboard's own code.

    The lookups are keyed on OUR codes (data.build_batch asks for
    (batch, mm, pod) and (batch, mm)); `data.paired` is widened to the
    Accelerator codes for the duration so the weekend view applies to them.
    """
    l2_lookup, l2_labels, mentors = {}, {}, {}
    for code in CODES:
        for ymd, s in sessions[code].items():
            mm = ymd[5:]
            l2_lookup[(code, mm, "")] = l2_lookup[(code, mm)] = " · ".join(dict.fromkeys(s["topics"]))
            l2_labels[(code, mm, "")] = l2_labels[(code, mm)] = " · ".join(dict.fromkeys(s["labels"]))
            mentors[(code, mm, "")] = mentors[(code, mm)] = ", ".join(
                dict.fromkeys(m for m in s["mentors"] if m))
    orig = ddata.paired
    ddata.paired = (lambda b: orig(b) or b in PAIRED) if WEEKEND_VIEW["on"] else orig
    try:
        DATA = {}
        for code in CODES:
            rows = tabs[TAB_OF[code]]
            d = ddata.build_batch(rows, code, l2_lookup, l2_labels, ratings, mentors)
            if d:
                DATA[code] = d
            else:
                warnings.append(f"{code}: no sessions with marks — not on the dashboard")
    finally:
        ddata.paired = orig
    summary = {"batches": len(DATA),
               "enrolled": sum(d["strength"] for d in DATA.values()),
               "active": sum(d["active"] for d in DATA.values()),
               "sessions": sum(d["n_sessions"] for d in DATA.values())}
    return DATA, summary


def enrich_sessions(DATA: dict, l2_bytes: bytes, smeta: dict, today) -> tuple[list, dict, dict]:
    """(sessions rows, recap, trainers) — the same three sections the main store
    carries, built by the same modules."""
    rows = _recap.collect_sessions(DATA, today)
    emails = ac.l2_mentor_emails(l2_bytes)
    mtypes, tconf = ac.l2_mentor_types(l2_bytes)
    trainer_section = _trainers.build(rows, emails, mtypes)
    trainer_section["type_conflicts"] = tconf
    sessions_section = _trainers.annotate(rows, emails, mtypes)
    for r in sessions_section:
        m = smeta.get((r["batch"], r.get("mm"), r.get("pod") or "")) or {}
        r["duration_hrs"] = m.get("span_hrs") or m.get("duration_hrs")
        r["peak"] = m.get("peak_computed") or m.get("peak")
        r["unique_viewers"] = m.get("unique_viewers")
        r["retention"] = m.get("curve")
        r["stick10"] = m.get("stick10")
        r["stick30"] = m.get("stick30")
        r["poll_at_min"] = None
        if r.get("poll_at") and m.get("t0") and r.get("retention"):
            try:
                mins = int((datetime.fromisoformat(r["poll_at"]) - datetime.fromisoformat(m["t0"])).total_seconds() // 60)
                if 0 <= mins < len(r["retention"]):
                    r["poll_at_min"] = mins
            except Exception:                    # noqa: BLE001
                pass
    recap_section = _recap.build(DATA, today, rows=sessions_section)
    return sessions_section, recap_section, trainer_section


def write_store(path: pathlib.Path, marked_bytes: bytes, tabs: dict, DATA: dict, summary: dict,
                meta_extra: dict) -> None:
    import duckdb
    df = dc.compute(marked_bytes, tabs=tabs)
    if not df.empty:
        df["Batch"] = df["Batch"].map(lambda t: CODE_OF_TAB.get(t, t))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    con.register("df_compute", df)
    con.execute("CREATE TABLE compute AS SELECT * FROM df_compute")
    for code in CODES:
        if code not in DATA:
            continue
        grid = dc.roster_grid(marked_bytes, TAB_OF[code], tabs=tabs)
        con.register("df_grid", grid)
        con.execute(f'CREATE TABLE "grid_{code}" AS SELECT * FROM df_grid')
        con.unregister("df_grid")
    meta = {
        "generated_at": datetime.now(IST).strftime("%d %b %Y, %H:%M IST"),
        "generated_at_iso": datetime.now(IST).isoformat(),
        "programme": "BSIAI",
        "DATA": DATA, "summary": summary,
        "batches": [c for c in CODES if c in DATA],
        "sheet_map": {c: TAB_OF[c] for c in CODES if c in DATA},
        "rulings": [r for r in RULINGS if not (WEEKEND_VIEW["on"] and "per day" in r)]
                   + (["Comparison build: Accelerator batches counted per Sat+Sun weekend "
                       "(--weekend-view), NOT the owner's per-day default."] if WEEKEND_VIEW["on"] else []),
        "weekend_view": WEEKEND_VIEW["on"],
        "forecast": None,
        **meta_extra,
    }
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
    con.close()


# ═════════════════════════════ main ══════════════════════════════════════════
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--skip-fetch", action="store_true",
                    help="reuse the LMS cache and the last Drive downloads")
    ap.add_argument("--roster-only", action="store_true", help="write the roster workbook and stop")
    ap.add_argument("--no-f", action="store_true", help="do not copy deliverables to F:")
    ap.add_argument("--upload", action="store_true",
                    help="publish the store and both workbooks to the Drive store folder "
                         "(STORE_FOLDER_ID / secrets drive.store_folder_id)")
    ap.add_argument("--per-day", action="store_true",
                    help="count Accelerator batches per day instead of per Sat+Sun weekend "
                         "— a comparison build, not the owner's default (2026-10-06)")
    args = ap.parse_args(argv)
    WEEKEND_VIEW["on"] = not args.per_day
    t0 = time.time()
    stamp = datetime.now(IST).strftime("%Y-%m-%d")
    today = datetime.now(IST).date()

    log(f"[1/6] roster from the LMS API ({'cached' if args.skip_fetch else 'fresh'})")
    raw = fetch_lms(refresh=not args.skip_fetch)
    rows = roster_rows(raw)
    log("   deduped: " + ", ".join(f"{c} {len(rows[c])}" for c in CODES))
    CACHE.mkdir(exist_ok=True)
    roster_path = CACHE / f"BSIAI_{len(BATCHES)}_batches_roster_format_{stamp}.xlsx"
    write_roster_workbook(rows, raw, roster_path, stamp)
    log(f"   wrote {roster_path.name}")
    if args.roster_only:
        return _deliver([roster_path], None, args.no_f, stamp)

    log("[2/6] L2 schedule")
    l2_bytes, l2_stamp = fetch_l2()
    log(f"   L2 modified {l2_stamp}")

    log(f"[3/6] attendee reports + polls from both Shared Drives ({'reusing downloads' if args.skip_fetch else 'listing'})")
    if args.skip_fetch:
        att_idx, poll_idx, tech_idx = load_indexes()
        log(f"   {len(att_idx)} attendee reports, {len(poll_idx)} poll exports, {len(tech_idx)} Techies reports on disk")
    else:
        att_idx, poll_idx, tech_idx = fetch_drive(l2_bytes)

    log("[4/6] sessions + marking")
    sessions = collect_sessions(att_idx, tech_idx, l2_bytes)
    log("   sessions: " + ", ".join(f"{c} {len(sessions[c])}" for c in CODES))
    marked, report = mark(rows, sessions)
    marked_bytes, tabs = marked_workbook(marked)
    marked_path = CACHE / f"BSIAI_marked_attendance_{stamp}.xlsx"
    marked_path.write_bytes(marked_bytes)
    for r in report:
        extra = f" +{len(r['extra_rooms'])} room(s)" if r["extra_rooms"] else ""
        log(f"   {r['batch']:<16} {r['date']} present {r['present']:>4}/{r['total']:<5} "
            f"in room {r['in_room']:>4}{extra}  {r['topic'][:40]}")

    log("[5/6] polls, session metadata, dashboard data")
    ratings = poll_ratings(poll_idx, sessions, rows, l2_bytes)
    smeta = session_meta(sessions)
    DATA, summary = build_data(tabs, sessions, ratings)
    sessions_section, recap_section, trainer_section = enrich_sessions(DATA, l2_bytes, smeta, today)
    for code, d in DATA.items():
        log(f"   {code:<16} strength {d['strength']:>5} active {d['active']:>5} sessions {d['n_sessions']:>3} "
            f"avg {d['avg_pct']:5.1f}%  peak {d['peak']:5.1f}  low {d['low']:5.1f}"
            + (f"  weekends {len(d['weekends'])}" if d.get("weekends") else ""))
    log(f"   summary {summary}; polls matched {len(ratings)}; {len(warnings)} warning(s)")

    log("[6/6] store")
    ids = _upload_workbooks(roster_path, marked_path) if args.upload else {}
    meta_extra = {
        **ids,
        "report": report, "warnings": warnings,
        "source": (f"LMS API ({stamp}) · L2 schedule · {len(att_idx)} attendee reports and "
                   f"{len(poll_idx)} poll exports from both Shared Drives"),
        "stamps": {"roster": raw[CODES[0]]["fetched_at"], "l2": l2_stamp,
                   "lms": {c: {"name": raw[c]["lms_name"], "id": raw[c]["batch_id"],
                              "claimed": raw[c]["claimed"], "rows": len(rows[c])} for c in CODES}},
        "sessions": sessions_section, "recap": recap_section, "trainers": trainer_section,
        "roster_xlsx": str(roster_path), "marked_xlsx": str(marked_path),
    }
    write_store(STORE_LOCAL, marked_bytes, tabs, DATA, summary, meta_extra)
    log(f"   wrote {STORE_LOCAL} ({STORE_LOCAL.stat().st_size / 1e6:.1f} MB)")
    for w in warnings:
        log("   WARN " + w)
    rc = _deliver([roster_path, marked_path], STORE_LOCAL, args.no_f, stamp)
    if args.upload:
        _upload_store(STORE_LOCAL)
    log(f"done in {time.time() - t0:.0f}s")
    return rc


def _store_folder() -> str:
    import pipeline
    fid = pipeline.load_config().get("store_folder_id") or ""
    if not fid:
        raise SystemExit("--upload needs STORE_FOLDER_ID (env) or drive.store_folder_id "
                         "in .streamlit/secrets.toml - the private folder the AI CAP store lives in")
    return fid


def _upload_workbooks(roster_path: pathlib.Path, marked_path: pathlib.Path) -> dict:
    """Both workbooks to the store folder, replaced in place, under FIXED names
    so the app can find them. Their Drive ids go into the store's meta so the
    deployed BSIAI tab can offer them as downloads."""
    import live_data
    svc, _cfg = _drive()
    fid = _store_folder()
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    out = {}
    for key, name, path in (("roster_xlsx_file_id", ROSTER_NAME, roster_path),
                            ("marked_xlsx_file_id", MARKED_NAME, marked_path)):
        out[key] = live_data.upload_to_folder(svc, fid, name, path.read_bytes(), mime=xlsx)
        log(f"   uploaded {name} -> {out[key]}")
    return out


def _upload_store(store: pathlib.Path) -> None:
    import live_data
    svc, _cfg = _drive()
    fid = _store_folder()
    got = live_data.upload_to_folder(svc, fid, STORE_NAME, store.read_bytes())
    log(f"   uploaded {STORE_NAME} -> {got}")


def _deliver(files: list, store: pathlib.Path | None, no_f: bool, stamp: str) -> int:
    """Copy the deliverables to F: (the owner's drive for outputs)."""
    if no_f:
        return 0
    try:
        F_ROOT.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, F_ROOT / f.name)
            log(f"   -> {F_ROOT / f.name}")
        if store:
            F_STORE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(store, F_STORE_DIR / f"bsiai_{stamp}.duckdb")
            shutil.copy2(store, F_STORE_DIR / "bsiai_latest.duckdb")
            log(f"   -> {F_STORE_DIR / f'bsiai_{stamp}.duckdb'} (+ bsiai_latest.duckdb)")
    except OSError as e:
        log(f"   !! could not copy to {F_ROOT}: {e} — the files are in {CACHE}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
