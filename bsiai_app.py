"""BSIAI attendance dashboard — standalone page over the BSIAI store.

The same pages the main app's **BSIAI** tab draws (`bsiai_view.py`), on their
own port, for working on the programme without the AI CAP app around it.
`bsiai_build.py` writes the store; this page only reads it.

Local by design (bound to 127.0.0.1 in .claude/launch.json, entry `bsiai-app`,
port 8520). It has NO password gate — the Roster page shows learner contact
details (masked by default) — so it must not be deployed as is; the main app's
BSIAI tab sits behind that app's gate and is the one to publish.

Where the store is looked for, in order: `$BSIAI_STORE`, `.cache/bsiai.duckdb`
beside this file, then `F:\\attendance_store\\bsiai_latest.duckdb`.
"""
from __future__ import annotations

import base64
import os
import pathlib
import sys

import streamlit as st

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bsiai_view                   # noqa: E402
import ui_theme as T                # noqa: E402


def _store_path() -> pathlib.Path | None:
    cands = [os.environ.get("BSIAI_STORE"), HERE / ".cache" / "bsiai.duckdb",
             pathlib.Path(r"F:\attendance_store\bsiai_latest.duckdb")]
    for c in cands:
        if c and pathlib.Path(c).exists():
            return pathlib.Path(c)
    return None


def _brand_title() -> None:
    """The wordmark beside the page name — the main app's header, one word changed."""
    dark = str(st.get_option("theme.base") or "light").lower() == "dark"
    path = HERE / str(T.BRAND.get("logo_dark" if dark else "logo_light") or "")
    if not path.exists():
        st.title("Be10X — BSIAI Attendance")
        return
    b64 = base64.b64encode(path.read_bytes()).decode()
    mime = "image/svg+xml" if path.suffix.lower() == ".svg" else "image/png"
    st.markdown(
        f'<div class="brandbar"><span class="mark"><img src="data:{mime};base64,{b64}" alt="be10X"></span>'
        f'<span class="t">BSIAI Attendance</span></div>', unsafe_allow_html=True)


_fav = HERE / str(T.BRAND.get("favicon") or "")
st.set_page_config(page_title="BSIAI Attendance", page_icon=str(_fav) if _fav.exists() else "📊",
                   layout="wide", initial_sidebar_state="collapsed")
T.inject_css()
_brand_title()

_path = _store_path()
if _path is None:
    st.error("No BSIAI store found. Build one with `.venv\\Scripts\\python.exe bsiai_build.py`.")
    st.stop()
store = bsiai_view.load_store(str(_path), _path.stat().st_mtime)


@st.fragment
def _page():
    bsiai_view.render(store, key="bsiai")


_page()
