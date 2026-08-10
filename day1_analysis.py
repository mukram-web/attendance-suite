"""
day1_analysis.py — payment (col I) and close type (col J) cut against who actually
turned up, for the N most recent batches. Generic: nothing about a batch, a date
or a webinar id is hardcoded, so a new batch appears on its own.

Two sessions are analysed per batch:
  • day one  — the batch's FIRST real class
  • latest   — its most recent real class

**Which session is "day one" (the one rule that matters).** Marketing walkthroughs
share the batch's folder naming but are not classes — B34's 2026-07-30 "AI Career
Accelerator Program Walkthrough" and B31's 2026-07-15 one would drag B34's day-1
attendance from 60% down to 26%. The L2 schedule is the discriminator: every real
class is registered there by webinar id, walkthroughs are not. Verified 2026-08-10
across B31-B35 (28 sessions): both known walkthroughs absent from L2, both known
real day-ones present, and zero real-looking sessions missing from L2. A title
guard backs it up in case an unregistered class ever appears.

Attendee → roster matching is email OR phone (roster cols C/F and B/E against the
Zoom report's Email and Phone, phones cut to the last 10 digits). Email alone
under-counts badly — B34 day 1: 1,594 matched on email vs 1,753 on either.

Everything returned is an AGGREGATE — counts and percentages only, no contact
details — so it is safe to embed in the dashboard page.
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import datetime

import attendance_core as ac
import live_data

# Titles that read like a funnel/marketing event. L2 membership is the actual
# rule — verified across B31-B35 that walkthroughs are never registered there —
# so this only RAISES A FLAG. It must not veto: real topics legitimately contain
# words like "onboarding" or "demo", and vetoing an L2-registered class would
# silently make class 2 look like day one.
TITLE_FLAG = re.compile(r"walkthrough|orientation", re.I)

ATTENDEE_RE = re.compile(r"attendee_(\d+)_(\d{4}_\d{2}_\d{2})", re.I)
BATCH_RE = re.compile(r"\bB0*(\d{1,3})\b", re.I)

# Columns I and J are hand-typed and the vocabulary drifts every batch — the same
# workbook holds "Full Paid" / "Full paid" / "Full", "Booking" / "Booking Amount",
# "Partial" / "Partially Paid", "BDA Closing" / "Bda closing" / "BDA Closimg", and
# eight spellings of unidentified-refunded. Matching on exact strings makes the
# headline tiles read 0 with no warning, so canonicalise on meaning, not spelling.
def _canon_payment(raw) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", str(raw or "").lower()).strip()
    if not s:
        return "(blank)"
    if any(t in s for t in ("unident", "unidet", "undifin", "undefin", "refund", "cancel")):
        return "Unidentified / Refunded"
    if s.startswith("full") or "full paid" in s:
        return "Full Paid"
    if "booking" in s:
        return "Booking Amount"
    if "partial" in s:
        return "Partially Paid"
    return str(raw).strip()          # unknown → its own bucket, never dropped


def _canon_close(raw) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", str(raw or "").lower()).strip()
    if not s or not re.search(r"[a-z]", s):     # empty or numeric junk like 0.0
        return "(blank)"
    if "collection" in s:
        return "BDA Collection"
    if "clos" in s:                  # covers the real "BDA Closimg" typo
        return "BDA Closing"
    if "system" in s:
        return "System"
    if "batch" in s and ("change" in s or "shift" in s):
        return "Batch change"
    if "lwb" in s or "resume" in s:
        return "LWB Resume"
    if "unident" in s or "undefin" in s:
        return "Unidentified"
    return str(raw).strip()

PAY_ORDER = ["Full Paid", "Booking Amount", "Partially Paid", "Unidentified / Refunded"]
CLOSE_ORDER = ["BDA Closing", "BDA Collection", "System", "Batch change",
               "LWB Resume", "Unidentified"]
UNMATCHED = "Not on this roster"


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def norm_phone(v) -> str:
    """Last 10 digits — drops +, spaces, a leading 0 and the 91 country code.

    Numbers must be de-floated FIRST: openpyxl hands back 919704189186.0, and
    blindly stripping non-digits would turn the trailing '.0' into an extra '0',
    shifting every phone one digit and silently killing all phone matches."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        s = str(int(v))
    else:
        s = re.sub(r"\.0$", "", str(v).strip())
    d = re.sub(r"\D", "", s).lstrip("0")
    return d[-10:] if len(d) >= 10 else ""


