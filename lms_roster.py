"""Build the roster workbook from the LMS API instead of the Master Batch Rosters Sheet.

WHY THIS EXISTS
---------------
The Sheet is ~8.2 MB against Google's 10 MB xlsx-export ceiling. Crossing it makes
`live_data.fetch_sheet_cached` raise `exportSizeLimitExceeded` and stops the weekly
run dead (CLAUDE.md §7b). The LMS API carries the same enrolment data with no such
limit.

This is a READ-side swap and nothing more. Since B29 the pipeline has never written
marks back to the Sheet: Present/Absent lives in `Master_Batch_Rosters_marked.xlsx`
on the private Shared Drive, and `carryforward.merge_marks` re-attaches it each week
by matching IDENTITY (email, then whole phone, then last 10 digits) — never row
order. So the Sheet was only ever an enrolment-and-attribute source, and replacing
it changes nothing about marking, freezing or GATE 6.

THE THREE LAYERS
----------------
Each batch tab is assembled in this order:

1. RETAINED — every person in last week's marked workbook. This is what stops the
   swap destroying history. Measured 2026-09-15: 9,017 people across B17-B39 exist
   in the roster but NOT in their own API batch (3,355 of them are really in a
   different CAP batch, 556 only in non-CAP workshops, 5,038 not found at all).
   Dropped from the fresh workbook, `carryforward` would count them `unmatched_prev`
   and bin their marks. Retaining them costs nothing and keeps GATE 6 at zero.
2. REFRESH — for the live cohorts only, update attributes in place on an identity
   match and append anyone the API knows who is not already retained.
3. SEED — a batch with no retained rows at all (a brand-new cohort) is built purely
   from the API. This is how B41 and everything after it will enter.

A batch that is neither live nor new is FROZEN: no API call, rows reused verbatim.
That is the owner's rule — extract once, then each week refresh only the current and
previous cohort — and it also stops a refund logged today silently flipping a
student's Active flag on a session from three months ago.

THE CLOSING-TYPE RULE, AND WHY IT IS NOT A DETAIL
-------------------------------------------------
The API's `closingType` vocabulary is NARROWER than the Sheet's. It returns only
`unknown` / `bda_collection` / `system` / `l3_purchased`, and collapses BDA Closing
and Old Customer — both real, both common — into `unknown`. Measured in the live
store: Sheet-sourced B39 reads BDA Collection 39%, System 28%, Old Customer 27%,
BDA Closing 6%, Unknown 0%. API-sourced B40 reads Unknown 58%.

So `_apply` takes the API's closing type ONLY when it maps to something real. A
retained `BDA Closing` is never overwritten with a blank. New students the Sheet
never saw still land in Unknown; that is an upstream gap to raise with the LMS
owner, not something this module can invent its way out of.

WHAT THE API CANNOT SUPPLY AT ALL
---------------------------------
`amount` — `transactionLedger` is a learner's LIFETIME history across every product
(one account: 1,359 entries, 2023-2026, ₹34M), not this batch's deal value. Summing
it is usually right and silently wrong on outliers. Harmless either way: no module
reads the roster's Amount column. Left blank for new rows, preserved for retained
ones.
"""
from __future__ import annotations

import io
import re

import attendance_core as ac
import pods

# ── the column contract ──────────────────────────────────────────────────────
# Reproduced from the live Sheet, typos included: every reader locates these by
# substring needle ('whatsa', 'broadcast', 'clos', 'pod'), so 'Whatsaap Number'
# and 'Contry code' are load-bearing spellings, not mistakes to tidy up.
#
# Two constraints that are easy to break and silent when broken:
#   * `dashboard_core._find_header_row` tests cell EQUALITY for 'registered
#     number' or 'payment' — unlike the three other header detectors, which use
#     substrings. 'Registered Number' below is what satisfies it.
#   * Everything right of Closing Type is treated as a session column by
#     `dashboard_core` and `data`. POD Prefrence therefore sits at K and is read
#     as a zero-data pseudo-session — exactly as it already is for B35-B40 in the
#     live store. `attendance_core` and `carryforward` scan from K too but key on
#     `session_key`, which returns None for it, so it is skipped there.
I_CC, I_NUM, I_MAIL, I_WACC, I_WA, I_BCAST, I_BATCH, I_AMT, I_PAY, I_CLOSE, I_POD = range(11)

