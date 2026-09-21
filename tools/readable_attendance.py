"""A human-readable attendance Sheet, built from the published store.

WHY THIS IS A SEPARATE SHEET, not formatting applied to the roster.

The roster the pipeline reads cannot carry formatting. `attendance_core`,
`carryforward` and `lms_roster` each do load_workbook -> wb.save, and
`upload_to_folder` replaces the Sheet's content wholesale, so any styling is
destroyed by the next weekly run. Worse, the roster's read path is brittle in
four independent places - `dashboard_core._col_idx` matches header text on
exact equality and falls back to a hard-coded column index, and both
`live_data` and `attendance_core` start their session scan at an absolute
column - so renaming or reordering anything there risks silently wrong
numbers. The roster stays exactly as it is.

WHY IT HAS NO PER-STUDENT ROWS.

The roster is unreadable *because* it is 64,069 students wide by 673 session
columns: 930 columns across 25 tabs, of which `amount` is 100% empty,
`Whatsaap Number` 93% and `broadcast mail` 89%. Nobody reads attendance one
student at a time. So this view is per SESSION and per DOMAIN, which is the
shape the questions actually come in - how did this weekend go, which domain
is slipping - and it lands in well under a megabyte.

Four tabs:
    Summary        one row per batch
    Sessions       one row per session (the room that ran)
    By domain      one row per session x domain
    Weekend reach  per cohort per weekend: best day, and either-day reach

Run after the pipeline publishes:
    python tools/readable_attendance.py                 # build + upload
    python tools/readable_attendance.py --dry-run       # build locally only
    python tools/readable_attendance.py --out x.xlsx    # keep a local copy
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import duckdb                                                  # noqa: E402
import openpyxl                                                # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill       # noqa: E402
from openpyxl.utils import get_column_letter                   # noqa: E402

import live_data                                               # noqa: E402
import pipeline                                                # noqa: E402

SHEET_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DEFAULT_NAME = "AI CAP Attendance — Readable"

# Verified against a real round trip (2026-09-21): bold, font colour, solid
# fills, freeze panes and column widths all survive Drive's xlsx -> Sheet
# import. Percentages are written as REAL NUMBERS with a number format rather
# than "51.2%" strings, so they stay sortable and chartable in Sheets.
HDR_FONT = Font(bold=True, color="FFFFFF", size=11)
HDR_FILL = PatternFill("solid", fgColor="2F5496")
TITLE = Font(bold=True, size=14)
BOLD = Font(bold=True)
PCT = "0.0%"

# Static fills chosen at build time instead of a conditional-formatting rule:
# static styling is verified to survive the import, conditional rules are not.
BANDS = ((0.50, "C6E7D6"), (0.35, "E8F2DA"), (0.20, "FDF2D0"), (0.0, "FBDDDD"))


def band(p):
    """The fill for an attendance fraction, or None when there is no value."""
    if p is None:
        return None
    for floor, rgb in BANDS:
        if p >= floor:
            return PatternFill("solid", fgColor=rgb)
    return None


def head(ws, labels, row=1):
    for c, label in enumerate(labels, 1):
        cell = ws.cell(row=row, column=c, value=label)
        cell.font = HDR_FONT
        cell.fill = HDR_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = ws.cell(row=row + 1, column=1).coordinate


def widths(ws, spec):
    for col, w in spec.items():
        ws.column_dimensions[col].width = w


def pct_cell(ws, r, c, present, total):
    """A real fraction, formatted as a percentage and banded by value."""
    cell = ws.cell(row=r, column=c)
    if not total:
        cell.value = None
        return
    cell.value = present / total
    cell.number_format = PCT
    f = band(cell.value)
    if f:
        cell.fill = f


def room_of(s):
    """What this row actually is, in words rather than a blank."""
    if s.get("is_intro"):
        return "Intro call"
    pod = s.get("pod")
    if not pod:
        return "All Domains"
    return pod


def weekend_of(mm, year=None):
    """Sat/Sun pairs share a key, so a weekend reads as one block."""
    y = year or datetime.date.today().year
    mo, da = (int(x) for x in mm.split("_")[-2:])
    d = datetime.date(y, mo, da)
    return (d - datetime.timedelta(days=(d.weekday() - 5) % 7)).isoformat()


def bnum(code):
    m = re.search(r"\d+", str(code))
    return int(m.group()) if m else 0


# ────────────────────────────── the tabs ─────────────────────────────────────
def tab_summary(wb, DATA, summary, generated):
    ws = wb.active
    ws.title = "Summary"
    ws.cell(row=1, column=1, value="AI CAP attendance").font = TITLE
    ws.cell(row=2, column=1,
            value=f"data as of {generated} · {summary['batches']} batches · "
                  f"{summary['enrolled']:,} enrolled · "
                  f"{summary['sessions']} sessions")
    head(ws, ["Batch", "Strength", "Active", "Sessions", "Avg attendance",
              "Best", "Worst", "Latest session", "Latest"], row=4)
    r = 5
    for code in sorted(DATA, key=bnum):
        d = DATA[code]
        real = [s for s in d["sessions"] if s.get("mm")]
        last = real[-1] if real else None
        ws.cell(row=r, column=1, value=code).font = BOLD
        ws.cell(row=r, column=2, value=d["strength"]).number_format = "#,##0"
        ws.cell(row=r, column=3, value=d["active"]).number_format = "#,##0"
        ws.cell(row=r, column=4, value=len(real))
        for col, val in ((5, d["avg_pct"]), (6, d["peak"]), (7, d["low"])):
            c = ws.cell(row=r, column=col, value=(val or 0) / 100)
            c.number_format = PCT
            if col == 5 and (f := band(c.value)):
                c.fill = f
        if last:
            ws.cell(row=r, column=8, value=f"{last['date_lbl']} · {room_of(last)}")
            pct_cell(ws, r, 9, last["present"], last["total"])
        r += 1
    widths(ws, {"A": 8, "B": 11, "C": 11, "D": 10, "E": 15, "F": 9, "G": 9,
                "H": 30, "I": 10})
    return r


def tab_sessions(wb, DATA):
    ws = wb.create_sheet("Sessions")
    head(ws, ["Batch", "Date", "Room", "Topic", "Trainer",
              "Present", "Absent", "Invited", "Attendance", "Rating", "NPS"])
    r = 2
    for code in sorted(DATA, key=bnum):
        for s in DATA[code]["sessions"]:
            if not s.get("mm"):
                continue                      # intro rows measure something else
            ws.cell(row=r, column=1, value=code)
            ws.cell(row=r, column=2, value=s["date_lbl"])
            ws.cell(row=r, column=3, value=room_of(s))
            ws.cell(row=r, column=4, value=s.get("topic") or "")
            ws.cell(row=r, column=5, value=s.get("mentor") or s.get("trainer") or "")
            ws.cell(row=r, column=6, value=s["present"]).number_format = "#,##0"
            ws.cell(row=r, column=7, value=s.get("absent")).number_format = "#,##0"
            ws.cell(row=r, column=8, value=s["total"]).number_format = "#,##0"
            pct_cell(ws, r, 9, s["present"], s["total"])
            if s.get("rating") is not None:
                ws.cell(row=r, column=10, value=s["rating"]).number_format = "0.00"
            if s.get("rating_nps") is not None:
                ws.cell(row=r, column=11, value=s["rating_nps"])
            r += 1
    widths(ws, {"A": 8, "B": 10, "C": 20, "D": 46, "E": 20, "F": 10, "G": 10,
                "H": 10, "I": 12, "J": 8, "K": 7})
    ws.auto_filter.ref = f"A1:K{max(1, r - 1)}"
    return r


def tab_by_domain(wb, DATA):
    """One row per session x domain, from the split computed in data.py.

    This is the tab that did not exist before: L2 records an All Domains day or
    a shared room as ONE session, so eleven very different turnouts arrived as
    a single number - B39's 5 Sep averaged 61.2% while Content Creators came in
    at 69.7% and Students at 50.8%.
    """
    ws = wb.create_sheet("By domain")
    head(ws, ["Batch", "Date", "Room", "Topic", "Domain",
              "Present", "Invited", "Attendance"])
    r = 2
    for code in sorted(DATA, key=bnum):
        for s in DATA[code]["sessions"]:
            split = s.get("pod_split") or {}
            if not split:
                continue
            for dom in sorted(split):
                part = split[dom]
                ws.cell(row=r, column=1, value=code)
                ws.cell(row=r, column=2, value=s["date_lbl"])
                ws.cell(row=r, column=3, value=room_of(s))
                ws.cell(row=r, column=4, value=s.get("topic") or "")
                ws.cell(row=r, column=5, value=dom)
                ws.cell(row=r, column=6, value=part["present"]).number_format = "#,##0"
                ws.cell(row=r, column=7, value=part["total"]).number_format = "#,##0"
                pct_cell(ws, r, 8, part["present"], part["total"])
                r += 1
    widths(ws, {"A": 8, "B": 10, "C": 20, "D": 46, "E": 22, "F": 10, "G": 10,
                "H": 12})
    ws.auto_filter.ref = f"A1:H{max(1, r - 1)}"
    return r


def tab_weekend_reach(wb, DATA):
    """Per batch per weekend: the best single day, and the day rollup.

    `by_date` already counts each student once across every room they were
    invited to that day, which is the honest per-day figure. True either-day
    reach needs per-student marks and therefore the marked workbook, so it is
    deliberately not claimed here - see the note written into the tab.
    """
    ws = wb.create_sheet("Weekend reach")
    ws.cell(row=1, column=1,
            value="Per-day figures count each student once across every room "
                  "they were invited to. Combining two days into unique reach "
                  "needs per-student marks, which live in the marked workbook, "
                  "not here.").font = Font(italic=True, size=10)
    head(ws, ["Batch", "Weekend of", "Day", "Rooms that ran",
              "Present", "Invited", "Attendance"], row=3)
    r = 4
    for code in sorted(DATA, key=bnum):
        for d in DATA[code].get("by_date") or ():
            if not d.get("mm"):
                continue
            ws.cell(row=r, column=1, value=code)
            ws.cell(row=r, column=2, value=weekend_of(d["mm"]))
            ws.cell(row=r, column=3, value=d["date_lbl"])
            ws.cell(row=r, column=4, value=d.get("n_pods"))
            ws.cell(row=r, column=5, value=d["present"]).number_format = "#,##0"
            ws.cell(row=r, column=6, value=d["total"]).number_format = "#,##0"
            pct_cell(ws, r, 7, d["present"], d["total"])
            r += 1
    widths(ws, {"A": 8, "B": 13, "C": 10, "D": 15, "E": 10, "F": 10, "G": 12})
    ws.auto_filter.ref = f"A3:G{max(3, r - 1)}"
    return r


def build(store_path):
    con = duckdb.connect(store_path, read_only=True)
    try:
        meta = {k: json.loads(v) for k, v in
                con.execute("SELECT key, value FROM meta").fetchall()}
    finally:
        con.close()
    DATA, summary = meta["DATA"], meta["summary"]
    wb = openpyxl.Workbook()
    n_sum = tab_summary(wb, DATA, summary, meta["generated_at"])
    n_ses = tab_sessions(wb, DATA)
    n_dom = tab_by_domain(wb, DATA)
    n_wk = tab_weekend_reach(wb, DATA)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), {"batches": n_sum - 5, "sessions": n_ses - 2,
                            "domain_rows": n_dom - 2, "day_rows": n_wk - 4,
                            "generated_at": meta["generated_at"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--parent", default="", help="Shared Drive / folder id "
                                                 "(default: store_folder_id)")
    ap.add_argument("--store", default=os.path.join(ROOT, ".cache",
                                                    "attendance.duckdb"))
    ap.add_argument("--out", default="", help="also save the xlsx locally")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and report, create nothing on Drive")
    a = ap.parse_args()

    data, rep = build(a.store)
    print(f"   built from the store of {rep['generated_at']}")
    print(f"   {rep['batches']} batch row(s) · {rep['sessions']} session row(s) · "
          f"{rep['domain_rows']} domain row(s) · {rep['day_rows']} day row(s)")
    print(f"   {len(data) / 1e6:.2f} MB "
          f"(Google refuses to EXPORT a Sheet over 10 MB, and every reader "
          f"goes through that export)")
    if a.out:
        with open(a.out, "wb") as fh:
            fh.write(data)
        print(f"   saved {a.out}")
    if a.dry_run:
        print("\n--dry-run: nothing was created on Drive.")
        return

    cfg = pipeline.load_config()
    live_data.set_service_account(cfg["sa_info"], scopes=pipeline.RW_SCOPES)
    svc = live_data._drive_service()
    parent = a.parent or cfg["store_folder_id"]
    if not parent:
        sys.exit("No --parent and no store_folder_id: a service account has no "
                 "Drive of its own, so this must live on a Shared Drive.")

    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=XLSX_MIME, resumable=True)
    existing = live_data.find_in_folder(svc, parent, a.name)
    if existing:
        # Replace in place so the URL people have bookmarked keeps working.
        f = svc.files().update(fileId=existing["id"], media_body=media,
                               supportsAllDrives=True, fields="id,name").execute()
        print(f"   refreshed {f['name']}")
    else:
        f = svc.files().create(body={"name": a.name, "parents": [parent],
                                     "mimeType": SHEET_MIME},
                               media_body=media, supportsAllDrives=True,
                               fields="id,name").execute()
        print(f"   created {f['name']}")
    print(f"   https://docs.google.com/spreadsheets/d/{f['id']}/edit")


if __name__ == "__main__":
    main()