# ───────────────────────────── session discovery ─────────────────────────────
def folder_batches(name: str) -> set:
    """Batch numbers a session folder belongs to, using the SAME rule the marker
    uses (attendance_core.extract_batches) so the two can't disagree — it expands
    ranges like "AI CAP B1 - AI CAP B22" and '+' lists, which a plain regex over
    the name would reduce to just the endpoints."""
    try:
        return {n for _track, n in ac.extract_batches(name)}
    except Exception:
        return {int(m) for m in BATCH_RE.findall(name)}


def discover_sessions(svc, attendee_folder_id: str, batches: set[int]) -> tuple:
    """({batch_number: [session dicts]}, errors) for the wanted batches.

    Duplicate session folders are real on this drive (same name, different
    exports), so identical (webinar id, date) pairs collapse to the LARGEST file
    — the fullest export. A folder we cannot list is recorded rather than raised:
    losing one session must not blank the whole analysis."""
    out: dict[int, dict] = {b: {} for b in batches}
    errors: list[str] = []
    try:
        top = live_data._list_children(svc, attendee_folder_id)
    except Exception as e:
        return {b: [] for b in batches}, [f"could not list the sessions drive: {e}"]

    for f in top:
        if f["mimeType"] != live_data._FOLDER_MIME:
            continue
        hit = folder_batches(f["name"]) & batches
        if not hit:
            continue
        try:
            kids = live_data._list_children(svc, f["id"])
        except Exception as e:
            errors.append(f"could not list “{f['name']}”: {e}")
            continue
        for kid in kids:
            m = ATTENDEE_RE.match(kid["name"])
            if not m:
                continue
            wid, date = m.group(1), m.group(2)
            size = int(kid.get("size") or 0)
            for b in hit:
                prev = out[b].get((wid, date))
                if not prev or size > prev["size"]:
                    out[b][(wid, date)] = {
                        "webinar_id": wid, "date": date, "folder": f["name"],
                        "file_id": kid["id"], "size": size,
                    }
    return ({b: sorted(v.values(), key=lambda s: (s["date"], s["webinar_id"]))
             for b, v in out.items()}, errors)


def is_real_class(session: dict, wid_map: dict) -> bool:
    """A class is a session the L2 schedule registers by webinar id — that alone.
    Walkthroughs are provably never registered there, and a title guard must not
    veto an L2-registered session (real topics contain words like 'onboarding'),
    so the title only raises a visible flag, never excludes."""
    return session["webinar_id"] in wid_map


def pick_sessions(sessions: list, wid_map: dict, notes: list) -> tuple:
    """(day_one, latest) — earliest and most recent real class. latest is None
    when the batch has only had one class so far."""
    real = [s for s in sessions if is_real_class(s, wid_map)]
    if not real:
        return None, None
    day_one = real[0]
    latest = real[-1] if len(real) > 1 else None
    for s, role in ((day_one, "day1"), (latest, "latest")):
        if s:
            s["title"] = wid_map.get(s["webinar_id"], (None, ""))[1] or _title_from_folder(s["folder"])
            s["role"] = role
            if TITLE_FLAG.search(s["folder"]):
                notes.append(f"{s['date']} “{_title_from_folder(s['folder'])}” is "
                             "titled like a walkthrough but IS registered in L2 — "
                             "using it; check the schedule if that looks wrong")
    return day_one, latest


def _title_from_folder(folder: str) -> str:
    parts = re.split(r"\s+-\s+", folder)
    return parts[-1].strip() if len(parts) > 2 else folder


def date_label(yyyy_mm_dd: str) -> str:
    try:
        return datetime.strptime(yyyy_mm_dd, "%Y_%m_%d").strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return yyyy_mm_dd


