"""preflight.py - what the next dispatched refresh WILL do with a weekend's
Zoom exports, worked out BEFORE anyone dispatches it.

The weekly job (refresh.yml) is deliberate: the owner reads this, answers what
needs answering, then presses "Run workflow". This does the reading the owner
used to do by hand - listing the Drive folders, matching them to L2, checking
the roster - and reports, in one page, per session:

  READY          L2 registers it on that date, its export is on Drive in a
                 folder naming a roster batch: the run will mark it.
  MISSING        L2 registers it, no export on either drive yet. The extractor
                 waits 6 h after a session ends; keyless Zoom accounts never
                 arrive. The run would simply not mark it, and a later run
                 picks it up once the export exists (its column is absent).
  DATE MISMATCH  an export exists only under a date L2 does not register the
                 webinar on (usually the extractor's day-early twin). The L2
                 gate will drop it: fix L2's date cell or re-extract - decide.
  NOT IN L2      an export for a webinar L2 has never heard of. Dropped by the
                 gate; add the row to L2 if it was a real session.
  NO TAB         a batch the dashboard would carry - a programme the roster
                 already has, numbered at or above its lowest tab - that has
                 no tab in the roster Sheet. Every folder for it is skipped
                 "without a roster tab" and the batch silently never appears
                 (B42 on 26-27 Sep 2026). Add the tab before dispatching.
  BAD FOLDER     the only export is in a folder whose name carries no roster
                 batch; the run never opens it. Rename the folder.
  SKIPPED        Hackathon calls (attendance_core.NOT_A_SESSION), and folders
                 L2 never listed whose name says walkthrough / broadcast /
                 telegram: the gate skips them. Listed so nobody hunts them.
  INFO           out-of-scope sessions (B11-B16, BSI: no tab by design, skipped
                 as always), rooms shared across programmes, duplicate copies
                 on one drive, dates already marked (a rerun leaves them
                 frozen exactly as they are).

It never writes anything, never downloads an attendee report, and never
dispatches. Exit 0 = nothing needs a decision; exit 2 = read the report.

Usage (from the repo root, with the suite's venv):
  PYTHONIOENCODING=utf-8 python tools/preflight.py                 # the most recent Sat+Sun
  PYTHONIOENCODING=utf-8 python tools/preflight.py --dates 2026-10-03 2026-10-04
  PYTHONIOENCODING=utf-8 python tools/preflight.py --out F:/preflight.md --json F:/preflight.json

Configuration is pipeline.load_config(): the same env vars the Actions job
uses, else .streamlit/secrets.toml, else the key file. STORE_FOLDER_ID is
needed to know which dates are already marked; without it that check is
skipped and the report says so.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

REPO = "mukram-web/attendance-suite"
ACTIONS_URL = f"https://github.com/{REPO}/actions/workflows/refresh.yml"

# The same shape attendance_core._parse_filename accepts: underscores only.
_ATTENDEE = re.compile(r"attendee_(\d{9,})_((?:20\d\d)_\d{2}_\d{2})", re.I)
# A folder L2 never listed whose NAME already says it was not a class. The gate
# drops it either way ("not in L2"); this only files it under SKIPPED instead
# of asking the owner about it every Monday. The 26-27 Sep 2026 weekend had a
# walkthrough and a Telegram broadcast; the Hackathon rooms are L2 rows and
# attendance_core.NOT_A_SESSION handles those.
_NON_SESSION_FOLDER = re.compile(r"walkthrough|broadcast|telegram|hackathon", re.I)

DECISION_KEYS = ("no_tab", "bad_folder", "date_mismatch", "not_in_l2", "missing")
BUCKETS = ("ready", "missing", "date_mismatch", "not_in_l2", "no_tab", "bad_folder",
           "skipped", "out_of_scope", "shared", "twins", "already_marked")


# ----------------------------------------------------------------- pure parts

def weekend_dates(today: _dt.date | None = None) -> list[_dt.date]:
    """The most recent Saturday and Sunday: this weekend on a Sat/Sun, else the
    one just gone. Monday's run therefore looks at the weekend before it."""
    today = today or _dt.date.today()
    sat = today - _dt.timedelta(days=(today.weekday() - 5) % 7)
    return [sat, sat + _dt.timedelta(days=1)]


def ymd(d: _dt.date) -> str:
    return f"{d:%Y_%m_%d}"


def as_key(k):
    """Batch keys are (track, number); a bare number means CAP."""
    if isinstance(k, tuple):
        return (str(k[0]), int(k[1]))
    return ("CAP", int(k))