HEADERS = ["Contry code", "Registered Number", "Registered mail", "Contry code",
           "Whatsaap Number", "broadcast mail", "batch name", "amount",
           "Payment", "Closing Type", "POD Prefrence"]

N_FIXED = len(HEADERS)

# API value -> the spelling the roster uses, validated against B38 by the parent
# folder's cmp_values.py. 'none' maps to blank deliberately: `data.is_active`
# already treats blank as inactive, and inventing a value would be worse than an
# empty cell. Both independent active-vocabularies (data._REFUND_TOKENS and
# dashboard_core._REFUND_HINTS) classify these the same way —
# 'Unidentified/Refunded' matches on both 'refund' and 'unidentif'.
PAYMENT = {"full_paid": "Full Paid", "booking": "Booking Amount",
           "partially_paid": "Partially Paid",
           "refunded": "Unidentified/Refunded", "none": ""}

# 'unknown' maps to blank because it genuinely means "the API does not know",
# which is not the same as a closing type of its own. l3_purchased is rare (9 of
# 60,968 sampled) and was leaking through raw into the dashboard before this map.
CLOSING = {"system": "System", "bda_collection": "BDA Collection",
           "bda_closing": "BDA Closing", "l3_purchased": "L3 Purchased",
           "unknown": ""}

# 'Finance - AI Career Accelerator Program B38' -> code B38, POD prefix 'Finance'.
# Anchored on the FULL programme name, never the bare number: batch numbers are
# reused across programme generations. Checked 2026-09-15 — 2025's 'AICA IC B19'
# and 2026's 'AI Career Accelerator Program B19' share ZERO of 1,161 people.
_BATCH_RE = re.compile(r"AI\s*Career\s*Accelerator\s*Program\s*B(\d+)\s*$", re.I)
_POD_RE = re.compile(r"\s*(.*?)\s*[-–]\s*AI\s*Career", re.I)


def batch_code(name) -> str | None:
    """'Finance - AI Career Accelerator Program B38' -> 'B38'. None if not CAP."""
    m = _BATCH_RE.search(str(name or "").strip())
    return f"B{int(m.group(1))}" if m else None


def pod_prefix(name) -> str:
    """The POD part of a batch name, '' for a whole-batch record.

    B35 onward the LMS stores ONE BATCH RECORD PER POD, and the name is
    byte-identical to what the roster's own POD cell holds — which is why the
    cell below gets the full name rather than the canonical POD.
    """
    m = _POD_RE.match(str(name or "").strip())
    return m.group(1).strip() if m else ""


def group_batches(raw_batches) -> dict[str, list[dict]]:
    """Every CAP batch record, grouped by batch code. Non-CAP batches are dropped."""
    out: dict[str, list[dict]] = {}
    for b in raw_batches or ():
        code = batch_code(b.get("name"))
        if code:
            out.setdefault(code, []).append(b)
    return out


def _num(code: str) -> int:
    m = re.search(r"(\d+)", code or "")
    return int(m.group(1)) if m else 0


def live_codes(codes, n: int = 2) -> list[str]:
    """The n highest-numbered batches — the cohorts still taking sessions.

    Everything below them is frozen. Two is the owner's rule: the current cohort
    and the one before it.
    """
    return sorted(codes, key=_num)[-n:] if codes else []


# ── API customers -> roster rows ─────────────────────────────────────────────

def _join(parts) -> str:
    return ", ".join(p for p in parts if p)


