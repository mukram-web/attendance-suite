"""
site_build.py — render the static website from attendance.duckdb.

Output (site/, gitignored — it contains roster PII under /roster/):
  index.html            dashboard (public: aggregates only)
  day1.html             day-1 analysis (public: aggregates only)
  roster/index.html     per-student grid — served ONLY behind Basic Auth
  roster/data/B<n>.json one file per batch (cols + rows)
  middleware.js         Vercel Edge Middleware: HTTP Basic Auth on /roster*
  vercel.json           noindex everywhere; no-store on roster responses

The public pages carry no student contact data; everything under /roster/ is
gated by the middleware, whose credentials live in the Vercel project's
ROSTER_USER / ROSTER_PASS environment variables — never in this repo.

Runs standalone (python site_build.py) or from pipeline.py after the store
build. JSON is embedded into <script> blocks, so `</` is escaped — an unlucky
session title must not be able to close the tag.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dash_view                       # noqa: E402 — figures + fragments shared with the app

TEMPLATES = os.path.join(HERE, "site_templates")
DAY1_TEMPLATE = os.path.join(HERE, "day1_template.html")


def _plotly_js_path() -> str:
    """plotly.min.js shipped inside the installed plotly package — bundling it
    keeps the site self-contained and pinned to the exact version the app uses."""
    import plotly
    path = os.path.join(os.path.dirname(plotly.__file__), "package_data", "plotly.min.js")
    if not os.path.exists(path):
        raise FileNotFoundError(f"plotly.min.js not found at {path}")
    return path


def _script_safe(payload) -> str:
    """JSON safe to inline inside a <script> block: '</' would end the tag (and
    U+2028/29 are line terminators to old parsers)."""
    return (json.dumps(payload, ensure_ascii=False)
            .replace("</", "<\\/")
            .replace(" ", "\\u2028")
            .replace(" ", "\\u2029"))


def _read_template(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# Written as middleware.mjs — Vercel's Routing Middleware docs require a
# non-framework project to use either a .mjs extension or a package.json with
# "type": "module". We do BOTH (see PACKAGE_JSON), because this file is the only
# thing standing between /roster and 50k students' contact details.
MIDDLEWARE_JS = """\
// Vercel Edge Middleware — HTTP Basic Auth for everything under /roster.
// Credentials come from the project's environment variables (Settings ->
// Environment Variables): ROSTER_USER and ROSTER_PASS. Changing them there
// requires a redeploy to take effect. If they are unset, or anything below
// throws, the gate FAILS CLOSED rather than serving PII.
export const config = { matcher: ["/roster", "/roster/:path*"] };