def batch_name(key) -> str:
    track, n = as_key(key)
    return f"B{n}" if track == "CAP" else f"{track} B{n}"


def in_scope(key, roster_keys) -> bool:
    """A batch the dashboard would carry if it had a tab: a programme the
    roster already has, numbered at or above that programme's lowest tab.
    The roster starts at B17 by design and BSIAI left the dashboard on
    2026-09-22, so B11 or a BSI batch with no tab is the status quo, not a
    gap; B43 or ECAP B4 with no tab is exactly the gap this exists to catch."""
    track, n = as_key(key)
    nums = [m for t, m in (as_key(k) for k in roster_keys) if t == track]
    return bool(nums) and n >= min(nums)


def assess(weekend, l2rows, l2_other, exports, roster_keys, marked_dates) -> dict:
    """Pure. What the run will do with each session, given:

    weekend      {'YYYY_MM_DD', ...} - the dates being loaded
    l2rows       {wid: {'dates': {ymd on the weekend}, 'batches': {(track, n)},
                        'topic': str, 'label': str, 'not_session': bool}}
                 - every L2 webinar registered on a weekend date
    l2_other     {wid: set-of-dates | NotASession} for every OTHER L2 webinar
    exports      {wid: [{'date': ymd, 'folder': name, 'drive': name,
                         'batches': {(track, n)}}]} - attendee reports on
                 Drive dated on the weekend or the day before
    roster_keys  {(track, n)} - the tabs this week's roster (plus ECAP) has
    marked_dates {'YYYY_MM_DD'} - dates already marked in the base workbook

    Mirrors live_data.fetch_new_attendees: a folder is opened only when its
    NAME carries a batch that has a roster tab, so an export in a folder
    naming none is invisible to the run.
    """
    f: dict = {k: [] for k in BUCKETS}
    roster_keys = {as_key(k) for k in roster_keys}
    seen_no_tab: set = set()

    def flag_no_tab(keys, why):
        for k in sorted({as_key(x) for x in keys} - roster_keys):
            if in_scope(k, roster_keys) and k not in seen_no_tab:
                seen_no_tab.add(k)
                f["no_tab"].append({"batch": batch_name(k), "why": why})

    def usable(ex):
        """The copies the run would open: folder names a roster batch."""
        return [e for e in ex if {as_key(k) for k in (e.get("batches") or ())} & roster_keys]

    def _order(w):
        return (min(l2rows[w]["dates"] or {""}), str(l2rows[w].get("label") or ""))

    for wid in sorted(l2rows, key=_order):
        row = l2rows[wid]
        ex = exports.get(wid, [])
        keys = {as_key(x) for x in row["batches"]}
        base = {"wid": wid, "label": row.get("label") or "", "topic": row.get("topic") or "",
                "dates": sorted(row["dates"]), "batches": [batch_name(k) for k in sorted(keys)]}
        if row.get("not_session"):
            f["skipped"].append({**base, "exports": len(ex)})
            continue
        flag_no_tab(keys, f"L2 lists it for {base['label'] or base['wid']}")
        covered = keys & roster_keys
        if not covered:
            if not any(k in seen_no_tab for k in keys):
                f["out_of_scope"].append({**base, "exports": len(ex)})
            continue                        # a NO TAB item already says the rest
        if len({t for t, _ in keys}) > 1:
            f["shared"].append(base)
        use = usable(ex)
        on_date = [e for e in use if e["date"] in row["dates"]]
        if on_date:
            f["ready"].append({**base, "folders": sorted({(e["drive"], e["folder"]) for e in on_date})})
            for drive in sorted({e["drive"] for e in on_date}):
                fold = sorted({e["folder"] for e in on_date if e["drive"] == drive})
                if len(fold) > 1:
                    f["twins"].append({**base, "drive": drive, "folders": fold})
        elif use:
            f["date_mismatch"].append({**base, "export_dates": sorted({e["date"] for e in use}),
                                       "folders": sorted({e["folder"] for e in use})})
        elif ex:
            f["bad_folder"].append({**base, "export_dates": sorted({e["date"] for e in ex}),
                                    "folders": sorted({e["folder"] for e in ex})})
        else:
            f["missing"].append(base)

    for wid, ex in sorted(exports.items()):
        if wid in l2rows:
            continue
        fkeys = set()
        for e in ex:
            fkeys |= {as_key(k) for k in (e.get("batches") or ())}
            flag_no_tab(e.get("batches") or (), f"folder '{e['folder']}'")
        item = {"wid": wid, "label": "", "topic": "", "dates": [],
                "batches": [batch_name(k) for k in sorted(fkeys)],
                "export_dates": sorted({e["date"] for e in ex}),
                "folders": sorted({e["folder"] for e in ex})}
        other = l2_other.get(wid)
        if isinstance(other, str):
            f["skipped"].append({**item, "topic": str(other),
                                 "dates": sorted(getattr(other, "dates", ())), "exports": len(ex)})
            continue
        if not usable(ex):
            if not any(k in seen_no_tab for k in fkeys):
                f["out_of_scope"].append({**item, "exports": len(ex)})
            continue                        # the run never opens these folders
        if other is None:
            m = _NON_SESSION_FOLDER.search(" ".join(item["folders"]))
            if m:
                f["skipped"].append({**item, "topic": f"not in L2; the folder name says '{m.group(0)}'",
                                     "exports": len(ex)})
            else:
                f["not_in_l2"].append(item)
        else:
            f["date_mismatch"].append({**item, "dates": sorted(other)})

    f["already_marked"] = sorted(set(weekend) & set(marked_dates))
    return f


