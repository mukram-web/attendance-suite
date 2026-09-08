"""
Be10X Attendance Dashboard — compute engine.

Reads a Master Batch Roster (.xlsx) and returns a tidy attendance table.
The roster already holds Present/Absent per session column and a Payment
column we use to decide who is an "active" student.

Public API:
    compute(roster_bytes: bytes) -> pandas.DataFrame
    batch_key(name: str) -> int          # for natural batch sorting

Returned DataFrame columns:
    Batch, SessionIdx, SessionLabel, Topic,
    PresentActive, Active, PresentAll, Total,
    PctActive, PctAll, HasData
"""
from __future__ import annotations
import io
import re
import openpyxl
import pandas as pd

# Payment values that mean the student is NOT active (refunded / unidentified / blank)
_REFUND_HINTS = ("refund", "undifined", "unidentif", "undefined")


def _is_active(payment) -> bool:
    if payment is None:
        return False
    p = str(payment).strip().lower()
    if p == "":
        return False
    return not any(h in p for h in _REFUND_HINTS)


def batch_key(name: str) -> int:
    """Natural sort key: 'B17' -> 17, 'AI CAP B7' -> 7."""
    m = re.sub(r"\D", "", str(name))
    return int(m) if m else 0


# How wide these readers look. Kept as named constants because they are a real
# limit, not a tuning knob: a session column beyond _MAX_SCAN_COL is invisible to
# compute() and roster_grid(), so it silently stops being counted. The widest
# roster tab measured 2026-09-08 was 43 columns (AI CAP B17), but pod-era batches
# gain one column per pod per week, so the headroom is finite — _wide_tab_warning
# below says so before it bites rather than after.
_MAX_SCAN_COL = 80
_MAX_HEADER_COL = 20


class _Tab:
    """One sheet, read the same way whether it came from a workbook or from rows
    already in memory.

    build_store already materialises the whole marked workbook into `tabs` to
    build DATA, and then used to hand the raw bytes to compute() and
    roster_grid(), each of which re-streamed the same 80k rows. Measured on the
    real 7.9 MB marked workbook: 46.7s to materialise, then 43.7s + 46.8s to do
    it twice more. This class lets both readers take the rows that are already
    in hand.

    The slicing mirrors openpyxl's max_col arguments EXACTLY. That matters: the
    bytes path pads short rows out to max_col with None, the rows path does not,
    and the session-column loop runs to len(row1). Padding-only differences are
    invisible (the loop skips empty labels) but the widths must not diverge in
    the other direction.
    """

    __slots__ = ("_ws", "_rows")

    def __init__(self, ws=None, rows=None):
        self._ws, self._rows = ws, rows

    def head(self, n: int, width: int) -> list:
        """First `n` rows, `width` columns wide."""
        if self._rows is not None:
            return [tuple(r[:width]) for r in self._rows[:n]]
        return list(self._ws.iter_rows(min_row=1, max_row=n, max_col=width,
                                       values_only=True))

    def body(self, first_row: int, width: int) -> list:
        """Every row from `first_row` (1-based) down, `width` columns wide."""
        if self._rows is not None:
            return [tuple(r[:width]) for r in self._rows[first_row - 1:]]
        return list(self._ws.iter_rows(min_row=first_row, max_col=width,
                                       values_only=True))


def _find_header_row(tab):
    """Return (row_number, lowercased_values) for the row that holds field names.

    An EMPTY sheet yields no rows at all — people add scratch/pivot tabs to the
    roster (e.g. "Pivot Table 2"), and an unguarded next() raises StopIteration
    that kills the whole build. Missing rows just mean "not a roster sheet"."""
    top = tab.head(3, _MAX_HEADER_COL)
    for r in range(1, 4):
        if r > len(top):
            break
        vals = [str(v).strip().lower() if v is not None else "" for v in top[r - 1]]
        if "registered number" in vals or "payment" in vals:
            return r, vals
    return 1, []


def _col_idx(vals, name):
    for i, v in enumerate(vals):
        if v == name:
            return i
    return None


def _looks_like_roster(sheet_name: str) -> bool:
    s = sheet_name.lower()
    if "att" in s:          # skip Zoom "...Att" helper sheets
        return False
    return True


def _wide_tab_warning(sheet: str, width: int) -> str | None:
    """A tab approaching _MAX_SCAN_COL is about to lose columns silently."""
    if width >= _MAX_SCAN_COL - 8:
        return (f"{sheet}: {width} columns, at or near the {_MAX_SCAN_COL}-column "
                "read limit — session columns beyond it stop being counted")
    return None