def customer_row(c: dict, code: str, pod_name: str) -> list:
    """One API customer -> one roster row, in the column contract above."""
    cc = str(c.get("countryCode") or "").strip()
    num = str(c.get("number") or "").strip()
    mail = str(c.get("email") or "").strip()
    alts = [str(e or "").strip() for e in (c.get("alternativeEmails") or [])]
    row = [""] * N_FIXED
    row[I_CC] = cc
    # Concatenated and kept a STRING. The Sheet's own numbers arrive from
    # openpyxl as floats, which is the trap `_cell_phone` exists to undo; a
    # string here means the marker's last-10 rule sees clean digits.
    row[I_NUM] = f"{cc}{num}" if num else ""
    row[I_MAIL] = mail
    row[I_WACC] = ""                                  # the API has no WhatsApp cc
    row[I_WA] = _join(f"{(p.get('countryCode') or '')}{p.get('number') or ''}"
                      for p in (c.get("alternativePhones") or []))
    row[I_BCAST] = _join(e for e in alts if e and e.lower() != mail.lower())
    row[I_BATCH] = f"AI CAP {code}"
    row[I_AMT] = ""                                   # see module docstring
    row[I_PAY] = PAYMENT.get(str(c.get("paymentStatus") or "").strip(), "")
    row[I_CLOSE] = CLOSING.get(str(c.get("closingType") or "").strip(), "")
    row[I_POD] = pod_name
    return row


def api_rows(code: str, records) -> tuple[list[list], list[str]]:
    """[(batch record, [customer, ...])] -> deduped roster rows + warnings.

    One person can sit in two POD records; the first wins, exactly as the
    parent folder's build_b40.py established.
    """
    rows: list[list] = []
    seen: dict[str, int] = {}
    warnings: list[str] = []
    bad_pods: set[str] = set()
    for rec, customers in records:
        name = str(rec.get("name") or "")
        prefix = pod_prefix(name)
        # The cell holds the FULL batch name, which is the roster's own
        # convention; pods.from_roster_cell strips the programme tail. Whole-batch
        # records (B17-B34, no prefix) get a blank cell rather than a name that
        # would canon to nothing.
        pod_cell = name if prefix else ""
        if prefix and pods.canon(prefix) is None and prefix not in bad_pods:
            bad_pods.add(prefix)
            warnings.append(f"{code}: unrecognised POD {prefix!r} — "
                            f"add it to pods._ALIASES")
        for c in customers or ():
            key = ac._cell_email(c.get("email"))
            if not key:
                p = ac._cell_phone(c.get("number"))
                key = f"p:{p[-10:]}" if len(p) >= 10 else ""
            if key and key in seen:
                continue
            if key:
                seen[key] = len(rows)
            rows.append(customer_row(c, code, pod_cell))
    return rows, warnings


# ── last week's marked workbook -> retained rows ─────────────────────────────

# The identity block is always the first handful of columns; a marked workbook
# has ~570 session columns after it and none of them can contain these needles.
# Capping the search keeps a stray header from ever binding a column index that
# sits out in the session range.
_ID_SCAN = 40


def _find_all(header, *needles) -> list[int]:
    """0-based indexes of every column whose header contains all needles."""
    out = []
    for i, v in enumerate(header[:_ID_SCAN]):
        h = str(v or "").strip().lower()
        if all(nd in h for nd in needles):
            out.append(i)
    return out


def _find(header, *needles) -> int | None:
    got = _find_all(header, *needles)
    return got[0] if got else None


def _val(row, i) -> str:
    """One cell as the roster's own text. Floats are de-pointed FIRST, because
    openpyxl hands phone numbers back as 9.19876543210e+11 and `str()` on that
    loses the number entirely."""
    if i is None or i >= len(row):
        return ""
    v = row[i]
    if v is None:
        return ""
    if isinstance(v, float) and float(v).is_integer():
        return str(int(v))
    return str(v).strip()