def _d(ymd_s: str) -> str:
    try:
        return _dt.datetime.strptime(ymd_s, "%Y_%m_%d").strftime("%d %b")
    except ValueError:
        return ymd_s


def render(dates, f: dict, notes: list, stamps: dict, counts: dict) -> str:
    """The report, as markdown that also reads fine as plain text."""
    span = (f"{dates[0]:%d %b}-{dates[-1]:%d %b %Y}" if len(dates) > 1
            else f"{dates[0]:%d %b %Y}")
    decisions = sum(len(f[k]) for k in DECISION_KEYS)
    out = [f"# Pre-flight: weekend of {span}",
           f"_L2 modified {stamps.get('l2', '?')} · roster modified {stamps.get('roster', '?')} · "
           f"{counts.get('folders', 0)} session folder(s) on {counts.get('drives', 0)} drive(s) · "
           f"{counts.get('exports', 0)} attendee export(s)_", ""]
    if decisions:
        out.append(f"## Verdict: NEEDS ATTENTION - {decisions} item(s) before dispatching")
        for k, title in (("no_tab", "NO TAB"), ("bad_folder", "BAD FOLDER"),
                         ("date_mismatch", "DATE MISMATCH"), ("not_in_l2", "NOT IN L2"),
                         ("missing", "MISSING")):
            if f[k]:
                out.append(f"- **{title}** x{len(f[k])}")
    else:
        out.append("## Verdict: READY TO DISPATCH")
    out.append("")

    def sess(r):
        who = r.get("label") or ", ".join(r.get("batches") or []) or "?"
        return f"{', '.join(_d(x) for x in r.get('dates') or []) or '-'} | {who} | {r.get('topic') or '-'} | {r['wid']}"

    if f["no_tab"]:
        out += ["## NO TAB - the run skips every folder for these batches", ""]
        out += [f"- **{r['batch']}** has no tab in the roster Sheet ({r['why']}). Add it before dispatching."
                for r in f["no_tab"]]
        out.append("")
    if f["bad_folder"]:
        out += ["## BAD FOLDER - the folder name carries no roster batch, so the run never opens it", "",
                "| L2 date | L2 label | Topic | Webinar | Export dated | Folder |", "|---|---|---|---|---|---|"]
        out += [f"| {sess(r)} | {', '.join(_d(x) for x in r['export_dates'])} | {'; '.join(r['folders'])} |"
                for r in f["bad_folder"]]
        out += ["", "Rename the folder so it starts `YYYY-MM-DD - AI CAP B<n> - ...` (the batch the "
                "session was for), or re-extract.", ""]
    if f["date_mismatch"]:
        out += ["## DATE MISMATCH - the L2 gate will drop these exports", "",
                "| L2 date | L2 label | Topic | Webinar | Export dated | Folder |", "|---|---|---|---|---|---|"]
        out += [f"| {sess(r)} | {', '.join(_d(x) for x in r['export_dates'])} | {'; '.join(r['folders'])} |"
                for r in f["date_mismatch"]]
        out += ["", "Either L2's date cell is wrong (fix the cell) or the export is (re-extract "
                "with the right date). A day-early twin whose correct export also exists is harmless.", ""]
    if f["not_in_l2"]:
        out += ["## NOT IN L2 - the L2 gate will drop these exports", "",
                "| Webinar | Folder batches | Export dated | Folder |", "|---|---|---|---|"]
        out += [f"| {r['wid']} | {', '.join(r['batches']) or '-'} | {', '.join(_d(x) for x in r['export_dates'])} "
                f"| {'; '.join(r['folders'])} |" for r in f["not_in_l2"]]
        out += ["", "A real session goes into L2 (batch, topic, date, webinar id); a walkthrough, "
                "a broadcast or a call is left alone and stays dropped.", ""]
    if f["missing"]:
        out += ["## MISSING - registered in L2, no export on Drive yet", "",
                "| Date | L2 label | Topic | Webinar |", "|---|---|---|---|"]
        out += [f"| {sess(r)} |" for r in f["missing"]]
        out += ["", "The extractor fetches 6 h after a session ends (last sweep 11:30 IST). A "
                "session on a Zoom account with no API key never arrives. Dispatching now marks "
                "everything else; these are picked up by the next run once their export exists.", ""]
    out += [f"## READY - the run will mark these ({len(f['ready'])})", ""]
    if f["ready"]:
        out += ["| Date | L2 label | Topic | Webinar | Drive |", "|---|---|---|---|---|"]
        out += [f"| {sess(r)} | {', '.join(sorted({d for d, _ in r['folders']}))} |" for r in f["ready"]]
    else:
        out.append("_nothing_")
    out.append("")
    if f["skipped"]:
        out += [f"## SKIPPED by rule - Hackathon calls, not sessions ({len(f['skipped'])})", ""]
        out += [f"- {sess(r)} ({r.get('exports', 0)} export(s) on Drive, ignored)" for r in f["skipped"]]
        out.append("")
    if f["out_of_scope"] or f["shared"] or f["twins"] or f["already_marked"]:
        out += ["## INFO", ""]
        if f["out_of_scope"]:
            names = dict.fromkeys(r.get("label") or ", ".join(r["batches"]) or r["wid"]
                                  for r in f["out_of_scope"])
            out.append(f"- not in the dashboard's scope, skipped as always (no roster tab by design): "
                       + "; ".join(names))
        for r in f["shared"]:
            out.append(f"- room shared across programmes: {sess(r)} - batches {', '.join(r['batches'])}")
        for r in f["twins"]:
            out.append(f"- two copies of one session on {r['drive']} (the fuller one wins): "
                       f"{r['wid']} in {'; '.join(r['folders'])}")
        if f["already_marked"]:
            out.append(f"- already marked: {', '.join(_d(x) for x in f['already_marked'])} - this is a "
                       "rerun; those columns stay exactly as they are (the freeze).")
        out.append("")
    if notes:
        out += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    out += ["## Dispatch", "",
            "Nothing here has been dispatched. When the answers above are in:", "",
            "```bash", f"gh workflow run refresh.yml -R {REPO}", "```", "",
            f"or the Run workflow button at {ACTIONS_URL}, or the dashboard's Add data tab. "
            "Then `python tools/postrun_check.py --wait`.", ""]
    return "\n".join(out)


