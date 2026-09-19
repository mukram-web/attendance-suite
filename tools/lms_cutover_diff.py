"""Would switching the roster to the LMS API change any number? Answer before switching.

This is the decision gate in the migration plan, and it is deliberately NOT named
`test_*`: it needs Drive, needs the LMS API, and takes minutes — the same reason
`tests/equivalence_check.py` sits outside the test run.

It builds the roster BOTH ways for the same week and diffs them per batch:

    rows            how many students each source produces
    lost / gained   people in one and not the other, matched on the MARKER's own
                    identity rule (email, else last-10 phone) so the answer means
                    what carryforward will actually do
    active          the dashboard denominator — this is the number that moves
                    every percentage on the site if it shifts
    closing         the closing-type mix, because the API collapses BDA Closing
                    and Old Customer into `unknown`

A `lost` count above zero is the one that matters: those students' marks would be
dropped by `carryforward` as `unmatched_prev`, and GATE 6 would refuse to publish.
The three-layer builder is designed to keep it at zero, so anything else is a bug
in the retention layer, not an acceptable cost.

Run:
    .venv/Scripts/python.exe tools/lms_cutover_diff.py --out cutover_diff.xlsx
    .venv/Scripts/python.exe tools/lms_cutover_diff.py --live B40,B41 --cache .lms_cache
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import pandas as pd                                     # noqa: E402

import attendance_core as ac                            # noqa: E402
import data as ddata                                    # noqa: E402
import lms_client                                       # noqa: E402
import lms_roster as lr                                 # noqa: E402
import live_data                                        # noqa: E402
import pipeline                                         # noqa: E402


def identity_set(rows) -> set:
    """One key per student, preferring email — the marker's own precedence."""
    out = set()
    for r in rows:
        e, p = lr._identity(r)
        out.add(e or (f"p:{p}" if p else f"row:{len(out)}"))
    return out


def profile(rows) -> dict:
    active = sum(1 for r in rows if ddata.is_active(r[lr.I_PAY]))
    closing: dict[str, int] = {}
    for r in rows:
        k = ddata.normalize_closing(r[lr.I_CLOSE])
        closing[k] = closing.get(k, 0) + 1
    return {"rows": len(rows), "active": active, "closing": closing}


def top_closing(d: dict, n: int = 4) -> str:
    tot = sum(d.values()) or 1
    return " · ".join(f"{k} {100 * v / tot:.0f}%"
                      for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="cutover_diff.xlsx",
                    help="where to write the per-batch workbook")
    ap.add_argument("--live", default="",
                    help="comma-separated batch codes to refresh from the API "
                         "(default: the two highest, as the pipeline does)")
    ap.add_argument("--cache", default="",
                    help="directory for cached /customers payloads — makes a "
                         "second run instant while iterating")
    ap.add_argument("--all", action="store_true",
                    help="refresh EVERY CAP batch from the API, not just the "
                         "live ones. Slow (~95 records) and 504-prone, but it "
                         "is the only way to see the full-cutover diff.")
    a = ap.parse_args()

    cfg = pipeline.load_config()
    live_data.set_service_account(cfg["sa_info"], scopes=pipeline.RW_SCOPES)
    svc = live_data._drive_service()

    print("[1] last week's marked workbook …", flush=True)
    prev = pipeline.fetch_prev_marked(svc, cfg, "nothing to compare against")
    if not prev:
        sys.exit("No marked workbook in the store folder — nothing to diff.")
    retained = lr.read_previous(prev)
    print(f"    {len(retained)} batch tab(s), "
          f"{sum(len(v) for v in retained.values()):,} student(s)")

    print("[2] LMS batches …", flush=True)
    key = lms_client.api_key(cfg.get("lms_api_key", ""))
    grouped = lr.group_batches(lms_client.fetch_batches(key))
    codes = sorted(set(grouped) | set(retained), key=lr._num)
    live = ([b.strip().upper() for b in a.live.split(",") if b.strip()]
            or (sorted(grouped, key=lr._num) if a.all else lr.live_codes(codes)))
    want = lr.plan_fetch(list(grouped), list(retained), live)
    print(f"    {len(grouped)} CAP batch(es); fetching {len(want)}", flush=True)

    api_by_code: dict[str, list] = {}
    for i, code in enumerate(want, 1):
        recs = []
        for b in grouped.get(code, []):
            recs.append((b, lms_client.cached_customers(key, b["id"],
                                                        a.cache or None)))
        api_by_code[code] = recs
        print(f"    [{i}/{len(want)}] {code}: "
              f"{sum(len(c) for _b, c in recs):,} customer(s)", flush=True)

    print("[3] building …", flush=True)
    _bytes, report = lr.build_from(retained, api_by_code, live)

    # Rebuild the per-batch rows the same way build_from did, so the diff
    # describes exactly what would be published.
    rows_by_code = {}
    for code in codes:
        ret = retained.get(code, [])
        fresh = (lr.api_rows(code, api_by_code[code])[0]
                 if (code in set(live) or not ret) and code in api_by_code else [])
        merged, _st = lr.merge(ret, fresh)
        if merged:
            rows_by_code[code] = merged

    out = []
    for code in codes:
        before = retained.get(code, [])
        after = rows_by_code.get(code, [])
        if not before and not after:
            # The LMS's own back catalogue (CAP batches down to B1) that the
            # roster has never tracked and `plan_fetch` deliberately skips.
            continue
        b_ids, a_ids = identity_set(before), identity_set(after)
        pb, pa = profile(before), profile(after)
        out.append({
            "Batch": code,
            "Mode": ("seeded" if not before else
                     "refreshed" if code in set(live) else "frozen"),
            "Rows before": pb["rows"], "Rows after": pa["rows"],
            "Lost": len(b_ids - a_ids), "Gained": len(a_ids - b_ids),
            "Active before": pb["active"], "Active after": pa["active"],
            "Active delta": pa["active"] - pb["active"],
            "Closing before": top_closing(pb["closing"]),
            "Closing after": top_closing(pa["closing"]),
        })

    df = pd.DataFrame(out)
    total_lost = int(df["Lost"].sum())
    with pd.ExcelWriter(a.out, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Per batch", index=False)
        pd.DataFrame([{
            "Batches": len(df), "Rows before": int(df["Rows before"].sum()),
            "Rows after": int(df["Rows after"].sum()), "Lost": total_lost,
            "Gained": int(df["Gained"].sum()),
            "Active before": int(df["Active before"].sum()),
            "Active after": int(df["Active after"].sum()),
        }]).to_excel(w, sheet_name="Total", index=False)

    print()
    print(df.to_string(index=False))
    print()
    print(f"wrote {a.out}")
    if total_lost:
        print(f"\n!! {total_lost} student(s) would LOSE their marks. "
              f"carryforward would count them unmatched_prev and GATE 6 would "
              f"refuse to publish. Do not switch until this is zero.")
    else:
        print("\nOK — no student loses their marks. GATE 6 has nothing to catch.")


if __name__ == "__main__":
    main()
