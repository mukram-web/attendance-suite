"""Freeze everything the roster Sheet knows that the LMS API cannot tell us.

WHY
---
The Sheet is being replaced by the LMS API (CLAUDE.md §4h). Two things the API
simply does not carry:

  * **Closing Type.** The API returns only unknown / bda_collection / system /
    l3_purchased. It has no **BDA Closing** and no **Old Customer**, and folds
    both into `unknown`. Measured: Sheet-sourced B39 reads BDA Collection 39%,
    System 28%, Old Customer 27%, BDA Closing 6%, Unknown 0%; API-sourced B40
    reads Unknown 58%. Without this extract the closing-types panel dies.
  * **Amount.** `transactionLedger` is a learner's LIFETIME history across every
    product, not this batch's deal value.

Both are frozen facts about people already enrolled, so they only have to be
extracted ONCE. New students after the cutover get whatever the API says.

WHAT IT WRITES
--------------
A gzipped JSON keyed by the MARKER's own identity (email, else last-10 phone), so
a row here matches a person exactly the way `attendance_core` and `carryforward`
match them. Per person: batch, and every roster attribute column.

THIS FILE IS STUDENT PII. It must live on the private Shared Drive or a local
path, never in this repository — `.gitignore` covers `*.json` and `*.gz`, but do
not rely on that alone.

Run:
    python tools/extract_roster_facts.py --out .cache/roster_facts.json.gz
    python tools/extract_roster_facts.py --from-file some_marked.xlsx --out ...
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import lms_roster as lr                                  # noqa: E402

FIELDS = ("cc", "phone", "email", "wa_cc", "whatsapp", "broadcast",
          "batch_name", "amount", "payment", "closing", "pod")


def extract(xlsx_bytes: bytes) -> dict:
    """Marked workbook (or a raw Sheet export) -> {identity: {batch, fields...}}."""
    tabs = lr.read_previous(xlsx_bytes)
    out, stats = {}, Counter()
    for code, rows in tabs.items():
        for row in rows:
            e, p = lr._identity(row)
            key = e or (f"p:{p}" if p else "")
            if not key:
                stats["no-identity"] += 1
                continue
            if key in out:
                stats["duplicate-identity"] += 1
                continue
            rec = {"batch": code}
            for i, name in enumerate(FIELDS):
                v = row[i] if i < len(row) else ""
                if v:
                    rec[name] = v
            out[key] = rec
            stats["people"] += 1
            if rec.get("closing"):
                stats["with-closing"] += 1
            if rec.get("amount"):
                stats["with-amount"] += 1
    return out, stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(ROOT, ".cache", "roster_facts.json.gz"))
    ap.add_argument("--from-file", default="",
                    help="read this local .xlsx instead of fetching the marked "
                         "workbook from the Drive store folder")
    a = ap.parse_args()

    if a.from_file:
        print(f"[1] reading {os.path.basename(a.from_file)} …", flush=True)
        data = open(a.from_file, "rb").read()
    else:
        print("[1] fetching the marked workbook from Drive …", flush=True)
        import pipeline, live_data
        cfg = pipeline.load_config()
        live_data.set_service_account(cfg["sa_info"], scopes=pipeline.RW_SCOPES)
        svc = live_data._drive_service()
        data = pipeline.fetch_prev_marked(svc, cfg, "nothing to extract from")
        if not data:
            sys.exit("No marked workbook in the store folder.")
    print(f"    {len(data):,} bytes")

    print("[2] extracting …", flush=True)
    facts, stats = extract(data)

    closings = Counter(v.get("closing", "") for v in facts.values())
    print(f"    {stats['people']:,} people across "
          f"{len(set(v['batch'] for v in facts.values()))} batches")
    print(f"    with a Closing Type: {stats['with-closing']:,}   "
          f"with an Amount: {stats['with-amount']:,}")
    if stats["duplicate-identity"]:
        print(f"    {stats['duplicate-identity']:,} duplicate identities "
              f"(first kept, as the marker does)")
    print("    closing-type mix the API could not reproduce:")
    for k, n in closings.most_common(10):
        print(f"      {(k or '(blank)'):<24} {n:>7,}")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    blob = json.dumps({"fields": list(FIELDS), "people": facts},
                      ensure_ascii=False).encode("utf-8")
    with gzip.open(a.out, "wb") as fh:
        fh.write(blob)
    print(f"\n[3] wrote {a.out}  ({os.path.getsize(a.out)/1e6:.1f} MB gzipped, "
          f"{len(blob)/1e6:.1f} MB raw)")
    print("    STUDENT PII — keep it off the public repo.")


if __name__ == "__main__":
    main()