def read_previous(xlsx_bytes: bytes, warnings: list | None = None
                  ) -> dict[str, list[list]]:
    """Last week's marked workbook -> {batch code: [roster row, ...]}.

    Identity and attributes only; the session columns are ignored, because
    `carryforward` puts those back. Column positions are located by header text
    on every tab, never assumed: Payment/Close Type are I/J on most tabs but J/K
    on B29 and B20, and the header row is row 1 on some tabs and row 2 on others.

    Two tabs can legitimately resolve to one code under the loose rule above; their
    rows are concatenated and de-duplicated on identity, and a warning is raised
    because a batch quietly gaining a second tab's students would move its
    strength and therefore every percentage on its dashboard.
    """
    from openpyxl import load_workbook

    out: dict[str, list[list]] = {}
    seen_tab: dict[str, str] = {}
    if not xlsx_bytes:
        return out
    # read_only: the marked workbook is ~8 MB with ~570 session columns per tab,
    # and a read-write load parses every one of those ~35M cells into objects.
    # carryforward reads it the same way for the same reason. Streaming rows also
    # means `ws.cell(r, c)` is unavailable, which is why the header is captured
    # from the first rows of the same single pass.
    wb = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    try:
        for name in wb.sheetnames:
            code = _roster_tab_code(name)
            if not code:
                continue
            ws = wb[name]
            header, rows = None, []
            for n_row, raw in enumerate(ws.iter_rows(values_only=True), start=1):
                if header is None:
                    # Same needles and the same 3-row window as
                    # attendance_core._header_row, so a tab either module accepts
                    # is accepted by both.
                    joined = " ".join(str(v or "") for v in raw[:13]).lower()
                    if ("registered number" in joined
                            or "registered mail" in joined):
                        header = raw
                        ccs = _find_all(raw, "code")
                        idx = {
                            I_CC: (ccs[0] if ccs else None),
                            I_NUM: _find(raw, "registered", "number"),
                            I_MAIL: _find(raw, "registered", "mail"),
                            I_WACC: (ccs[1] if len(ccs) > 1 else None),
                            I_WA: _find(raw, "whatsa"),
                            I_BCAST: _find(raw, "broadcast"),
                            I_BATCH: _find(raw, "batch", "name"),
                            I_AMT: _find(raw, "amount"),
                            I_PAY: _find(raw, "payment"),
                            I_CLOSE: _find(raw, "clos"),
                            I_POD: _find(raw, "pod"),
                        }
                        if idx[I_MAIL] is None and idx[I_NUM] is None:
                            break
                    elif n_row >= 3:
                        break                      # not a roster tab
                    continue
                row = [_val(raw, idx[i]) for i in range(N_FIXED)]
                if not row[I_MAIL] and not row[I_NUM]:
                    continue
                if not row[I_BATCH]:
                    row[I_BATCH] = f"AI CAP {code}"
                rows.append(row)
            if not rows:
                continue
            if code in out:
                if warnings is not None:
                    warnings.append(
                        f"{code}: tabs {seen_tab[code]!r} and {name!r} both "
                        f"resolve to {code} — their students were merged")
                out[code] = _dedupe(out[code] + rows)
            else:
                out[code] = rows
                seen_tab[code] = name
    finally:
        wb.close()
    return out


def _dedupe(rows: list[list]) -> list[list]:
    """First occurrence of each identity wins — the marker's own precedence."""
    out, seen = [], set()
    for r in rows:
        e, p = _identity(r)
        k = e or (f"p:{p}" if p else "")
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        out.append(r)
    return out


# Two different rules, deliberately, and getting them the wrong way round loses
# students:
#
#   READING last week's workbook uses `attendance_core._sheet_key` — the LOOSEST
#   rule in the codebase, and the one `carryforward` itself uses. A tab named
#   `AI CAP B37 8PM` is a real possibility; carryforward would carry its marks
#   onto our `AI CAP B37` (same key), so if we did NOT retain its students they
#   would arrive as `unmatched_prev` and lose everything. Retention has to be at
#   least as generous as the thing it is protecting against.
#
#   WRITING uses the strict `AI CAP B<n>` form, because `data.py:_BATCH_RE`
#   full-matches exactly that and a tab it rejects produces marks that silently
#   never reach the dashboard. We choose the names here, so we choose clean ones.
def _roster_tab_code(sheet_name: str) -> str | None:
    name = str(sheet_name or "")
    # `_track_named`, not `_track`: the latter DEFAULTS to CAP for a name that
    # mentions no programme at all, so a stray `Sheet1` would come back as batch
    # B1 (the digit is enough for `_sheet_key`) and appear on the dashboard as a
    # cohort that does not exist. Requiring the name to actually say "CAP" keeps
    # `AI CAP B37 8PM` and rejects the scratch tabs people leave behind.
    if ac._track_named(name) != "CAP":
        return None
    key = ac._sheet_key(name)
    return f"B{key[1]}" if key else None


