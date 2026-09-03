"""
AttendaSync core engine (v2 — multi-batch aware).

Marks Zoom webinar attendance into a Master Batch Roster from a bulk attendee ZIP,
using the L2 weekly schedule (Webinar ID -> batch/date/topic).

Key behaviour:
  * A session shared by several batches (e.g. "AI CAP B2 + B3 + B4", "AI CAP B1 - AI CAP B22")
    is marked in EVERY covered batch that has a roster sheet — nothing is skipped.
  * If a webinar ID isn't in L2, the batch is recovered from the attendee's folder name.
  * If several attendee files land on the same batch+date, their attendee lists are merged.

Matching rule (default 'exact', per the auditing guide):
  Registered mail == Email (lower-cased, whitespace-stripped);
  Registered Number == Phone (digits only, trailing .0 removed); blanks never match.
Optional 'inclusive' mode also uses WhatsApp / broadcast columns and last-10-digit phones.
"""
import re, csv, io, zipfile, datetime
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

# ---------------- helpers ----------------
def _digits(s): return re.sub(r'\D', '', s or '')
def _wid(v):    return _digits(str(v)) if v is not None else ''

def _mmdd(v):
    if v is None: return None
    if isinstance(v, (datetime.datetime, datetime.date)):
        return f'{v.month:02d}_{v.day:02d}'
    s = str(v)
    m = re.search(r'(20\d\d)[_\-/](\d{1,2})[_\-/](\d{1,2})', s)
    if m: return f'{int(m.group(2)):02d}_{int(m.group(3)):02d}'
    months = dict(jan=1, feb=2, mar=3, apr=4, may=5, jun=6, jul=7,
                  aug=8, sep=9, oct=10, nov=11, dec=12)
    up = s.upper()
    for nm, mn in months.items():
        mt = re.search(r'([\dOo]{1,2})\s*(?:ST|ND|RD|TH)?\s*' + nm.upper(), up)
        if mt:
            return f'{mn:02d}_{int(mt.group(1).replace("O","0").replace("o","0")):02d}'
    return None

# A session column header is the date, plus the POD when the session was for
# one domain: "2026_08_23 | Techies". From B35 a single date carries up to
# eleven different sessions, so the date alone no longer identifies a column -
# keying on it merged all eleven attendee sets into one, which is how B35's
# 23 Aug came to report 1,193 present against a 916-member POD.
POD_SEP = " | "


def _col_pod(header):
    """Session column header -> its POD name, or '' for a whole-batch session."""
    s = str(header or "")
    return s.split(POD_SEP, 1)[1].strip() if POD_SEP in s else ""


def _col_header(ymd, pod):
    return f"{ymd}{POD_SEP}{pod}" if pod else ymd


def _parse_filename(fname):
    m = re.match(r'attendee_(\d+)_((20\d\d)_(\d{2})_(\d{2}))', fname)
    return (m.group(1), m.group(2)) if m else None

def _track(seg):
    s = seg.lower()
    # BSIAI first: its folders and L2 rows say 'BSIAI B1', 'BSI AI B3' or a
    # plain 'BSI B1'. Without this they fall through to CAP and collide head-on
    # with AI CAP B1/B2 - same key, different programme, silently wrong marks.
    if re.search('(?<![a-z])bsi', s):  return 'BSIAI'
    if 'ecap' in s or 'e-cap' in s: return 'ECAP'
    if re.search(r'\bus\b', s):      return 'US'
    if 'lcp' in s:                   return 'LCP'
    return 'CAP'