# ─────────────────────────────── roster side ─────────────────────────────────
def _header_row(rows: list) -> int:
    for i, row in enumerate(rows[:4]):
        joined = " | ".join(str(c or "").strip().lower() for c in row[:14])
        if "registered number" in joined or "payment" in joined:
            return i
    return 0


def _col(header: list, *needles) -> int | None:
    for i, c in enumerate(header):
        h = str(c or "").strip().lower()
        if all(n in h for n in needles):
            return i
    return None


def read_roster(rows: list) -> dict | None:
    """Roster tab rows -> {rows: [{payment, close, present}], by_email, by_phone}.
    Columns are found by HEADER NAME, never by fixed letter — some tabs (B29) are
    shifted a column right."""
    if not rows:
        return None
    hr = _header_row(rows)
    header = rows[hr]
    ci = {
        "num": _col(header, "registered", "number"),
        "mail": _col(header, "registered", "mail"),
        "wa": _col(header, "whatsa"),
        "bcast": _col(header, "broadcast"),
        "pay": _col(header, "payment"),
        "close": _col(header, "clos"),
    }
    if ci["pay"] is None:
        return None
    if ci["close"] is None:
        ci["close"] = ci["pay"] + 1
    if ci["mail"] is None and ci["num"] is None:
        return None

    def cell(row, i):
        return row[i] if (i is not None and i < len(row)) else None

    entries, by_email, by_phone = [], {}, {}
    for row in rows[hr + 1:]:
        key_val = cell(row, ci["num"]) or cell(row, ci["mail"])
        if key_val is None or str(key_val).strip() == "":
            continue
        idx = len(entries)
        entries.append({
            "payment": _canon_payment(cell(row, ci["pay"])),
            "close": _canon_close(cell(row, ci["close"])),
        })
        for c in (ci["mail"], ci["bcast"]):
            e = str(cell(row, c) or "").strip().lower()
            if "@" in e:
                by_email.setdefault(e, idx)
        for c in (ci["num"], ci["wa"]):
            p = norm_phone(cell(row, c))
            if p:
                by_phone.setdefault(p, idx)
    if not entries:
        return None
    return {"rows": entries, "by_email": by_email, "by_phone": by_phone}


# ────────────────────────────── attendee side ────────────────────────────────
def read_attendees(raw: bytes) -> tuple:
    """(people, unique_viewers) from a Zoom attendee report. people maps email ->
    {phones, minutes, intervals}; only rows with recorded time count as present."""
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))

    unique_viewers = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == "Topic" and i + 1 < len(rows):
            idx = {c.strip().lower(): j for j, c in enumerate(row)}
            try:
                unique_viewers = int(rows[i + 1][idx["unique viewers"]])
            except (KeyError, IndexError, ValueError):
                unique_viewers = None
            break

    people, section, idx = {}, None, None
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        c0 = row[0].strip().lower()
        if c0 in ac_SECTIONS:
            section, idx = c0, None
            continue
        if section not in ("attendee details", "other attended"):
            continue
        if idx is None:
            idx = {c.strip().lower(): j for j, c in enumerate(row)}
            continue

        def g(key):
            j = idx.get(key)
            return row[j].strip() if j is not None and j < len(row) else ""

        email = g("email").lower()
        if not email or "@" not in email:
            continue
        if (g("attended").lower() or "yes") != "yes":
            continue
        p = people.setdefault(email, {"phones": set(), "minutes": 0.0, "intervals": []})
        ph = norm_phone(g("phone"))
        if ph:
            p["phones"].add(ph)
        join, leave = _parse_time(g("join time")), _parse_time(g("leave time"))
        if join and leave and leave > join:
            p["intervals"].append((join, leave))
        else:
            try:
                p["minutes"] += float(g("time in session (minutes)"))
            except ValueError:
                pass

    attended = {e: p for e, p in people.items()
                if not INTERNAL_PAT.search(e) and _minutes(p) > 0}
    return attended, unique_viewers


ac_SECTIONS = {"host details", "panelist details", "attendee details", "other attended"}
INTERNAL_PAT = re.compile(r"@(houseofedtech\.in|be10x\.com|fireflies\.ai)$|fireflies", re.I)
_TIME_FMTS = ("%b %d, %Y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p",
              "%d/%m/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S")