def compute(roster_bytes: bytes, tabs: dict | None = None) -> pd.DataFrame:
    """Per batch × session attendance.

    `tabs` is {sheet name: [row tuples]} as build_store already materialises it.
    Pass it to skip re-streaming the workbook — the numbers are identical either
    way (pinned by tests/test_dashboard_core_tabs.py); it is the same rows read
    from memory instead of from the zip a second time.
    """
    wb = None
    if tabs is None:
        wb = openpyxl.load_workbook(io.BytesIO(roster_bytes), read_only=True,
                                    data_only=True)
        names = wb.sheetnames
    else:
        names = list(tabs)
    records = []

    for sh in names:
        if not _looks_like_roster(sh):
            continue
        tab = _Tab(rows=tabs[sh]) if tabs is not None else _Tab(ws=wb[sh])

        top = tab.head(2, _MAX_SCAN_COL)
        row1 = top[0] if len(top) > 0 else ()
        row2 = top[1] if len(top) > 1 else ()

        H, hvals = _find_header_row(tab)
        pay_i = _col_idx(hvals, "payment")
        close_i = _col_idx(hvals, "closing type")
        if close_i is None:
            close_i = (pay_i + 1) if pay_i is not None else 9

        # Session columns live to the right of "Closing Type".
        # The date label for each session is always in row 1 at that column.
        sess_cols = []
        for c in range(close_i + 1, len(row1)):
            label = row1[c] if c < len(row1) else None
            if label is None or str(label).strip() == "":
                continue
            topic = ""
            if H == 2 and c < len(row2) and row2[c] is not None:
                topic = str(row2[c]).strip()
            sess_cols.append((c, str(label).strip(), topic))

        if not sess_cols:
            continue

        last_col = max([c for c, _, _ in sess_cols] + [pay_i or 0, close_i]) + 1
        data = tab.body(H + 1, last_col)

        active = 0
        total = 0
        p_active = [0] * len(sess_cols)
        p_all = [0] * len(sess_cols)

        for row in data:
            if all((x is None or str(x).strip() == "") for x in row[:8]):
                continue
            total += 1
            act = _is_active(row[pay_i]) if (pay_i is not None and pay_i < len(row)) else False
            if act:
                active += 1
            for k, (c, _lbl, _t) in enumerate(sess_cols):
                v = row[c] if c < len(row) else None
                if v is not None and str(v).strip().lower() == "present":
                    p_all[k] += 1
                    if act:
                        p_active[k] += 1

        bn = sh.replace("AI CAP", "").replace("AICAP", "").strip()
        for k, (c, lbl, topic) in enumerate(sess_cols):
            has_data = p_all[k] > 0
            records.append(
                dict(
                    Batch=bn,
                    SessionIdx=k + 1,
                    SessionLabel=lbl,
                    Topic=topic,
                    PresentActive=p_active[k],
                    Active=active,
                    PresentAll=p_all[k],
                    Total=total,
                    PctActive=round(p_active[k] / active * 100, 1) if active else 0.0,
                    PctAll=round(p_all[k] / total * 100, 1) if total else 0.0,
                    HasData=has_data,
                )
            )

    if wb is not None:
        wb.close()
    df = pd.DataFrame.from_records(records)
    if not df.empty:
        df = df.sort_values(by=["Batch", "SessionIdx"], key=lambda s: s.map(batch_key) if s.name == "Batch" else s)
        df = df.reset_index(drop=True)
    return df


def _clean_batch_name(sheet_name: str) -> str:
    return sheet_name.replace("AI CAP", "").replace("AICAP", "").strip()


def batch_sheet_map(roster_bytes: bytes) -> dict:
    """Map cleaned batch name (e.g. 'B17') -> actual sheet name (e.g. 'AI CAP B17')."""
    wb = openpyxl.load_workbook(io.BytesIO(roster_bytes), read_only=True)
    out = {}
    for sh in wb.sheetnames:
        if _looks_like_roster(sh):
            out.setdefault(_clean_batch_name(sh), sh)
    wb.close()
    return out


def roster_grid(roster_bytes: bytes, sheet_name: str,
                tabs: dict | None = None) -> pd.DataFrame:
    """Per-student attendance grid for ONE batch sheet — the spreadsheet view.

    Columns: Email, Phone, Active, Present (count), then one column per session
    labelled by its date, holding 'Present' / 'Absent' / '' exactly as marked.
    Contact columns are raw here; the UI masks them for privacy.

    `tabs` works exactly as it does in compute() — see there.
    """
    wb = None
    if tabs is None:
        wb = openpyxl.load_workbook(io.BytesIO(roster_bytes), read_only=True,
                                    data_only=True)
        tab = _Tab(ws=wb[sheet_name])
    else:
        tab = _Tab(rows=tabs[sheet_name])

    top = tab.head(2, _MAX_SCAN_COL)
    row1 = top[0] if len(top) > 0 else ()

    H, hvals = _find_header_row(tab)
    pay_i = _col_idx(hvals, "payment")
    close_i = _col_idx(hvals, "closing type")
    if close_i is None:
        close_i = (pay_i + 1) if pay_i is not None else 9
    mail_i = _col_idx(hvals, "registered mail")
    num_i = _col_idx(hvals, "registered number")

    sess_cols = []
    for c in range(close_i + 1, len(row1)):
        label = row1[c] if c < len(row1) else None
        if label is None or str(label).strip() == "":
            continue
        sess_cols.append((c, str(label).strip()))

    last_col = max([c for c, _ in sess_cols] + [pay_i or 0, close_i, mail_i or 0, num_i or 0]) + 1
    data = tab.body(H + 1, last_col)
    if wb is not None:
        wb.close()

    records = []
    for row in data:
        if all((x is None or str(x).strip() == "") for x in row[:8]):
            continue
        email = row[mail_i] if (mail_i is not None and mail_i < len(row)) else None
        phone = row[num_i] if (num_i is not None and num_i < len(row)) else None
        active = _is_active(row[pay_i]) if (pay_i is not None and pay_i < len(row)) else False
        rec = {
            "Email": "" if email is None else str(email).strip(),
            "Phone": "" if phone is None else str(phone).strip().replace(".0", ""),
            "Active": active,
        }
        present = 0
        for c, label in sess_cols:
            v = row[c] if c < len(row) else None
            mark = "" if v is None else str(v).strip().title()
            rec[label] = mark
            if mark.lower() == "present":
                present += 1
        rec["Present"] = present
        records.append(rec)

    cols = ["Email", "Phone", "Active", "Present"] + [lbl for _, lbl in sess_cols]
    return pd.DataFrame.from_records(records, columns=cols)