# ── merge ────────────────────────────────────────────────────────────────────

def _identity(row) -> tuple[str, str]:
    """(email, last-10 phone) using the MARKER's own helpers, so a person matched
    here is matched identically by attendance_core and carryforward."""
    e = ac._cell_email(row[I_MAIL])
    p = ac._cell_phone(row[I_NUM])
    return e, (p[-10:] if len(p) >= 10 else "")


def _apply(dst: list, src: list) -> None:
    """Fold a fresh API row onto a retained one, in place.

    Identity columns are never touched — the retained spelling is what the marked
    workbook's columns are already keyed to. Attributes take the API value only
    when it says something: a blank closing type means "the API does not know",
    and overwriting a real `BDA Closing` with it is exactly the regression the
    module docstring describes.
    """
    dst[I_PAY] = src[I_PAY]                      # payment IS the live truth
    for i in (I_CLOSE, I_POD, I_WA, I_BCAST, I_CC):
        if src[i]:
            dst[i] = src[i]
    if src[I_BATCH]:
        dst[I_BATCH] = src[I_BATCH]
    # I_AMT and I_NUM/I_MAIL deliberately left alone: the API has no amount, and
    # changing a retained identity would orphan that person's carried marks.


def merge(retained: list[list], fresh: list[list]) -> tuple[list[list], dict]:
    """Layers 1-3. Retained rows keep their order; API-only people are appended."""
    out = [list(r) for r in retained]
    by_email: dict[str, int] = {}
    by_p10: dict[str, int] = {}
    for i, row in enumerate(out):
        e, p = _identity(row)
        if e:
            by_email.setdefault(e, i)
        if p:
            by_p10.setdefault(p, i)

    added = updated = 0
    for fr in fresh:
        e, p = _identity(fr)
        i = by_email.get(e) if e else None
        if i is None and p:
            i = by_p10.get(p)
        if i is None:
            out.append(list(fr))
            added += 1
            # Index the newcomer too, so the same person arriving again from a
            # second POD record updates rather than duplicating.
            if e:
                by_email.setdefault(e, len(out) - 1)
            if p:
                by_p10.setdefault(p, len(out) - 1)
        else:
            _apply(out[i], fr)
            updated += 1
    return out, {"retained": len(retained), "added": added, "updated": updated,
                 "total": len(out)}


# ── the workbook ─────────────────────────────────────────────────────────────