def _parse_time(s):
    s = (s or "").strip().strip('"')
    for f in _TIME_FMTS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def _minutes(p) -> float:
    """Merged in-session minutes — rejoins and two devices don't double count."""
    total, iv = p["minutes"], sorted(p["intervals"])
    if iv:
        cur_s, cur_e = iv[0]
        for s, e in iv[1:]:
            if s <= cur_e:
                cur_e = max(cur_e, e)
            else:
                total += (cur_e - cur_s).total_seconds() / 60
                cur_s, cur_e = s, e
        total += (cur_e - cur_s).total_seconds() / 60
    return total


# ─────────────────────────────── the analysis ────────────────────────────────
def _buckets(entries, key, order, present_flags):
    counts, present = defaultdict(int), defaultdict(int)
    for i, e in enumerate(entries):
        counts[e[key]] += 1
        present[e[key]] += bool(present_flags[i])
    keys = [k for k in order if k in counts] + \
           [k for k in sorted(counts) if k not in order]
    n = len(entries)
    return [{"k": k, "n": counts[k], "pct": _pct(counts[k], n),
             "present": present[k], "rate": _pct(present[k], counts[k])} for k in keys]


def _attendee_buckets(counts, order, matched, unique_total):
    keys = [k for k in order if k in counts] + \
           [k for k in sorted(counts) if k not in order and k != UNMATCHED]
    out = [{"k": k, "n": counts[k], "pct_matched": _pct(counts[k], matched),
            "pct_all": _pct(counts[k], unique_total)} for k in keys]
    if counts.get(UNMATCHED):
        out.append({"k": UNMATCHED, "n": counts[UNMATCHED], "pct_matched": None,
                    "pct_all": _pct(counts[UNMATCHED], unique_total)})
    return out


def analyse_session(roster: dict, raw: bytes, session: dict) -> dict:
    """One session's numbers, both sides: roster-side presence and attendee-side
    payment/close mix."""
    attended, unique_viewers = read_attendees(raw)
    entries = roster["rows"]
    present_flags = [False] * len(entries)
    pay_counts, close_counts, matched = defaultdict(int), defaultdict(int), 0

    for email, p in attended.items():
        i = roster["by_email"].get(email)
        if i is None:
            for ph in p["phones"]:
                if ph in roster["by_phone"]:
                    i = roster["by_phone"][ph]
                    break
        if i is None:
            pay_counts[UNMATCHED] += 1
            close_counts[UNMATCHED] += 1
            continue
        matched += 1
        present_flags[i] = True
        pay_counts[entries[i]["payment"]] += 1
        close_counts[entries[i]["close"]] += 1

    n = len(entries)
    present_rows = sum(present_flags)
    unique_total = unique_viewers or len(attended)
    return {
        "role": session["role"],
        "date": date_label(session["date"]),
        "title": session["title"],
        "webinar_id": session["webinar_id"],
        "present": present_rows,
        "absent": n - present_rows,
        "pct": _pct(present_rows, n),
        "unique_attendees": unique_total,
        "matched": matched,
        "matched_pct": _pct(matched, unique_total),
        "unmatched": len(attended) - matched,
        # Zoom's Unique Viewers includes people we deliberately don't analyse
        # (internal accounts, viewers with no recorded time). Without this the
        # three displayed numbers can never reconcile: matched + unmatched
        # + excluded == unique_attendees.
        "excluded": max(0, unique_total - len(attended)),
        "payment": _buckets(entries, "payment", PAY_ORDER, present_flags),
        "close": _buckets(entries, "close", CLOSE_ORDER, present_flags),
        "attendee_payment": _attendee_buckets(pay_counts, PAY_ORDER, matched, unique_total),
        "attendee_close": _attendee_buckets(close_counts, CLOSE_ORDER, matched, unique_total),
    }


