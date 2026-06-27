"""
data.py — pure, Streamlit-free data layer for the AI CAP attendance dashboard.

It turns raw sheet rows (list-of-lists of strings, exactly what gspread's
get_all_values() returns, or what we synthesise from the .xlsx) into the
per-batch object the prototype renders, applying every business rule from the
spec. No I/O, no Streamlit — so the numbers are unit-testable on their own.

build(sheets, l2_lookup) -> (DATA, summary) where:
  sheets     = {tab_name: [[cell, ...], ...]}      # one entry per worksheet
  l2_lookup  = {(batch, mm_dd): topic, mm_dd: topic}  # from the L2 schedule (optional)

  DATA[code] = {
    code, strength, active, n_sessions, avg_pct, peak, low,
    sessions: [{date_lbl, mm, topic, present, absent, total, pct,
                present_only, no_l2}],
    closing:  [{type, count, pct, att}],
  }
  summary = {batches, enrolled, active, sessions}

Dynamic by design: batches, sessions, and closing-type values are all derived
from the data at read time — nothing about counts is hardcoded.
"""
from __future__ import annotations
import re
from collections import defaultdict

from attendance_core import _mmdd  # proven date parser: "4th April", "31st may", "2026_05_31", datetimes

# ── business-rule constants (the only hardcoded things, per spec) ─────────────
# Payment values that mean NOT active (covers the real misspellings in the data).
_REFUND_TOKENS = ("refund", "unidentified", "not paid", "cancel",
                  "undifined", "unidetified", "undefined")

# Colour bands for attendance pills/bars.
BAND_HIGH, BAND_MID = 45, 30  # ≥45 green, 30–45 amber, <30 red

# Valid-session thresholds.
_MIN_MARKED_FRAC = 0.30   # a real session has Present/Absent for >30% of strength
_MIN_PRESENT_FRAC = 0.01  # …and present > 1% of strength (drops broken near-empty cols)

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_BATCH_RE = re.compile(r"^\s*ai\s*cap\s*(b\d+)\s*$", re.I)


# ── tiny helpers ──────────────────────────────────────────────────────────────
def band(pct: float) -> str:
    """'high' / 'mid' / 'low' — the UI maps these to the prototype's colours."""
    if pct >= BAND_HIGH:
        return "high"
    if pct >= BAND_MID:
        return "mid"
    return "low"


def is_active(payment) -> bool:
    """Active = Payment non-blank AND not a refund/unidentified/cancel/etc."""
    p = str(payment or "").strip().lower()
    if not p:
        return False
    return not any(t in p for t in _REFUND_TOKENS)


def normalize_closing(raw) -> str:
    """Closing-type buckets (applied before grouping). Unknown values are kept as
    their own bucket — never dropped."""
    s = str(raw or "").strip().lower()
    if not s:
        return "Unknown"
    if "collection" in s:
        return "BDA Collection"
    if "clos" in s:                 # catches the 'BDA Closimg' typo and all casings
        return "BDA Closing"
    if "system" in s:
        return "System"
    if "lwb" in s or "resume" in s:
        return "LWB Resume"
    return str(raw).strip()         # unrecognised → keep verbatim (own bucket)


def batch_label(tab: str) -> str | None:
    """Roster tab name -> batch label ('AI CAP B17' -> 'B17'); None for non-batch
    tabs and the Zoom '*Att' helper tabs."""
    if "att" in tab.lower():
        return None
    m = _BATCH_RE.match(tab)
    return m.group(1).upper() if m else None


def date_label(mm: str | None) -> str | None:
    """'04_27' -> '27 Apr' (clean, consistent display label)."""
    if not mm:
        return None
    try:
        m, d = mm.split("_")
        return f"{int(d)} {_MONTHS[int(m)]}"
    except (ValueError, IndexError):
        return None


def _cell(row, i):
    return row[i] if (i is not None and 0 <= i < len(row)) else None


def _nonempty(v) -> bool:
    return v is not None and str(v).strip() != ""


def _find_col(header, *needles):
    """Index of the first header cell containing ALL needles (case-insensitive)."""
    for i, c in enumerate(header):
        h = str(c or "").strip().lower()
        if all(n in h for n in needles):
            return i
    return None


def _find_header_row(rows) -> int:
    """Scan the first ~5 rows for the field-name row (don't assume row 1)."""
    keys = ("batch name", "country code", "contry code", "registered number", "payment")
    for i, row in enumerate(rows[:5]):
        joined = " | ".join(str(c or "").strip().lower() for c in row[:14])
        if any(k in joined for k in keys):
            return i
    return 0


