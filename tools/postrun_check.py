"""postrun_check.py - what the last dispatched refresh DID, on one screen.

Reads the run through the GitHub CLI (`gh`, already signed in on this machine)
and prints only the lines that matter: each step with its clock time, the L2
gate's drops, GATE 6, every WARNING, a REFUSING TO PUBLISH, and the uploads.
The full log stays where it is; this is the page to read before trusting the
dashboard, and the page to paste when something looks off.

Usage (from the repo root):
  python tools/postrun_check.py             # the latest run of refresh.yml
  python tools/postrun_check.py --wait      # poll until it finishes (default 40 min max)
  python tools/postrun_check.py --run 35866043335

Exit 0 = the run succeeded; 1 = it failed, is still running, or was not found.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import time

REPO = "mukram-web/attendance-suite"
WORKFLOW = "refresh.yml"
FIELDS = "databaseId,status,conclusion,createdAt,updatedAt,url,event,displayTitle"

# Lines worth showing. Step markers first, then everything a human would grep for.
KEEP = re.compile(r"\[\d/8\]|\[1[a-d]\]|\[5a2?\]|\[8[a-c]\]|GATE \d|WARNING|REFUSING|ABORT"
                  r"|L2 gate|frozen|carried|uploaded|Uploading|column\(s\) marked"
                  r"|file\(s\) from|not in L2|not a session|Error|error:", re.I)
# Runner chatter that matches the above by accident: Node deprecation notices,
# git hints, the workflow's own coloured `echo` lines, the Node-20 banner.
NOISE = re.compile(r"DeprecationWarning|node --trace|^hint:|##\[warning\]Node\.js"
                   r"|punycode|url\.parse\(\)|::notice::|^# ")
ANSI = re.compile(r"\x1b\[[0-9;]*m|\^\[\[[0-9;]*m")
STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT)(\d\d:\d\d:\d\d)\.\d+Z (.*)$")


def gh(*args: str) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise SystemExit(f"gh {' '.join(args[:2])} failed: {p.stderr.strip()[:400]}")
    return p.stdout


def latest_run() -> dict | None:
    runs = json.loads(gh("run", "list", "-R", REPO, "--workflow", WORKFLOW,
                         "-L", "1", "--json", FIELDS))
    return runs[0] if runs else None


def run_info(run_id: int | str) -> dict:
    return json.loads(gh("run", "view", str(run_id), "-R", REPO, "--json", FIELDS))


def _iso(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def key_lines(log: str) -> list[str]:
    out = []
    for raw in log.splitlines():
        body = raw.split("\t", 2)[-1]          # gh prefixes "job\tstep\t"
        m = STAMP.match(body)
        text = ANSI.sub("", m.group(3) if m else body).strip()
        if not KEEP.search(text) or NOISE.search(text):
            continue
        clock = m.group(2) if m else "        "
        out.append(f"{clock}  {text.rstrip()}")
    return out


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                                # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", help="a run id (default: the latest run of the workflow)")
    ap.add_argument("--wait", action="store_true", help="poll every 30 s until the run completes")
    ap.add_argument("--timeout", type=int, default=40, help="minutes to wait at most (default 40)")
    args = ap.parse_args(argv)

    run = run_info(args.run) if args.run else latest_run()
    if not run:
        print(f"no run of {WORKFLOW} found on {REPO}")
        return 1
    rid = run["databaseId"]
    if args.wait:
        deadline = time.time() + args.timeout * 60
        while run["status"] != "completed":
            if time.time() > deadline:
                print(f"still {run['status']} after {args.timeout} min - giving up; {run['url']}")
                return 1
            print(f"  ... {run['status']} ({_dt.datetime.now():%H:%M:%S}), checking again in 30 s",
                  flush=True)
            time.sleep(30)
            run = run_info(rid)

    started = _iso(run["createdAt"])
    ended = _iso(run["updatedAt"])
    mins = (ended - started).total_seconds() / 60
    ist = started.astimezone(_dt.timezone(_dt.timedelta(hours=5, minutes=30)))
    print(f"Run {rid} - {run.get('displayTitle') or WORKFLOW}")
    print(f"  status: {run['status']} / {run.get('conclusion') or '-'}   started {ist:%d %b %Y %H:%M} IST"
          f"   {mins:.1f} min   via {run.get('event')}")
    print(f"  {run['url']}")
    print()
    if run["status"] != "completed":
        print("(not finished - pass --wait to poll)")
        return 1
    lines = key_lines(gh("run", "view", str(rid), "-R", REPO, "--log"))
    print("\n".join(lines) if lines else "(no step lines found in the log)")
    print()
    warn = sum(1 for ln in lines if "WARNING" in ln)
    gate6 = [ln for ln in lines if "GATE 6" in ln]
    verdict = ("SUCCESS" if run.get("conclusion") == "success" else f"FAILED ({run.get('conclusion')})")
    print(f"Verdict: {verdict} - {warn} warning line(s)"
          + (f"; {gate6[-1].split('GATE 6:', 1)[-1].strip()}" if gate6 else "; no GATE 6 line"))
    return 0 if run.get("conclusion") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