def extract_batches(text):
    """A batch-name string -> set of (track, number) keys. Handles &, + / commas, and - ranges."""
    keys = set()
    for seg in re.split(r'&|\band\b', text):
        tr = _track(seg)
        # 'CAP\s*B?' also catches the run-together form the POD rows use
        # ('AICAPB35, B36-Techies'): there is no word boundary before B35, so
        # the B pattern alone silently credits the session to B36 only.
        nums = sorted(set(int(x) for x in re.findall(r'\bB(\d{1,2})\b', seg, re.I)) |
                      set(int(x) for x in re.findall(r'CAP\s*B?(\d{1,2})\b', seg, re.I)))
        if not nums:
            continue
        is_list  = ('+' in seg) or (',' in seg)
        is_range = (not is_list) and bool(re.search(r'[-–]', seg)) and len(nums) >= 2
        chosen = range(min(nums), max(nums) + 1) if is_range else nums
        for n in chosen:
            keys.add((tr, n))
    return keys

def _sheet_key(name):
    """Roster sheet name -> (track, number) or None."""
    if 'att' in name.lower(): return None
    m = re.search(r'B?(\d{1,2})\b', name.split('-')[0])
    if not m: return None
    return (_track(name), int(m.group(1)))

def _folder_batches(member_path):
    """Recover batch keys from an attendee file's folder when the webinar isn't in L2."""
    folder = member_path.rsplit('/', 1)[0] if '/' in member_path else member_path
    rest = re.sub(r'^\d{4}-\d{2}-\d{2}\s*-\s*', '', folder)
    parts, batch_parts = re.split(r'\s+-\s+', rest), []
    for p in parts:                       # keep leading segments that name batches; stop at topic
        # Same run-together form extract_batches handles ('AICAPB37- Techies'):
        # without it this gate rejects the segment, the folder resolves to NO
        # batch, and the session is never even downloaded - L2 never gets asked.
        if re.search(r'\bB\d{1,2}\b|CAP\s*B?\d{1,2}\b', p, re.I): batch_parts.append(p)
        else: break
    return extract_batches(' - '.join(batch_parts)) if batch_parts else set()

# ---------------- L2 schedule ----------------
def parse_l2(l2_bytes, with_labels=False, with_mentors=False):
    """webinar_id -> (frozenset[(track,num)], topic). Includes combined/range sessions.

    With `with_labels`, also returns {webinar_id: raw 'Batch Name' cell} so callers
    can show a session the way L2 names it ('AI CAP B35 - Techies', 'AI CAP B8 + B22')
    rather than a bare parsed number. With `with_mentors`, also returns
    {webinar_id: 'Mentor' cell} - who actually taught it. Both extra maps are
    first-wins per webinar id, exactly like `out`, so they never disagree.

    The flags are separate rather than one wider return because five callers
    already unpack the two-tuple; adding a third element unconditionally would
    break every one of them."""
    wb = load_workbook(io.BytesIO(l2_bytes), data_only=True)
    out, labels, mentors = {}, {}, {}
    for ws in wb.worksheets:
        bcol = tcol = wcol = mcol = hrow = None
        for r in range(1, min(ws.max_row or 1, 6) + 1):
            for c in range(1, (ws.max_column or 1) + 1):
                v = str(ws.cell(r, c).value or '').strip().lower()
                if v == 'batch name':  bcol, hrow = c, r
                elif v == 'topic name': tcol = c
                elif v == 'webinar id': wcol = c
                # EXACT match only: the same header row also carries "Mentor's
                # email" and "In-house/Freelancer mentor", and a substring test
                # would bind the column to whichever came first.
                elif v == 'mentor': mcol = c
            if bcol and wcol: break
        if not (bcol and wcol and hrow): continue
        for r in range(hrow + 1, (ws.max_row or hrow) + 1):
            wid = _wid(ws.cell(r, wcol).value)
            if not wid: continue
            raw = str(ws.cell(r, bcol).value or '').strip()
            keys = extract_batches(raw)
            if not keys: continue
            topic = str(ws.cell(r, tcol).value or '').strip() if tcol else ''
            if wid not in out:
                out[wid] = (frozenset(keys), topic)
                labels[wid] = raw
                # Older monthly tabs have no Mentor column at all; absent simply
                # means "not recorded", which the UI renders as blank.
                if mcol:
                    mentors[wid] = str(ws.cell(r, mcol).value or '').strip()
    if with_labels and with_mentors: return out, labels, mentors
    if with_mentors: return out, mentors
    return (out, labels) if with_labels else out