# ── per-batch build ─────────────────────────────────────────────────────────
def build_batch(rows: list[list], batch: str, l2_lookup: dict | None) -> dict | None:
    """Compute one batch's dashboard object from its raw rows. None if the tab
    doesn't look like a roster (no mail/closing columns) or has no strength."""
    if not rows:
        return None
    hr = _find_header_row(rows)
    header = rows[hr]
    date_row = rows[hr - 1] if hr >= 1 else rows[hr]   # dates sit in the row above the header
    mail_col = _find_col(header, "registered", "mail")
    pay_col = _find_col(header, "payment")
    close_col = _find_col(header, "closing")
    if mail_col is None or close_col is None:
        return None

    width = max((len(r) for r in rows), default=0)
    data = rows[hr + 1:]
    enrolled = [r for r in data if _nonempty(_cell(r, mail_col))]
    strength = len(enrolled)
    if strength == 0:
        return None
    active = sum(1 for r in enrolled if is_active(_cell(r, pay_col)))

    l2_lookup = l2_lookup or {}

    # discover + validate session columns (everything after Closing Type)
    sessions = []
    for c in range(close_col + 1, width):
        present = absent = 0
        for r in enrolled:
            v = str(_cell(r, c) or "").strip().lower()
            if v == "present":
                present += 1
            elif v == "absent":
                absent += 1
        marked = present + absent
        if not (marked > _MIN_MARKED_FRAC * strength and present > _MIN_PRESENT_FRAC * strength):
            continue  # un-synced/empty formula column or broken near-empty column

        mm = _mmdd(_cell(date_row, c)) or _mmdd(_cell(header, c))
        hraw = _cell(header, c)
        # a real roster header (topic) is one that ISN'T itself a date
        roster_topic = None if (hraw is None or _mmdd(hraw)) else str(hraw).strip()
        l2_topic = l2_lookup.get((batch, mm)) or l2_lookup.get(mm)
        topic = l2_topic or roster_topic or (f"Session on {date_label(mm)}" if mm else "Live session")

        sessions.append({
            "col": c, "mm": mm,
            "date_lbl": date_label(mm) or (str(hraw).strip() if hraw else "—"),
            "topic": topic,
            "present": present, "absent": absent, "total": strength,
            "pct": round(present / strength * 100, 1),
            "present_only": absent == 0,
            "no_l2": l2_topic is None,
        })

    if not sessions:
        return None

    if all(s["mm"] for s in sessions):           # sort chronologically when fully dated
        sessions.sort(key=lambda s: s["mm"])
    valid_cols = [s["col"] for s in sessions]
    n_valid = len(sessions)

    # closing-type breakdown
    groups: dict[str, list] = defaultdict(list)
    for r in enrolled:
        groups[normalize_closing(_cell(r, close_col))].append(r)
    closing = []
    for ctype, grp in groups.items():
        pres = sum(1 for r in grp for c in valid_cols
                   if str(_cell(r, c) or "").strip().lower() == "present")
        att = (pres / (len(grp) * n_valid) * 100) if (grp and n_valid) else 0.0
        closing.append({
            "type": ctype, "count": len(grp),
            "pct": round(len(grp) / strength * 100, 1),
            "att": round(att, 1),
        })
    closing.sort(key=lambda c: c["count"], reverse=True)   # biggest channel first

    pcts = [s["pct"] for s in sessions]
    return {
        "code": batch, "strength": strength, "active": active,
        "n_sessions": n_valid,
        "avg_pct": round(sum(pcts) / len(pcts), 1),
        "peak": max(pcts), "low": min(pcts),
        "sessions": sessions, "closing": closing,
    }


def build(sheets: dict[str, list[list]], l2_lookup: dict | None = None):
    """Build the full DATA dict + summary from all worksheets. Auto-discovers
    batch tabs; never hardcodes the batch or session list."""
    data = {}
    for tab, rows in sheets.items():
        b = batch_label(tab)
        if not b:
            continue
        bd = build_batch(rows, b, l2_lookup)
        if bd:
            data[b] = bd

    order = sorted(data, key=lambda b: int(re.sub(r"\D", "", b) or 0))
    data = {b: data[b] for b in order}
    summary = {
        "batches": len(data),
        "enrolled": sum(d["strength"] for d in data.values()),
        "active": sum(d["active"] for d in data.values()),
        "sessions": sum(d["n_sessions"] for d in data.values()),
    }
    return data, summary