def write_workbook(by_code: dict[str, list[list]]) -> bytes:
    """{batch code: rows} -> xlsx bytes shaped exactly like the Sheet's export."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for code in sorted(by_code, key=_num):
        rows = by_code[code]
        # The POD column only exists from B35 on, and emitting an empty one for
        # B17-B34 would add a blank pseudo-session column to those batches' grids.
        # Mirror what the Sheet actually does instead.
        width = N_FIXED if any(r[I_POD] for r in rows) else N_FIXED - 1
        ws = wb.create_sheet(f"AI CAP {code}")
        ws.append(HEADERS[:width])
        for r in rows:
            ws.append(r[:width])
        # Phones must never be read back as floats. openpyxl already stores them
        # as strings, but the explicit text format stops Excel helpfully
        # converting them if a human opens and re-saves the file.
        for c in (I_CC + 1, I_NUM + 1, I_WACC + 1, I_WA + 1):
            for cell in ws.iter_cols(min_col=c, max_col=c, min_row=2):
                for one in cell:
                    one.number_format = "@"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build(prev_marked_bytes: bytes | None, api_by_code: dict[str, list],
          live: list[str] | None = None, log=print) -> tuple[bytes, dict]:
    """Convenience wrapper: read last week's workbook, then build.

    The pipeline calls `read_previous` itself and then `build_from`, because it
    needs the retained batch codes to plan which batches to fetch and parsing an
    8 MB workbook twice is a minute it does not need to spend.
    """
    warns: list[str] = []
    retained = read_previous(prev_marked_bytes, warns)
    data, report = build_from(retained, api_by_code, live, log)
    report["warnings"] = warns + list(report.get("warnings") or ())
    return data, report


def build_from(retained: dict[str, list[list]], api_by_code: dict[str, list],
               live: list[str] | None = None, log=print) -> tuple[bytes, dict]:
    """The whole thing: retained + refresh/seed -> roster workbook bytes.

    `api_by_code` maps a batch code to [(batch record, [customer, ...])] and only
    needs to contain the batches actually being refreshed or seeded — see
    `plan_fetch`.
    """
    live_set = set(live or ())
    codes = sorted(set(retained) | set(api_by_code), key=_num)

    by_code: dict[str, list[list]] = {}
    report = {"batches": {}, "warnings": [], "frozen": [], "refreshed": [],
              "seeded": []}
    for code in codes:
        ret = retained.get(code, [])
        refresh = code in live_set or not ret
        fresh: list[list] = []
        if refresh and code in api_by_code:
            fresh, warns = api_rows(code, api_by_code[code])
            report["warnings"].extend(warns)
        rows, st = merge(ret, fresh)
        if not rows:
            continue
        by_code[code] = rows
        report["batches"][code] = st
        if refresh and not fresh and ret:
            # Asked for a refresh, got no API rows: the batch is really frozen.
            # Saying "refreshed" here would hide a live cohort running on last
            # week's enrolment.
            report["frozen"].append(code)
            report["warnings"].append(
                f"{code}: wanted a refresh but the API returned no rows — "
                f"left on last week's roster")
        else:
            (report["seeded"] if not ret else
             report["refreshed"] if refresh else report["frozen"]).append(code)

    data = write_workbook(by_code)
    report["tabs"] = len(by_code)
    report["rows"] = sum(len(v) for v in by_code.values())
    report["bytes"] = len(data)
    return data, report


FIRST_BATCH = 17          # the oldest cohort the dashboard has ever tracked


def plan_fetch(api_codes, retained_codes, live: list[str],
               first_batch: int = FIRST_BATCH, missing: list | None = None) -> list[str]:
    """Which batches actually need an API call this run.

    The live cohorts, plus genuinely NEW cohorts. Everything else is frozen and
    costs nothing — the point of the whole refresh policy, since a full sweep is
    ~95 batch records against an endpoint that 504s under load.

    "New" means numerically ABOVE everything we already hold, because that is the
    only direction cohorts arrive from. Without that floor the LMS's own history
    counts as new: it carries CAP batches back to B1, none of which the roster has
    ever tracked, so a first run would have seeded SIXTEEN extra tabs of students
    with no attendance data and put sixteen empty batches on the dashboard.

    `first_batch` only applies to a bootstrap run with nothing retained at all;
    after that the retained set defines the floor by itself.
    """
    missing = [] if missing is None else missing
    have = set(retained_codes)
    floor = max((_num(c) for c in have), default=first_batch - 1)
    fresh = {c for c in api_codes if _num(c) > floor}
    want = (set(live) | fresh) & set(api_codes)
    # A live cohort the API listing does not carry is the dangerous case: it is
    # the NEWEST batch, the one whose enrolment is still moving, and dropping it
    # here silently leaves it frozen on last week's roster while `build_from`
    # still reports it as refreshed. Hand the caller the misses to warn about.
    missing[:] = sorted(set(live) - set(api_codes), key=_num)
    return sorted(want, key=_num)


def summary(report: dict) -> str:
    return (f"{report.get('tabs', 0)} tab(s), {report.get('rows', 0):,} row(s), "
            f"{len(report.get('refreshed') or ())} refreshed, "
            f"{len(report.get('seeded') or ())} seeded, "
            f"{len(report.get('frozen') or ())} frozen")
