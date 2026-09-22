"""
ingest.py — take a week's Zoom exports by hand and get them into the pipeline.

The owner uploads each week's files themselves (2026-09-10) and wants them live
on the shared dashboard, not on one laptop. So the app's "Add data" page uses
this module to do three things, and deliberately NOT a fourth:

  1. read the file names and work out which SESSIONS they are  (pure, tested)
  2. check each one against the L2 schedule before anything moves (pure, tested)
  3. put the files on Drive and ask GitHub Actions to run the pipeline (thin I/O)

It never marks attendance. That belongs on GitHub's runner: in-process marking
is what made the app take three minutes and die on Streamlit Cloud's 1 GB
instance, and CLAUDE.md §2 exists to stop it coming back.

WHY THE L2 CHECK COMES FIRST
---------------------------
`data.REQUIRE_L2` drops any session whose webinar has no L2 row — it is not
merely unlabelled, it is HIDDEN. So a perfectly good upload of a session L2 has
never heard of produces a green run, a longer file list on Drive and no visible
change at all, which reads as "the tool is broken". Those files are refused up
front with the webinar id to add, which is the one thing that actually fixes it.

WHY THE FOLDER NAME MATTERS MORE THAN IT LOOKS
----------------------------------------------
`live_data.fetch_new_attendees` decides what to download from the FOLDER name:
`attendance_core._folder_batches` must find batch tokens in its leading
segments, or the folder is counted "without a roster tab" and never opened.
`_existing_sessions` reads the date and the POD from it too. So the name is
built from L2's own Batch Name cell plus the topic — the same shape the existing
605 folders use — rather than anything invented here.
"""
from __future__ import annotations

import re

import pods as _pods

# Zoom's two exports, as the pipeline's own parsers expect them. `attendee_` is
# `attendance_core._parse_filename`; `poll_` is `polls.name_key`. A file that
# matches neither is refused rather than uploaded: the parsers skip unknown
# names in silence, so it would sit on Drive forever doing nothing.
# Underscores only, because `attendance_core._parse_filename` accepts only
# underscores. Allowing a hyphen here would let 'attendee_99_2026-09-13.csv'
# pass this page's validation, upload, dispatch a run — and then be skipped in
# silence by the marker, which is the worst of the three outcomes.
_ATTENDEE = re.compile(r"^attendee_(\d{9,})_(20\d\d)_(\d{2})_(\d{2})", re.I)
_POLL = re.compile(r"^poll_(\d{9,})_(20\d\d)[_-](\d{2})[_-](\d{2})", re.I)
# The hand-saved form the poll reader also accepts: '9912345678 - 2026-08-30 - Poll Report.csv'
_POLL_ALT = re.compile(r"^(\d{9,})\s*[-_]\s*(20\d\d)[-_](\d{2})[-_](\d{2}).*poll", re.I)


def classify_name(name: str) -> tuple[str, str, str] | None:
    """'attendee_9912_2026_09_13.csv' -> ('attendee', '9912', '2026_09_13').

    Returns None for anything the pipeline's parsers would ignore. Any leading
    folders in the name are dropped first, so a ZIP's internal paths are fine.
    """
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    for kind, rx in (("attendee", _ATTENDEE), ("poll", _POLL), ("poll", _POLL_ALT)):
        m = rx.match(base)
        if m:
            return kind, m.group(1), f"{m.group(2)}_{m.group(3)}_{m.group(4)}"
    return None


def classify(names) -> tuple[dict, list]:
    """[file names] -> ({(wid, ymd): {'attendee': [...], 'poll': [...]}}, rejected).

    Grouped by webinar and date because that pair is what the whole pipeline
    calls a session — the same key `attendance_core` and `polls` join on.
    """
    sessions: dict = {}
    rejected = []
    for n in names or ():
        got = classify_name(n)
        if not got:
            rejected.append(n)
            continue
        kind, wid, ymd = got
        sessions.setdefault((wid, ymd), {"attendee": [], "poll": []})[kind].append(n)
    return sessions, rejected


def folder_name(ymd: str, label: str, topic: str) -> str:
    """'2026-09-13 - AI CAP B35 , B36 , B37 - Finance - Financial Analysis'.

    Date first (that is where `_mmdd` and `_ymd_key` read it), then L2's own
    Batch Name cell so `_folder_batches` can recover the batches and
    `pods.from_folder` the POD, then the topic for a human reading Drive.
    """
    date = ymd.replace("_", "-")
    parts = [date, re.sub(r"\s+", " ", str(label or "")).strip()]
    # Drive query strings are single-quoted (archive.ensure_folder looks the
    # folder up by name), so an apostrophe would break the lookup and create a
    # second folder for the same session.
    t = re.sub(r"[\\/:*?\"<>|']", " ", str(topic or "")).strip()
    if t:
        parts.append(re.sub(r"\s+", " ", t)[:80])
    return " - ".join(p for p in parts if p)


