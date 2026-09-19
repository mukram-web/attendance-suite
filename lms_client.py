"""The 10xstats LMS API — enrolment data, replacing the Master Batch Rosters Sheet.

Two endpoints, both GET, both authenticated with an `X-API-Key` header:

    /batches                  every batch the LMS knows, with a customer count
    /customers?batchId=<id>   every customer in one batch

What was measured against the live API (2026-09-11 and 2026-09-15) and is
therefore designed around rather than discovered again:

* **There is no pagination.** `?limit` and `?page` are accepted and ignored; a
  6,000-customer batch comes back as one payload. So a request is all-or-nothing
  and the only lever on cost is *which* batches we ask for.
* **Large batches return 504.** 43 of 368 batches (40k-73k customers each, all
  non-CAP workshops) never succeeded across three retry passes. Retries DO help
  on mid-size batches — 31 of 74 initial failures recovered — so the timeout is
  load-dependent, not a hard size ceiling. Every CAP batch reads fine. A client
  without retry silently gets nothing, which is why `fetch_customers` retries by
  default and raises rather than returning an empty list on final failure.
* **No browser can call this API.** No `Access-Control-Allow-Origin` is returned
  for any origin, so anything running in a page fails with an opaque network
  error. Server-side only.

The key is read from the environment or a `.env`-style file OUTSIDE this repo —
this repository is public. It is never logged, never echoed, and never put in a
URL query string.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

BASE = "https://10xstats.com/api/v1/lms"

# Generous: a mid-size batch legitimately takes a minute or more to serialise.
# The failure we are guarding against is a hung socket, not a slow-but-working
# request, and cutting a slow success short just converts it into a retry.
TIMEOUT = 900

RETRIES = 4
BACKOFF = 20          # seconds, multiplied by the attempt number

# Where the key lives when it is not in the environment. Deliberately the PARENT
# of this repo: the repo is public, and .gitignore now refuses *.env as a second
# line of defence, but the file should not be here in the first place.
_KEY_FILE = pathlib.Path(__file__).resolve().parent.parent / "lms_api.env"
_KEY_VAR = "LMS_API_KEY"


def api_key(explicit: str = "") -> str:
    """The API key, from an explicit value, the environment, then the key file.

    Raises rather than returning empty: a missing key produces a 401 on every
    request, and a loop of 401s dressed up as "no data" is the kind of failure
    that publishes an empty dashboard.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get(_KEY_VAR, "").strip()
    if env:
        return env
    if _KEY_FILE.exists():
        for line in _KEY_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{_KEY_VAR}="):
                got = line.split("=", 1)[1].strip().strip('"').strip("'")
                if got:
                    return got
    raise RuntimeError(
        f"No LMS API key. Set {_KEY_VAR} in the environment, pass it in the "
        f"config, or put it in {_KEY_FILE.name} beside the repo.")


def _headers(key: str) -> dict:
    return {"X-API-Key": key, "Accept": "application/json"}


def _get(path: str, key: str, params: dict | None = None,
         retries: int = RETRIES, log=None) -> dict:
    """One GET with retry and linear backoff. Returns the decoded body.

    Never includes the key in anything it logs or raises — the message quotes the
    path and status only.
    """
    import requests

    last = ""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(f"{BASE}{path}", params=params or None,
                             headers=_headers(key), timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            # 4xx other than 429 will not fix themselves; stop wasting minutes.
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"GET {path} -> {last}")
        except RuntimeError:
            raise
        except Exception as e:                      # timeout, reset, bad JSON
            last = f"{type(e).__name__}: {str(e)[:160]}"
        if attempt < retries:
            if log:
                log(f"      {path} {last} — retry {attempt}/{retries - 1}")
            time.sleep(BACKOFF * attempt)
    raise RuntimeError(f"GET {path} failed after {retries} attempts ({last})")


def fetch_batches(key: str, log=None) -> list[dict]:
    """Every batch. One small request; this endpoint has never been seen to fail."""
    return _get("/batches", key, log=log).get("data") or []


def fetch_customers(key: str, batch_id: str, log=None) -> list[dict]:
    """Every customer in one batch. Raises if it cannot be read — see module docstring."""
    data = _get("/customers", key, {"batchId": batch_id}, log=log).get("data") or {}
    return data.get("customers") or []


# ─────────────────────────── disk cache ──────────────────────────────────────
# A batch's payload is large and the endpoint is slow, so a local run that is
# iterating on the BUILD should not re-download the same rows. Keyed on the
# batch id, and written only when a directory is supplied, so the weekly job
# (which wants fresh data and has no persistent disk) never uses it.

def cached_customers(key: str, batch_id: str, cache_dir: str | None,
                     log=None) -> list[dict]:
    if not cache_dir:
        return fetch_customers(key, batch_id, log=log)
    d = pathlib.Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"customers_{batch_id}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass                                    # corrupt cache -> refetch
    got = fetch_customers(key, batch_id, log=log)
    try:
        f.write_text(json.dumps(got), encoding="utf-8")
    except Exception:
        pass                                        # cache is an optimisation
    return got