# ---------------- Zoom attendee report ----------------
def parse_attendees(text):
    rows = list(csv.reader(io.StringIO(text)))
    hidx = next((i for i, r in enumerate(rows)
                 if r and r[0].strip() == 'Attended' and 'First Name' in r), None)
    emails, ph_full, ph_last10 = set(), set(), set()
    if hidx is None: return emails, ph_full, ph_last10
    h = rows[hidx]
    try: ei, pi = h.index('Email'), h.index('Phone')
    except ValueError: return emails, ph_full, ph_last10
    for r in rows[hidx + 1:]:
        if not r or len(r) <= ei: continue
        if r[0].strip() == 'Attended': break
        e = re.sub(r'\s', '', (r[ei] or '')).lower()
        p = _digits(re.sub(r'\.0$', '', ((r[pi] if len(r) > pi else '') or '').strip()))
        if e: emails.add(e)
        if p:
            ph_full.add(p)
            if len(p) >= 10: ph_last10.add(p[-10:])
    return emails, ph_full, ph_last10

# ---------------- roster helpers ----------------
def _header_row(ws):
    for r in range(1, 4):
        j = ' '.join(str(ws.cell(r, c).value or '') for c in range(1, 13)).lower()
        if 'registered number' in j or 'registered mail' in j: return r
    return 1

def _col(ws, hr, *needles):           # first column whose header contains all needles
    for c in range(1, (ws.max_column or 1) + 1):
        h = str(ws.cell(hr, c).value or '').strip().lower()
        if all(nd in h for nd in needles): return c
    return None

def _last_used(ws):
    last = 10
    for c in range(1, (ws.max_column or 1) + 1):
        for r in range(1, (ws.max_row or 1) + 1):
            if ws.cell(r, c).value is not None:
                last = c; break
    return last

def _cell_email(v):
    if v is None: return ''
    s = str(v); return re.sub(r'\s', '', s).lower() if '@' in s else ''
def _cell_phone(v):
    if v is None: return ''
    if isinstance(v, (int, float)):
        s = str(int(v)) if float(v).is_integer() else str(v)
    else:
        s = re.sub(r'\.0$', '', str(v).strip())
    return _digits(s)

# ---------------- main entry ----------------
def process(roster_bytes, l2_bytes, zip_bytes, mode='exact', values_only=False):
    """Mark attendance from an uploaded ZIP of Zoom attendee CSVs.

    Thin wrapper: unpacks the ZIP into (name, raw_bytes) pairs and hands them to
    process_files(). Returns (output_xlsx_bytes, report_rows, warnings).
    """
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    files = [(info, z.read(info)) for info in z.namelist() if not info.endswith('/')]
    return process_files(roster_bytes, l2_bytes, files, mode=mode, values_only=values_only)


