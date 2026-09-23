"""ECAP — a second programme, tracked from a SNAPSHOT of its roster.

ECAP ("AI Engineering Career Accelerator Program") is a different programme
from AI CAP that shares this pipeline's L2 schedule and its attendee Shared
Drives. Most of the stack already knew about it: `attendance_core._sheet_key`
keys an `AI ECAP B1` tab to `('ECAP', 1)`, `extract_batches` reads ECAP out of
an L2 label, and 47 of L2's webinars are ECAP sessions with real webinar ids.
What was missing was the roster and the dashboard layer.

**The roster is a snapshot, by the owner's choice (2026-09-23).** CAP's roster
is rebuilt from the LMS API every week; ECAP's is one workbook exported on
23 Sep, and `ECAP_ROSTER_ID` points at it. The consequence is not subtle and is
not a bug: anyone who enrols or refunds after that date never appears, so
ECAP's denominators go stale from the day it shipped. The fix, when it is
wanted, is to teach `tools/make_lms_sheet.py` to write `AI ECAP B<n>` tabs into
the main roster sheet — then this module's only job disappears.

**Tabs are grafted, never overwritten.** `graft` skips a batch already present
in the base workbook, which is what makes it safe under `--incremental`: after
the first run the base IS last week's marked workbook, ECAP tabs and all, and
re-copying the pristine snapshot over it would wipe every mark the freeze is
supposed to protect.

**Its B1 is not CAP's B1.** ECAP B1 is 206 people; CAP B1 is 3,985. They are
different cohorts of different programmes that happen to share a number, which
is why the dashboard carries the programme in the batch code (`ECAP B1`) rather
than the bare `B1` CAP uses. `dashboard_core.batch_key` sorts ECAP after every
CAP batch instead of interleaving the two numberings.
"""
import io
import re

from openpyxl import load_workbook

# 'AI ECAP B1', 'AI E-CAP B2', 'ECAP B3'. Anchored: a tab merely MENTIONING
# ECAP is not a batch tab.
TAB_RE = re.compile(r"\s*(AI\s*)?E\s*-?\s*CAP\s*B\d+\s*", re.I)


def is_batch_tab(name) -> bool:
    return bool(TAB_RE.fullmatch(str(name or "")))


def graft(base_bytes: bytes, extra_bytes: bytes) -> tuple[bytes, dict]:
    """Copy the ECAP batch tabs out of `extra_bytes` into `base_bytes`.

    Returns `(xlsx_bytes, report)`. A batch already in the base is SKIPPED, not
    replaced — see the module docstring: under `--incremental` the base carries
    last week's marks and overwriting it would destroy them.

    Reported rather than silent, because "ECAP is missing from the dashboard"
    and "ECAP was grafted but every tab was skipped" look identical from the
    outside and need different fixes.
    """
    import attendance_core as ac

    report = {"added": [], "skipped": [], "rows": 0, "warnings": []}
    if not extra_bytes:
        return base_bytes, report

    wb = load_workbook(io.BytesIO(base_bytes), data_only=True)
    ex = load_workbook(io.BytesIO(extra_bytes), read_only=True, data_only=True)
    try:
        have = {ac._sheet_key(n) for n in wb.sheetnames}
        for name in ex.sheetnames:
            if not is_batch_tab(name):
                continue                      # 'Read me' and anything else
            key = ac._sheet_key(name)
            if key in have:
                report["skipped"].append(name)
                continue
            ws = wb.create_sheet(name)
            n = 0
            for row in ex[name].iter_rows(values_only=True):
                # openpyxl's read-only max_row overshoots on some exports, so a
                # trailing run of empty rows would become phantom students with
                # neither email nor phone.
                if not any(v not in (None, "") for v in row):
                    continue
                ws.append(list(row))
                n += 1
            report["added"].append(name)
            report["rows"] += max(0, n - 1)   # minus the header
            have.add(key)
        if not report["added"] and not report["skipped"]:
            report["warnings"].append(
                "the ECAP workbook held no 'AI ECAP B<n>' tab — nothing grafted")
    finally:
        ex.close()

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), report