# ------------------------------------------------------------------ the I/O

def collect(dates: list[_dt.date]) -> tuple[dict, list, dict, dict]:
    """Everything `assess` needs, read live. Returns (findings, notes, stamps, counts)."""
    import attendance_core as ac
    import ffa
    import live_data
    import pipeline

    weekend = {ymd(d) for d in dates}
    before = {ymd(d - _dt.timedelta(days=1)) for d in dates}
    notes: list = []

    cfg = pipeline.load_config()
    live_data.set_service_account(cfg["sa_info"])          # read-only scopes
    svc = live_data._drive_service()

    # L2: the register, plus batches/topic/label per webinar.
    l2_bytes, l2_stamp = live_data.fetch_sheet_cached(svc, cfg["l2_id"])
    reg = ac.l2_dates(l2_bytes)
    parsed, labels = ac.parse_l2(l2_bytes, with_labels=True)
    l2rows, l2_other = {}, {}
    for wid, v in reg.items():
        hit = set(getattr(v, "dates", v)) & weekend
        if hit:
            b, t = parsed.get(wid, (frozenset(), ""))
            l2rows[wid] = {"dates": hit, "batches": set(b), "topic": t or str(v),
                           "label": labels.get(wid, ""), "not_session": isinstance(v, str)}
        else:
            l2_other[wid] = v
    # FFA sits in the register (ffa.py) but not in parse_l2: fill in its batches.
    for s in ffa.sessions():
        w, d = ffa._norm_wid(s["wid"]), ffa._norm_ymd(s["ymd"])
        if w in l2rows and d in weekend:
            l2rows[w]["batches"] |= {as_key(x) for x in (s.get("batches") or ())}
            l2rows[w]["topic"] = l2rows[w]["topic"] or "FFA"
            l2rows[w]["label"] = l2rows[w]["label"] or "FFA - " + ", ".join(
                batch_name(x) for x in sorted(as_key(y) for y in (s.get("batches") or ())))

    # This week's roster: the tabs the run will have (Sheet export + ECAP graft).
    roster_bytes, roster_stamp = live_data.fetch_sheet_cached(svc, cfg["roster_id"])
    if cfg.get("ecap_roster_id"):
        try:
            import ecap
            roster_bytes, _ = ecap.graft(roster_bytes, live_data.fetch_file_bytes(svc, cfg["ecap_roster_id"]))
        except Exception as e:                                       # noqa: BLE001
            notes.append(f"ECAP roster could not be read ({e}); ECAP tabs treated as absent.")
    roster_keys = set(live_data._existing_sessions(roster_bytes))

    # Last week's marked workbook: which dates are already marked.
    marked: set = set()
    if cfg.get("store_folder_id"):
        prev = pipeline.fetch_prev_marked(svc, cfg, "already-marked dates unknown",
                                          log=lambda m: notes.append(str(m).strip()))
        if prev:
            marked = live_data.marked_dates(prev)
    else:
        notes.append("STORE_FOLDER_ID / store_folder_id is not configured, so which dates are "
                     "already marked could not be checked.")

    # The attendee drives: session folders dated on the weekend or the day before.
    wanted = weekend | before
    drive_ids = live_data._folder_ids(cfg["attendee_folder_id"])
    folders: list = []
    for i, fid in enumerate(drive_ids, 1):
        try:
            label = svc.drives().get(driveId=fid, fields="name").execute().get("name") or f"drive {i}"
        except Exception:                                            # noqa: BLE001
            label = f"drive {i}"
        label = label.strip()
        try:
            for fo in live_data._list_children(svc, fid):
                if fo["mimeType"] == live_data._FOLDER_MIME and ac._ymd(fo["name"]) in wanted:
                    folders.append((label, fo["name"], fo["id"]))
        except Exception as e:                                       # noqa: BLE001
            notes.append(f"{label} could not be listed ({e}) - its sessions are NOT in this report.")

    def _kids(item):
        label, name, fid = item
        try:
            return label, name, live_data._list_children(live_data._thread_drive(), fid), None
        except Exception as e:                                       # noqa: BLE001
            return label, name, [], str(e)

    exports: dict = {}
    n_exports = 0
    if folders:
        with ThreadPoolExecutor(max_workers=min(8, len(folders))) as ex:
            for label, name, kids, err in ex.map(_kids, folders):
                if err:
                    notes.append(f"folder '{name}' could not be listed ({err}).")
                    continue
                batches = set(ac._folder_batches(name))
                for k in kids:
                    m = _ATTENDEE.search(k["name"])
                    if m and k["mimeType"] != live_data._FOLDER_MIME:
                        n_exports += 1
                        exports.setdefault(m.group(1), []).append(
                            {"date": m.group(2), "folder": name, "drive": label, "batches": batches})

    findings = assess(weekend, l2rows, l2_other, exports, roster_keys, marked)
    stamps = {"l2": l2_stamp, "roster": roster_stamp}
    counts = {"folders": len(folders), "drives": len(drive_ids), "exports": n_exports}
    return findings, notes, stamps, counts


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                                # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dates", nargs="+", metavar="YYYY-MM-DD",
                    help="the session dates to check (default: the most recent Saturday and Sunday)")
    ap.add_argument("--out", help="also write the report (markdown) here")
    ap.add_argument("--json", help="also write the findings as JSON here")
    args = ap.parse_args(argv)
    dates = ([_dt.date.fromisoformat(d) for d in args.dates] if args.dates else weekend_dates())
    dates = sorted(set(dates))

    findings, notes, stamps, counts = collect(dates)
    report = render(dates, findings, notes, stamps, counts)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"[written] {args.out}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"dates": [d.isoformat() for d in dates], "findings": findings,
                       "notes": notes, "stamps": stamps, "counts": counts}, fh, indent=1, default=list)
        print(f"[written] {args.json}")
    return 2 if any(findings[k] for k in DECISION_KEYS) else 0


if __name__ == "__main__":
    sys.exit(main())
