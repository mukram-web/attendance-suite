"""Does an incremental run produce the same marks as a full one?

The load-bearing check for the freeze design, on the real corpus:

  full        = process_files(pristine roster, ALL attendee files)
  incremental = process_files(merge_marks(roster, marked-without-this-week),
                              only this week's files)

Every session column in both workbooks must agree cell for cell. Any drift is
the carry-forward losing or inventing a mark, and with no weekly rebuild there
would be nothing to correct it.

Also proves the fetch side: handed the merged workbook, fetch_new_attendees must
queue ONLY the sessions that are not carried.

NOT a unit test — it needs Drive and takes about five minutes, which is why the
name does not start with `test_` and unittest never collects it. Run it after
touching `carryforward`, `attendance_core.session_key`, `pods.from_folder` or
the fetch's skip rule; CLAUDE.md §4g records the numbers to compare against.

    .venv\\Scripts\\python.exe tests\\equivalence_check.py [YYYY_MM_DD]

The argument is the week to treat as "new" (default 2026_09_06).
"""
import io
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

from openpyxl import load_workbook           # noqa: E402

import attendance_core as ac                 # noqa: E402
import carryforward as cf                    # noqa: E402
import live_data                             # noqa: E402
import pipeline                              # noqa: E402

CUT = sys.argv[1] if len(sys.argv) > 1 else "2026_09_06"    # "this week"


def t(msg, t0):
    print(f"   [{time.time() - t0:5.1f}s] {msg}", flush=True)


def session_cols(b, label):
    """{(sheet, header): {student_email: value}} for every session column."""
    wb = load_workbook(io.BytesIO(b), read_only=True, data_only=True)
    out = {}
    for name in wb.sheetnames:
        if not ac._sheet_key(name):
            continue
        rows = [tuple(r) for r in wb[name].iter_rows(values_only=True)]
        if not rows:
            continue
        hr = cf._header_row_of(rows)
        cols = cf._sheet_session_cols(rows[0], rows[1] if len(rows) > 1 else None, hr)
        rm = cf._col_of(rows[hr - 1], "registered", "mail")
        if not rm:
            continue
        for key, (ci, header, _topic) in cols.items():
            cell = {}
            for row in rows[hr:]:
                e = ac._cell_email(row[rm - 1]) if rm - 1 < len(row) else ""
                v = row[ci - 1] if ci - 1 < len(row) else None
                if e:
                    cell[e] = str(v).strip() if v is not None else ""
            out[(name, header)] = cell
    wb.close()
    print(f"   {label}: {len(out)} session column(s)")
    return out


def main():
    t0 = time.time()
    cfg = pipeline.load_config()
    live_data.set_service_account(cfg["sa_info"], scopes=pipeline.RW_SCOPES)
    svc = live_data._drive_service()

    roster, stamp = live_data.fetch_sheet_cached(svc, cfg["roster_id"])
    l2, _ = live_data.fetch_sheet_cached(svc, cfg["l2_id"])
    t(f"roster {len(roster):,} bytes (modified {stamp})", t0)

    all_files, info = live_data.fetch_new_attendees(
        svc, cfg["attendee_folder_id"], roster)
    t(f"{len(all_files)} attendee file(s) from {info['new_folders']} folder(s)", t0)

    this_week = [(n, b) for n, b in all_files if CUT in n.split("/")[-1]]
    older = [(n, b) for n, b in all_files if CUT not in n.split("/")[-1]]
    print(f"   split on {CUT}: {len(older)} older + {len(this_week)} this week")
    if not this_week:
        sys.exit(f"no files for {CUT} — pass another date as argv[1]")

    marked_full, rep_full, warn_full = ac.process_files(
        roster, l2, all_files, values_only=True)
    t(f"FULL: {len(rep_full)} column(s), {len(warn_full)} warning(s)", t0)

    marked_prev, rep_prev, _ = ac.process_files(
        roster, l2, older, values_only=True)
    t(f"previous week's workbook: {len(rep_prev)} column(s)", t0)

    base, carry = cf.merge_marks(roster, marked_prev)
    t("merge: " + cf.summary(carry), t0)

    # the fetch side, on real folder names. Only the CARRIED columns may
    # suppress a download - exactly what the pipeline passes.
    queued, info2 = live_data.fetch_new_attendees(
        svc, cfg["attendee_folder_id"], base)
    qnames = {n.split("/")[-1] for n, _ in queued}
    expect = {n.split("/")[-1] for n, _ in this_week}
    t(f"fetch with the merged workbook queued {len(queued)} file(s) "
      f"(skipped {info2['skipped_already_marked']} folder(s) already marked)", t0)
    extra = qnames - expect
    missing = expect - qnames
    print(f"   queued-but-not-this-week: {len(extra)}   this-week-but-not-queued: "
          f"{len(missing)}")
    if missing:
        print("   !! MISSING: " + ", ".join(sorted(missing)[:5]))

    # Mark what the FETCH returned, which is what pipeline.py does - not just
    # this week's files. The two differ on purpose: the legacy Sheet-supplied
    # columns are re-marked rather than frozen.
    marked_inc, rep_inc, warn_inc = ac.process_files(
        base, l2, queued, values_only=True)
    t(f"INCREMENTAL: {len(rep_inc)} column(s) marked this run", t0)

    a = session_cols(marked_full, "full")
    b = session_cols(marked_inc, "incremental")

    _k = lambda k: (str(k[0]), str(k[1]))
    only_full = sorted(set(a) - set(b), key=_k)
    only_inc = sorted(set(b) - set(a), key=_k)
    diffs = defaultdict(list)
    for k in sorted(set(a) & set(b), key=_k):
        va, vb = a[k], b[k]
        for e in set(va) | set(vb):
            if va.get(e, "") != vb.get(e, ""):
                diffs[k].append((e, va.get(e, ""), vb.get(e, "")))

    print("\n=== RESULT ===")
    print(f"columns in both: {len(set(a) & set(b))}")
    print(f"only in FULL: {len(only_full)}")
    for k in only_full[:8]:
        print(f"   - {k}")
    print(f"only in INCREMENTAL: {len(only_inc)}")
    for k in only_inc[:8]:
        print(f"   - {k}")
    print(f"columns whose cells differ: {len(diffs)}")
    for k, ds in list(diffs.items())[:8]:
        kinds = defaultdict(int)
        for _e, x, y in ds:
            kinds[f"{x or 'blank'} -> {y or 'blank'}"] += 1
        print(f"   - {k}: {len(ds)} cell(s) {dict(kinds)}")
    ok = not only_full and not only_inc and not diffs and not missing
    print("\nEQUIVALENT" if ok else "\nNOT EQUIVALENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