def process_files(roster_bytes, l2_bytes, attendee_files, mode='exact', values_only=False):
    """Same engine as process(), but attendee data arrives as a list of
    (name, raw_bytes) pairs instead of a ZIP.

    This lets the marker run off ANY source — an uploaded ZIP, or files pulled
    live from a Google Drive folder/zip — without changing the matching logic.
    `name` should keep its folder path (e.g. "2025-05-09 - AI CAP B17 - Topic/
    attendee_123_2025_05_09.csv") so the folder-name fallback still works.
    `l2_bytes` may be falsy; then sessions are mapped by folder name only.

    `values_only`: some rosters store attendance as FORMULAS. With the default
    (False) we keep formulas (right when the output will be re-opened/recomputed
    in Excel/Sheets). Set True to load cached values instead — formulas become
    static text, so the marked workbook still carries every Present/Absent when
    read straight back by a values-only reader (the dashboard). Without this,
    saving a formula-based roster drops those cached values.

    Returns (output_xlsx_bytes, report_rows, warnings).
    """
    import pods

    wb = load_workbook(io.BytesIO(roster_bytes), data_only=values_only)
    wid_map, l2_labels = (parse_l2(l2_bytes, with_labels=True) if l2_bytes
                          else ({}, {}))

    key_sheet = {}
    for name in wb.sheetnames:
        k = _sheet_key(name)
        if k and k not in key_sheet: key_sheet[k] = name

    blob = {}                                            # name -> raw bytes
    chosen = {}                                          # (wid,ymd) -> name (prefer non "(1)")
    for name, data in attendee_files:
        blob[name] = data
        pf = _parse_filename(name.split('/')[-1])
        if not pf: continue
        if pf in chosen and '(1)' in name: continue
        chosen[pf] = name

    # accumulate attendee sets per (sheet_name, mmdd, POD), merging multiple files
    acc, warnings = {}, []
    unknown_pods = set()
    for (wid, ymd), info in chosen.items():
        keys, topic = wid_map.get(wid, (None, ''))
        if keys is None:                                 # not in L2 -> recover from folder
            keys = _folder_batches(info)
            if keys:
                warnings.append(f'Webinar {wid}: not in L2 — batch taken from folder name')
        if not keys:
            warnings.append(f'Webinar {wid}: no batch info in L2 or folder — skipped ({info.split("/")[-1]})')
            continue
        try:
            text = blob[info].decode('utf-8-sig', errors='replace')
        except Exception as e:
            warnings.append(f'Could not read {info}: {e}'); continue
        em, pf, p10 = parse_attendees(text)
        mm = f'{int(ymd[5:7]):02d}_{int(ymd[8:10]):02d}'

        # Which POD is this session for? L2's own label is authoritative; the
        # folder name is the fallback, exactly as it is for the batch itself.
        # An unrecognised domain becomes a whole-batch session AND warns - it
        # must never be silently folded into another POD's denominator.
        pod = pods.from_l2_label(l2_labels.get(wid)) if l2_labels.get(wid) else None
        if pod is None:
            pod = pods.from_folder(info)
        if pod is None and l2_labels.get(wid):
            tail = str(l2_labels.get(wid)).split('-')[-1].strip()
            if tail and tail not in unknown_pods:
                unknown_pods.add(tail)
                warnings.append(f'Webinar {wid}: POD "{tail}" not recognised - '
                                f'counted against the whole batch. Add it to pods._ALIASES.')
        pod = '' if pod in (None, pods.WHOLE_BATCH) else pod
        targets = [key_sheet[k] for k in keys if k in key_sheet]
        if not targets:
            blist = ', '.join(f'B{n}' if t == 'CAP' else f'{t} B{n}' for t, n in sorted(keys))
            warnings.append(f'Webinar {wid}: batch(es) {blist} not in this roster — left empty (sheets are never created)')
            continue
        for sheet in targets:
            a = acc.setdefault((sheet, mm, pod), dict(ymd=ymd, topic=topic, pod=pod,
                                                      em=set(), pf=set(), p10=set()))
            a['em'] |= em; a['pf'] |= pf; a['p10'] |= p10
            if not a['topic']: a['topic'] = topic

    # write per sheet
    report = []
    sheets = sorted({k[0] for k in acc}, key=lambda n: _sheet_key(n) or ('', 0))
    for sheet in sheets:
        ws = wb[sheet]; hr = _header_row(ws)
        rm = _col(ws, hr, 'registered', 'mail')
        rn = _col(ws, hr, 'registered', 'number')
        wa = _col(ws, hr, 'whatsa');  bc = _col(ws, hr, 'broadcast')
        pc = _col(ws, hr, 'pod')      # 'POD pref' / 'POD Pref' / 'POD Prefrence'
        if rm is None and rn is None:
            warnings.append(f'Sheet "{sheet}": no Registered mail/number columns — skipped')
            continue
        dmap = {}
        for c in range(11, (ws.max_column or 1) + 1):
            h1 = ws.cell(1, c).value
            d = _mmdd(h1) or (_mmdd(ws.cell(2, c).value) if hr == 2 else None)
            if d: dmap.setdefault((d, _col_pod(h1)), c)
        nxt = _last_used(ws) + 1
        # Real rosters sometimes have merged cells in the body; writing a mark into
        # a merged (non-anchor) cell raises in openpyxl. Unmerge any range that
        # touches a data row (header merges in rows 1–hr are left intact, and dmap
        # above was already built from the headers, so this is safe).
        for rng in list(ws.merged_cells.ranges):
            if rng.max_row > hr:
                ws.unmerge_cells(str(rng))
        for (s, mm, pod) in sorted((k for k in acc if k[0] == sheet),
                                   key=lambda k: (k[1], k[2])):
            a = acc[(s, mm, pod)]
            if (mm, pod) in dmap:
                ci, kind = dmap[(mm, pod)], 're-mark'
            else:
                ci, kind = nxt, 'NEW'; nxt += 1
                ws.cell(1, ci).value = _col_header(a['ymd'], pod)
                if hr == 2 and a['topic']: ws.cell(2, ci).value = a['topic']
                dmap[(mm, pod)] = ci
            em, pf, p10 = a['em'], a['pf'], a['p10']
            pres = tot = outside = 0
            for r in range(hr + 1, (ws.max_row or hr) + 1):
                # A POD session is only for that POD. Marking everyone else
                # 'Absent' is simply false - they were never invited - and it put
                # ten wrong Absents against every student in the downloadable
                # roster the moment a batch ran eleven PODs in a day.
                if pod and pc:
                    rp, _multi = pods.from_roster_cell(ws.cell(r, pc).value)
                    if (rp or pods.UNKNOWN) != pod:
                        # still worth knowing if they turned up
                        e0 = _cell_email(ws.cell(r, rm).value) if rm else ''
                        p0 = _cell_phone(ws.cell(r, rn).value) if rn else ''
                        if (e0 and e0 in em) or (p0 and p0 in pf):
                            outside += 1
                        continue
                if mode == 'exact':
                    e = _cell_email(ws.cell(r, rm).value) if rm else ''
                    p = _cell_phone(ws.cell(r, rn).value) if rn else ''
                    if not e and not p: continue
                    hit = (e and e in em) or (p and p in pf)
                else:
                    es = [x for x in (_cell_email(ws.cell(r, c).value) for c in (rm, bc) if c) if x]
                    ps = [x for x in (_cell_phone(ws.cell(r, c).value) for c in (rn, wa) if c) if x]
                    if not es and not ps: continue
                    hit = any(x in em for x in es) or any(len(x) >= 10 and x[-10:] in p10 for x in ps)
                tot += 1
                ws.cell(r, ci).value = 'Present' if hit else 'Absent'
                if hit: pres += 1
            # A "POD" session most of the batch attended is not a POD session -
            # either L2 mislabels a whole-batch session, or the domain is wrong.
            # Loudly, because the numbers look plausible either way.
            if pod and outside > max(20, pres):
                warnings.append(
                    f'{sheet} {a["ymd"]} "{pod}": {outside} attendee(s) matched '
                    f'students OUTSIDE that POD (vs {pres} inside) - is this really '
                    f'a POD session, or does L2 label a whole-batch session with a '
                    f'domain?')
            report.append(dict(batch=sheet, date=a['ymd'], col=get_column_letter(ci),
                               kind=kind, topic=a['topic'], pod=pod,
                               present=pres, total=tot, outside=outside))
    out = io.BytesIO(); wb.save(out)
    return out.getvalue(), report, warnings