def preflight(sessions: dict, l2_map: dict, l2_labels: dict) -> list:
    """One row per uploaded session, saying what will happen to it.

    `l2_map` / `l2_labels` are `attendance_core.parse_l2(l2_bytes,
    with_labels=True)`. A session missing from L2 is marked `in_l2=False` and
    carries no folder — the caller must refuse it, because REQUIRE_L2 would hide
    it after a green run.
    """
    rows = []
    for (wid, ymd), got in sorted(sessions.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        info = (l2_map or {}).get(wid)
        label = str((l2_labels or {}).get(wid) or "").strip()
        keys, topic = info if info else (frozenset(), "")
        batches = sorted((f"B{n}" if t == "CAP" else f"{t} B{n}") for t, n in keys)
        pod = _pods.from_l2_label(label) if label else None
        rows.append({
            "wid": wid, "ymd": ymd, "date": ymd.replace("_", "-"),
            "in_l2": bool(info), "label": label, "topic": topic,
            "batches": batches,
            "pod": "" if pod in (None, _pods.WHOLE_BATCH) else pod,
            "n_attendee": len(got["attendee"]), "n_poll": len(got["poll"]),
            "files": got["attendee"] + got["poll"],
            "folder": folder_name(ymd, label, topic) if info else "",
        })
    return rows


def blockers(rows: list, rejected: list) -> list:
    """Plain-language reasons this upload should not proceed, or []."""
    out = []
    missing = [r for r in rows if not r["in_l2"]]
    if missing:
        out.append(
            "Not in the L2 schedule, so the dashboard would hide "
            f"{'them' if len(missing) > 1 else 'it'} even after a successful run — "
            "add a row for " + ", ".join(f"webinar {r['wid']} ({r['date']})"
                                         for r in missing[:5])
            + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
            + ", then upload again.")
    no_att = [r for r in rows if r["in_l2"] and not r["n_attendee"]]
    if no_att:
        out.append(
            "No attendee report, so there is nothing to mark (a poll on its own "
            "cannot produce attendance): "
            + ", ".join(f"{r['date']} webinar {r['wid']}" for r in no_att[:5]))
    if rejected:
        out.append(
            f"{len(rejected)} file(s) are not Zoom exports this pipeline reads — "
            "names must look like attendee_<webinar>_<YYYY>_<MM>_<DD>.csv or "
            "poll_<webinar>_<YYYY>_<MM>_<DD>.csv: "
            + ", ".join(rejected[:5]))
    return out


# ───────────────────────────── Drive + GitHub I/O ─────────────────────────────
def target_drive(attendee_folder_id: str) -> str:
    """Which Shared Drive new files go to: the LAST configured id.

    Not a free choice. `attendance_core.process_files` prefers the last-listed
    drive when the same session exists twice ("Zoom extracts", authoritative per
    the owner 2026-09-06), so uploading anywhere else would put the new copy
    behind an older one.
    """
    ids = _folder_ids(attendee_folder_id)
    if not ids:
        raise ValueError("no attendee folder configured")
    return ids[-1]


def _folder_ids(v) -> list:
    import live_data
    return live_data._folder_ids(v)


def upload_session(svc, drive_id: str, row: dict, blobs: dict) -> dict:
    """Create-or-reuse the session folder and put its files in it.

    `blobs` maps the uploaded file name to its bytes. Returns
    `{"folder_id":…, "uploaded":[names]}`. Uses the same helpers the pipeline
    uses, so a file that lands here is indistinguishable from one Zoom's own
    export dropped in by hand.
    """
    import archive
    import live_data
    fid = archive.ensure_folder(svc, drive_id, row["folder"])
    done = []
    for name in row["files"]:
        base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
        live_data.upload_to_folder(svc, fid, base, blobs[name], "text/csv")
        done.append(base)
    return {"folder_id": fid, "uploaded": done}


_GH = "https://api.github.com"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def dispatch(token: str, repo: str, workflow: str = "refresh.yml",
             ref: str = "main", inputs: dict | None = None) -> None:
    """Ask GitHub to run the pipeline. 204 No Content on success, so there is no
    run id to return — `latest_run` finds it by time."""
    import requests
    r = requests.post(
        f"{_GH}/repos/{repo}/actions/workflows/{workflow}/dispatches",
        headers=_headers(token), timeout=30,
        json={"ref": ref, "inputs": {k: str(v).lower() if isinstance(v, bool) else str(v)
                                     for k, v in (inputs or {}).items()}})
    if r.status_code not in (201, 204):
        raise RuntimeError(f"GitHub refused the run ({r.status_code}): {r.text[:300]}")


def latest_run(token: str, repo: str, workflow: str = "refresh.yml",
               since: str | None = None) -> dict | None:
    """The newest dispatched run, optionally only one created at/after `since`
    (an ISO-8601 UTC string). Returns None while GitHub is still queueing it."""
    import requests
    r = requests.get(
        f"{_GH}/repos/{repo}/actions/workflows/{workflow}/runs",
        headers=_headers(token), timeout=30,
        params={"event": "workflow_dispatch", "per_page": 10})
    r.raise_for_status()
    for run in r.json().get("workflow_runs") or ():
        if since and str(run.get("created_at") or "") < since:
            continue
        return {"id": run["id"], "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "url": run.get("html_url"), "created_at": run.get("created_at")}
    return None


def run_state(token: str, repo: str, run_id: int) -> dict:
    """`{'status','conclusion','url','step'}` — `step` is the job step running
    now, which is the only progress GitHub exposes without reading the log."""
    import requests
    r = requests.get(f"{_GH}/repos/{repo}/actions/runs/{run_id}",
                     headers=_headers(token), timeout=30)
    r.raise_for_status()
    run = r.json()
    step = ""
    try:
        j = requests.get(f"{_GH}/repos/{repo}/actions/runs/{run_id}/jobs",
                         headers=_headers(token), timeout=30).json()
        for job in j.get("jobs") or ():
            for s in job.get("steps") or ():
                if s.get("status") == "in_progress":
                    step = s.get("name") or ""
    except Exception:
        pass
    return {"status": run.get("status"), "conclusion": run.get("conclusion"),
            "url": run.get("html_url"), "step": step}