def build(svc, roster_tabs: dict, l2_bytes, attendee_folder_id: str,
         n_batches: int = 4, log=print) -> dict:
    """The whole payload. roster_tabs = {tab_title: rows} (only 'AI CAP B<n>' tabs
    are considered). Returns {"batches": [...], "skipped": [...], "errors": [...]}.
    skipped = expected conditions (a batch with no sessions yet); errors = things
    that mean the result is INCOMPLETE and should not silently replace a good one."""
    wid_map = ac.parse_l2(l2_bytes) if l2_bytes else {}
    if not wid_map:
        # No fallback exists (or should): without L2 there is no way to tell a
        # real class from a walkthrough, and guessing would publish wrong numbers.
        return {"batches": [], "skipped": [],
                "errors": ["L2 schedule unavailable — day one cannot be identified "
                           "without it, analysis not built"]}

    tab_by_batch = {}
    for title in roster_tabs:
        if "att" in title.lower():
            continue
        m = re.fullmatch(r"\s*AI\s*CAP\s*B(\d+)\s*", title, re.I)
        if m:
            tab_by_batch[int(m.group(1))] = title
    if not tab_by_batch:
        return {"batches": [], "skipped": ["no batch tabs found in the roster"],
                "errors": []}

    # The window is the newest N batches WITH data. A tab that exists before its
    # first class (batches sell before they teach) must not evict a live batch —
    # so consider a few extra candidates and stop once N have an analysis.
    candidates = sorted(tab_by_batch, reverse=True)[:n_batches + 4]
    found, errors = discover_sessions(svc, attendee_folder_id, set(candidates))
    out, skipped = [], []

    for b in candidates:
        if len(out) >= n_batches:
            break
        roster = read_roster(roster_tabs[tab_by_batch[b]])
        if not roster:
            skipped.append(f"B{b}: roster tab has no Payment column")
            continue
        day_one, latest = pick_sessions(found.get(b, []), wid_map, skipped_notes := [])
        skipped.extend(f"B{b}: {note}" for note in skipped_notes)
        if not day_one:
            skipped.append(f"B{b}: no session registered in L2 yet")
            continue

        sessions = {}
        for s in (day_one, latest):
            if not s:
                continue
            try:
                raw = live_data.download_cached(svc, s["file_id"])
                sessions[s["role"]] = analyse_session(roster, raw, s)
            except Exception as e:                       # one bad report ≠ no batch
                errors.append(f"B{b} {s['role']} ({s['date']}): {e}")

        if "day1" not in sessions:
            errors.append(f"B{b}: day-one report unreadable")
            continue

        entries = roster["rows"]
        n = len(entries)
        nil = [False] * n
        pay = _buckets(entries, "payment", PAY_ORDER, nil)
        close = _buckets(entries, "close", CLOSE_ORDER, nil)
        get = lambda rows, k: next((x for x in rows if x["k"] == k), {})
        bda = get(close, "BDA Closing").get("n", 0) + get(close, "BDA Collection").get("n", 0)

        out.append({
            "batch": f"B{b}",
            "batch_no": b,
            "roster": n,
            "payment": [{"k": x["k"], "n": x["n"], "pct": x["pct"]} for x in pay],
            "close": [{"k": x["k"], "n": x["n"], "pct": x["pct"]} for x in close],
            "headline": {
                "full_paid": get(pay, "Full Paid").get("n", 0),
                "full_paid_pct": get(pay, "Full Paid").get("pct"),
                "bda_closing": get(close, "BDA Closing").get("n", 0),
                "bda_collection": get(close, "BDA Collection").get("n", 0),
                "bda_total": bda, "bda_total_pct": _pct(bda, n),
            },
            "sessions": sessions,
        })
        d1 = sessions["day1"]
        extra = ""
        if "latest" in sessions:
            lt = sessions["latest"]
            extra = f" | latest {lt['date']}: {lt['pct']}%"
        log(f"   B{b}: roster {n:,} · day 1 {d1['date']} {d1['pct']}% present "
            f"({d1['matched']:,}/{d1['unique_attendees']:,} attendees matched){extra}")

    out.sort(key=lambda x: x["batch_no"])
    for s in skipped:
        log(f"   skipped — {s}")
    for e in errors:
        log(f"   ERROR — {e}")
    return {"batches": out, "skipped": skipped, "errors": errors}