// Base64 of the UTF-8 bytes. btoa() alone throws on any non-Latin-1 character,
// which would turn a stray accented password into a 500 on every request.
function b64utf8(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

function constantTimeEqual(a, b) {
  const enc = new TextEncoder();
  const ab = enc.encode(a), bb = enc.encode(b);
  const cb = ab.length === bb.length ? bb : ab;   // flat timing on length mismatch
  let diff = ab.length === bb.length ? 0 : 1;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ cb[i];
  return diff === 0;
}

const DENY = (status, body, headers) => new Response(body, { status, headers });

export default function middleware(request) {
  try {
    const user = process.env.ROSTER_USER, pass = process.env.ROSTER_PASS;
    if (!user || !pass) {
      return DENY(503, "Roster login is not configured on this deployment.");
    }
    const auth = request.headers.get("authorization") || "";
    if (constantTimeEqual(auth, "Basic " + b64utf8(user + ":" + pass))) {
      return;                                     // authorised -> serve the file
    }
  } catch (e) {
    return DENY(500, "Roster login check failed.");
  }
  return DENY(401, "Authentication required.", {
    "WWW-Authenticate": 'Basic realm="Be10X roster", charset="UTF-8"',
  });
}
"""

# No scripts and no dependencies, so Vercel runs no build — it exists only to
# declare the module type the middleware needs.
PACKAGE_JSON = {"private": True, "type": "module"}

VERCEL_JSON = {
    "headers": [
        {
            "source": "/(.*)",
            "headers": [
                {"key": "X-Robots-Tag", "value": "noindex, nofollow"},
                {"key": "Referrer-Policy", "value": "no-referrer"},
                {"key": "X-Content-Type-Options", "value": "nosniff"},
            ],
        },
        {
            # PII responses must never rest in a shared or browser cache.
            # The pattern covers bare /roster as well as /roster/anything.
            "source": "/roster(/.*)?",
            "headers": [{"key": "Cache-Control", "value": "private, no-store"}],
        },
    ]
}


def build_site(store_path: str, out_dir: str, log=print,
               include_roster: bool | None = None) -> dict:
    if include_roster is None:
        include_roster = (os.environ.get("PUBLISH_ROSTER", "").strip().lower()
                          in ("1", "true", "yes", "on"))
    con = duckdb.connect(store_path, read_only=True)
    meta = {k: json.loads(v)
            for k, v in con.execute("SELECT key, value FROM meta").fetchall()}

    # Start clean, but tolerate a file being held open (a local preview server,
    # an editor) — the important part is that nothing STALE survives, which is
    # asserted for the roster data below.
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    generated = meta.get("generated_at", "")

    # ── public: dashboard — the SAME front end the Streamlit tab renders ────
    # Figures come from dash_view's own builders (exported as Plotly JSON and
    # replayed by plotly.min.js) and the closing/sessions panels are the exact
    # HTML strings the app injects, so the two stay pixel-identical.
    DATA = meta["DATA"]
    batches_payload = {}
    for code, d in DATA.items():
        real = [s for s in d["sessions"] if not s.get("is_intro")] or d["sessions"]
        peak_s = max(real, key=lambda s: s["pct"])
        low_s = min(real, key=lambda s: s["pct"])
        batches_payload[code] = {
            "strength": d["strength"], "active": d["active"],
            "avg_pct": d["avg_pct"], "peak": d["peak"], "low": d["low"],
            "n_sessions": d["n_sessions"],
            "peak_lbl": peak_s["date_lbl"], "low_lbl": low_s["date_lbl"],
            # Formatted HERE so the site can't disagree with the app: Python's
            # %.0f rounds half-to-even, JS Math.round rounds half-up, and .5
            # values are routine (B23's peak is exactly 58.5 → 58 vs 59).
            "peak_txt": f"{d['peak']:.0f}", "low_txt": f"{d['low']:.0f}",
            "avg_txt": f"{d['avg_pct']:.1f}",
            "fig": json.loads(dash_view._date_line(d).to_json()),
            "closing_html": dash_view.closing_rows_html(d),
            "sessions_html": dash_view.sessions_table_html(d),
            "sessions_sub": dash_view.sessions_subtitle(d),
        }
    dash_payload = {
        "generated": generated,
        "summary": meta["summary"],
        "codes": list(DATA),
        "cmp": json.loads(dash_view._comparison_bar(DATA).to_json()),
        "batches": batches_payload,
    }
    dash_css = dash_view._CSS.replace("<style>", "").replace("</style>", "")
    _write(os.path.join(out_dir, "index.html"),
           _read_template(os.path.join(TEMPLATES, "dashboard.html"))
           .replace("__DASH_CSS__", dash_css)
           .replace("__DATA__", _script_safe(dash_payload)))

    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    shutil.copyfile(_plotly_js_path(), os.path.join(out_dir, "assets", "plotly.min.js"))

    # ── public: day-1 analysis (same template the Streamlit tab embeds) ────
    day1 = meta.get("day1") or {"batches": [], "skipped": [], "errors": []}
    day1_payload = dict(day1)
    day1_payload["generated"] = generated
    day1_payload["sheet_url"] = ""      # public page — never link the source sheet
    day1_payload["standalone"] = True   # show nav + in-page batch chips
    _write(os.path.join(out_dir, "day1.html"),
           _read_template(DAY1_TEMPLATE)
           .replace("__DATA__", _script_safe(day1_payload)))

    # ── restricted: roster grids ────────────────────────────────────────────
    # Publishing student contact details is OPT-IN. Until PUBLISH_ROSTER is set,
    # the deployment carries none, so the very first deploy can be used to prove
    # the login gate works (with a harmless canary) before any PII is uploaded.
    batches = meta.get("batches", [])
    if include_roster:
        for b in batches:
            df = con.execute(f'SELECT * FROM "grid_{b}"').df()
            payload = {
                "cols": list(df.columns),
                "rows": [[None if (isinstance(v, float) and v != v) else
                          (bool(v) if str(type(v).__name__) == "bool_" else v)
                          for v in row]
                         for row in df.itertuples(index=False, name=None)],
            }
            _write(os.path.join(out_dir, "roster", "data", f"{b}.json"),
                   json.dumps(payload, ensure_ascii=False, default=str))
    con.close()

    # Publishing off ⇒ there must be NO student data in the output, even if an
    # earlier run left some and the wipe above was blocked. Refuse to hand a
    # deployable folder to the uploader rather than ship PII that the operator
    # believes is switched off.
    data_dir = os.path.join(out_dir, "roster", "data")
    if not include_roster:
        shutil.rmtree(data_dir, ignore_errors=True)
        if os.path.isdir(data_dir) and os.listdir(data_dir):
            raise RuntimeError(
                f"PUBLISH_ROSTER is off but stale roster data is still present in "
                f"{data_dir} and could not be deleted (a file is open?). Refusing "
                f"to build the site — close whatever holds it and re-run.")

    # Always inside the gate: open /roster/canary.txt in a private window — if it
    # asks for the login, the gate works; if it shows this text, it does NOT.
    _write(os.path.join(out_dir, "roster", "canary.txt"),
           "If you can read this without being asked for a username and password,\n"
           "the /roster login gate is NOT working. Do not enable PUBLISH_ROSTER.\n")

    manifest = {"batches": batches if include_roster else [],
                "generated": generated, "enabled": bool(include_roster)}
    _write(os.path.join(out_dir, "roster", "index.html"),
           _read_template(os.path.join(TEMPLATES, "roster.html"))
           .replace("__MANIFEST__", _script_safe(manifest)))

    # ── platform files ─────────────────────────────────────────────────────
    _write(os.path.join(out_dir, "middleware.mjs"), MIDDLEWARE_JS)
    _write(os.path.join(out_dir, "package.json"), json.dumps(PACKAGE_JSON, indent=2))
    _write(os.path.join(out_dir, "vercel.json"),
           json.dumps(VERCEL_JSON, indent=2))

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(out_dir) for f in fs)
    stats = {"batches": len(batches) if include_roster else 0, "bytes": size,
             "roster_published": bool(include_roster)}
    log(f"   site: {size / 1e6:.1f} MB -> {out_dir}"
        + (f" (roster: {len(batches)} batches)" if include_roster
           else " (roster data NOT published - set PUBLISH_ROSTER=1 once the "
                "login gate is verified)"))
    return stats


if __name__ == "__main__":
    build_site(os.path.join(HERE, ".cache", "attendance.duckdb"),
               os.path.join(HERE, "site"))
