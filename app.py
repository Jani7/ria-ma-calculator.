"""
RIA M&A Calculator — Streamlit Application
Buyer-side economics for acquiring a Registered Investment Advisor.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from calculations import (
    compute_eboc, implied_multiples, build_pro_forma,
    compute_irr_and_returns, build_loan_amortization,
    build_seller_note_amortization, compute_dscr,
    compute_valuation_band, auto_replacement_cost,
    suggest_deal_structure,
    compute_earnout_scenarios, compute_seller_total_proceeds,
    sensitivity_irr, sensitivity_breakeven,
    compute_synergy_schedule,
    lookup_comps_band,
)
import sec_lookup
import html as _html
import tempfile
import threading
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

# Where the runtime-downloaded parquet lives. /tmp on Streamlit Cloud is
# writable and persists for the container's lifetime (cold starts re-fetch).
_ADV_CACHE_PATH = Path(tempfile.gettempdir()) / "ria_adv_cache" / "adv_current.parquet"

# Local dev fallback — gitignored, populated by scripts/build_adv_data.py.
_ADV_LOCAL_PATH = Path(__file__).parent / "data" / "adv_current.parquet"


@st.cache_resource(show_spinner="Fetching SEC ADV data…")
def _resolve_adv() -> dict:
    """Resolve the parquet location, downloading from a private GitHub repo
    if no local copy exists.

    Returns a dict with:
        "path":   Path | None      — the parquet file, or None if unavailable
        "status": str               — short code for the UI to render (PAT-safe)

    Resolution order:
        1. Local dev file at data/adv_current.parquet (gitignored)
        2. /tmp cache from a prior fetch in this container
        3. Private GitHub repo at branch HEAD via the Contents API,
           authenticated with secret ADV_DATA_TOKEN. Optional secrets:
           ADV_DATA_REPO (default "Jani7/ria-ma-data"),
           ADV_DATA_PATH (default "adv_current.parquet"),
           ADV_DATA_REF  (default "main").
        4. None — UI shows "data unavailable" caption.

    The status code is intentionally minimal (e.g. "http_403") — no token,
    URL, or exception text is ever stored, so this dict is safe to log.
    """
    def _is_valid_parquet(p: Path) -> bool:
        """A parquet file starts and ends with the 4-byte 'PAR1' magic."""
        try:
            with open(p, "rb") as f:
                head = f.read(4)
                f.seek(-4, 2)
                tail = f.read(4)
            return head == b"PAR1" and tail == b"PAR1"
        except OSError:
            return False

    if _ADV_LOCAL_PATH.exists() and _is_valid_parquet(_ADV_LOCAL_PATH):
        return {"path": _ADV_LOCAL_PATH, "status": "local"}
    if _ADV_CACHE_PATH.exists():
        if _is_valid_parquet(_ADV_CACHE_PATH):
            return {"path": _ADV_CACHE_PATH, "status": "cache"}
        # Stale/bad bytes from a prior failed fetch — discard and re-fetch.
        try:
            _ADV_CACHE_PATH.unlink()
        except OSError:
            pass

    token = None
    repo = "Jani7/ria-ma-data"
    file_path = "adv_current.parquet"
    ref = "main"
    try:
        if "ADV_DATA_TOKEN" in st.secrets:
            token = st.secrets["ADV_DATA_TOKEN"]
        if "ADV_DATA_REPO" in st.secrets:
            repo = st.secrets["ADV_DATA_REPO"]
        if "ADV_DATA_PATH" in st.secrets:
            file_path = st.secrets["ADV_DATA_PATH"]
        if "ADV_DATA_REF" in st.secrets:
            ref = st.secrets["ADV_DATA_REF"]
    except Exception:
        # st.secrets may not be available in some local-dev contexts.
        pass

    # Validate operator-controlled secret strings before splicing them into
    # the GitHub Contents API URL. These are *operator* values (set on
    # Streamlit Cloud), not user input, so this isn't an SSRF mitigation —
    # it's a footgun guard: a stray space or backslash in the secret would
    # otherwise build a broken URL or, in a contrived case, redirect the
    # fetch to a different repo/branch.
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}", repo):
        return {"path": None, "status": "bad_config"}
    if not _re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", file_path):
        return {"path": None, "status": "bad_config"}
    if not _re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", ref):
        return {"path": None, "status": "bad_config"}

    if not token:
        return {"path": None, "status": "no_token"}

    try:
        # Contents API with Accept: application/vnd.github.raw returns the
        # file bytes directly — no separate asset/blob download step.
        api = f"https://api.github.com/repos/{repo}/contents/{file_path}?ref={ref}"
        req = urllib.request.Request(
            api,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.raw",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ria-ma-calculator",
            },
        )
        _ADV_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
        _ADV_CACHE_PATH.write_bytes(body)
        # Verify we got a real parquet, not an HTML error page or JSON wrapper.
        # Parquet starts and ends with the 4-byte "PAR1" magic.
        if not (len(body) > 8 and body[:4] == b"PAR1" and body[-4:] == b"PAR1"):
            return {"path": None, "status": "bad_response"}
        return {"path": _ADV_CACHE_PATH, "status": "fetched"}
    except urllib.error.HTTPError as e:
        # Record only the HTTP status — no body, no headers, no URL.
        return {"path": None, "status": f"http_{e.code}"}
    except urllib.error.URLError:
        return {"path": None, "status": "network_error"}
    except OSError:
        return {"path": None, "status": "io_error"}
    except Exception:
        # Catch-all so exotic exceptions don't bubble to Streamlit's error
        # overlay (which would surface the private repo name from the f-string).
        return {"path": None, "status": "unknown_error"}


def _ensure_adv_data_path():
    return _resolve_adv()["path"]


def _adv_resolution_status() -> str:
    return _resolve_adv()["status"]


@st.cache_data(show_spinner=False)
def _load_adv_df_by_path(path_str: str):
    """Cached by path string so the cache invalidates when the resolved
    path changes (e.g. None → /tmp/... after a fix to the secret/PAT)."""
    if not path_str:
        return sec_lookup.load_adv_data(None)
    return sec_lookup.load_adv_data(Path(path_str))


def _load_adv_df():
    p = _ensure_adv_data_path()
    return _load_adv_df_by_path(str(p) if p else "")


# -- Comps database ------------------------------------------------------------
_COMPS_CSV_PATH = Path(__file__).parent / "data" / "ria_ma_comps.csv"


@st.cache_data(show_spinner=False)
def _load_comps_df():
    """Load the manually curated RIA M&A transaction database.

    Returns an empty DataFrame (with the expected columns) when the file is
    missing, so the rest of the app keeps rendering. The file ships in-repo
    at data/ria_ma_comps.csv; refresh quarterly per scripts/seed_comps.py.
    """
    cols = [
        "date", "buyer", "seller", "seller_aum",
        "ev_revenue_multiple", "ev_ebitda_multiple",
        "aum_tier", "recurring_tier", "channel", "source_url", "notes",
    ]
    if not _COMPS_CSV_PATH.exists():
        return pd.DataFrame(columns=cols)
    try:
        df = pd.read_csv(_COMPS_CSV_PATH)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        for c in ("seller_aum", "ev_revenue_multiple", "ev_ebitda_multiple"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame(columns=cols)


# -- Page config ---------------------------------------------------------------
st.set_page_config(page_title="RIA M&A Calculator", page_icon="📊", layout="wide")

# Single dark theme — the runtime toggle was removed (May 2026) after a string
# of bugs where Streamlit's --primary-color/--background-color variables fought
# our injected CSS. The base palette now lives in .streamlit/config.toml so
# Streamlit's own chrome (tooltips, dropdowns, date pickers) is consistent;
# the CSS below only adds polish on top — no `!important` overrides needed
# to fix a competing light theme. If a future contributor wants light mode
# back, restore it as a separate stylesheet — do NOT layer it on top of this.
#
# Palette (Linear/Vercel-inspired):
#   --bg          #0b0d12   page background
#   --panel       #12151c   cards, tabs, sidebar inputs
#   --panel-hi    #1a1f2b   hover/active state
#   --border      #232938   1px borders
#   --border-hi   #2f3646   stronger borders / dividers
#   --text        #e5e9f0   primary text
#   --muted       #8b94a8   secondary text
#   --subtle      #6b7385   tertiary text
#   --accent      #7c8cff   indigo primary (buttons, focus rings)
#   --accent-hi   #95a3ff   accent hover
#   --positive    #4ade80   green
#   --warning     #fbbf24   amber
#   --negative    #f87171   red
_THEME_CSS = """
<style>
    /* ---- Web fonts ----------------------------------------------------- */
    /* Inter for body + headings; IBM Plex Mono for digit-aligned inputs.
       display=swap so first paint uses system fallback rather than blocking. */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

    /* ---- Typography ---------------------------------------------------- */
    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
        -webkit-font-smoothing: antialiased;
        font-feature-settings: "ss01", "cv11", "tnum", "lnum";
    }
    .stApp { background-color: #0b0d12; color: #e5e9f0; }

    /* ---- Hide Streamlit's own header chrome (cleaner first paint) ----- */
    header[data-testid="stHeader"] {
        background: transparent;
    }
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }

    /* ---- Sidebar ------------------------------------------------------- */
    section[data-testid="stSidebar"] > div {
        background-color: #0e1118;
        border-right: 1px solid #1c2230;
        padding-top: 12px;
    }
    section[data-testid="stSidebar"] hr {
        margin: 14px 0 12px 0;
        border-color: #1c2230;
    }
    section[data-testid="stSidebar"] h3 {
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #8b94a8;
        margin: 4px 0 10px 0;
    }
    section[data-testid="stSidebar"] label p {
        font-size: 0.82rem;
        color: #c5cad6;
        margin-bottom: 4px;
    }
    section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p,
    section[data-testid="stSidebar"] .stCaption {
        font-size: 0.72rem;
        color: #6b7385;
        line-height: 1.45;
    }
    /* Tighter spacing between sidebar widgets so we can fit more density
       without it feeling crammed. */
    section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] > div {
        gap: 0.45rem;
    }

    /* ---- Inputs (text, number, select) -------------------------------- */
    .stTextInput input,
    .stNumberInput input,
    div[data-baseweb="input"] input,
    div[data-baseweb="select"] > div {
        background-color: #12151c !important;
        border: 1px solid #232938 !important;
        border-radius: 6px !important;
        color: #e5e9f0 !important;
        transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }
    .stTextInput input:focus,
    .stNumberInput input:focus,
    div[data-baseweb="input"]:focus-within,
    div[data-baseweb="select"] > div:focus-within {
        border-color: #7c8cff !important;
        box-shadow: 0 0 0 3px rgba(124,140,255,0.15) !important;
        outline: none !important;
    }
    section[data-testid="stSidebar"] .stTextInput input {
        font-family: 'IBM Plex Mono', ui-monospace, 'SF Mono', 'Consolas', 'Menlo', monospace;
        font-size: 0.88rem;
        font-feature-settings: "tnum", "zero";
    }
    /* Tooltip — constrain width so the help-icon popover doesn't overflow
       on narrow viewports / sidebar. */
    div[role="tooltip"] {
        max-width: 280px !important;
        white-space: normal !important;
        word-wrap: break-word !important;
        background: #1a1f2b !important;
        border: 1px solid #2f3646 !important;
        color: #e5e9f0 !important;
    }

    /* ---- Sliders ------------------------------------------------------- */
    /* Streamlit's slider track and thumb don't pick up the theme accent
       reliably across versions — pin them explicitly. */
    .stSlider [data-baseweb="slider"] [role="slider"] {
        background-color: #7c8cff !important;
        border-color: #7c8cff !important;
    }
    .stSlider [data-baseweb="slider"] > div > div > div {
        background-color: #7c8cff !important;
    }

    /* ---- Buttons ------------------------------------------------------- */
    .stButton > button {
        border-radius: 6px;
        font-weight: 500;
        font-size: 0.88rem;
        padding: 0.45rem 0.95rem;
        transition: all 0.15s ease;
        border: 1px solid #232938;
        background-color: #12151c;
        color: #e5e9f0;
    }
    .stButton > button:hover {
        background-color: #1a1f2b;
        border-color: #2f3646;
        color: #ffffff;
    }
    .stButton > button[kind="primary"] {
        background-color: #7c8cff;
        color: #0b0d12;
        border: 1px solid #7c8cff;
        font-weight: 600;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #95a3ff;
        border-color: #95a3ff;
        color: #0b0d12;
        box-shadow: 0 4px 14px rgba(124,140,255,0.30);
    }
    .stDownloadButton > button {
        border-radius: 6px;
        background-color: #12151c;
        border: 1px solid #2f3646;
        color: #e5e9f0;
    }
    .stDownloadButton > button:hover {
        background-color: #1a1f2b;
        border-color: #7c8cff;
        color: #ffffff;
    }

    /* ---- Tabs ---------------------------------------------------------- */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        border-bottom: 1px solid #1c2230;
        margin-bottom: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: transparent;
        border-radius: 6px 6px 0 0;
        padding: 10px 16px;
        color: #8b94a8;
        font-size: 0.88rem;
        font-weight: 500;
        border: 1px solid transparent;
        border-bottom: none;
        transition: color 0.15s ease, background-color 0.15s ease;
    }
    .stTabs [data-baseweb="tab"]:hover {
        color: #e5e9f0;
        background-color: rgba(124,140,255,0.06);
    }
    .stTabs [aria-selected="true"] {
        background-color: #12151c;
        color: #ffffff;
        border-color: #1c2230;
        position: relative;
    }
    .stTabs [aria-selected="true"]::after {
        content: "";
        position: absolute;
        left: 12px;
        right: 12px;
        bottom: -1px;
        height: 2px;
        background: #7c8cff;
        border-radius: 1px;
    }

    /* ---- Metric cards (the top of Deal Summary) ----------------------- */
    .metric-card {
        background: #12151c;
        border: 1px solid #1c2230;
        border-radius: 10px;
        padding: 14px 14px 16px 14px;
        margin: 0;
        min-height: 92px;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        transition: border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;
    }
    .metric-card:hover {
        border-color: #2f3646;
        transform: translateY(-1px);
        box-shadow: 0 6px 16px rgba(0,0,0,0.25);
    }
    .metric-card h3 {
        color: #6b7385;
        font-size: 0.66rem;
        font-weight: 600;
        margin: 0 0 8px 0;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .metric-card h2 {
        color: #ffffff;
        font-size: 1.55rem;
        font-weight: 600;
        margin: 0;
        white-space: nowrap;
        letter-spacing: -0.015em;
        font-feature-settings: "tnum", "lnum", "zero";
        font-variant-numeric: tabular-nums lining-nums;
    }
    .metric-card .positive { color: #4ade80; }
    .metric-card .negative { color: #f87171; }
    .metric-card .neutral  { color: #ffffff; }
    .metric-card .accent   { color: #95a3ff; }

    /* ---- Section headers ---------------------------------------------- */
    .section-header {
        color: #e5e9f0;
        margin: 28px 0 14px 0;
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.10em;
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .section-header::before {
        content: "";
        width: 3px;
        height: 14px;
        background: #7c8cff;
        border-radius: 2px;
    }

    /* ---- Streamlit alert callouts ------------------------------------- */
    div[data-testid="stAlert"] {
        background-color: #12151c;
        border: 1px solid #1c2230;
        border-radius: 8px;
        color: #e5e9f0;
    }
    div[data-testid="stAlert"][data-baseweb="notification"]:has(svg[data-testid="stIconWarning"]),
    div[data-testid="stAlert"]:has([data-baseweb="icon"][data-testid="stIcon"]) {
        /* warning state uses a left accent — see narrower selector below */
    }
    /* Targeted warning, info, error tints — Streamlit doesn't expose a
       clean variant class, so we lean on the icon's title for matching. */
    div[data-testid="stAlertContentWarning"] { color: #e5e9f0 !important; }
    div[data-testid="stAlertContentInfo"]    { color: #e5e9f0 !important; }
    div[data-testid="stAlertContentSuccess"] { color: #e5e9f0 !important; }
    div[data-testid="stAlertContentError"]   { color: #e5e9f0 !important; }

    /* ---- DataFrames --------------------------------------------------- */
    .stDataFrame, [data-testid="stDataFrame"] {
        border: 1px solid #1c2230;
        border-radius: 8px;
        overflow: hidden;
    }
    /* Tabular figures inside data tables — column digits stack consistently
       so the eye reads down a column instead of zig-zagging. */
    .stDataFrame [role="gridcell"],
    [data-testid="stDataFrame"] [role="gridcell"],
    .stDataFrame [role="grid"],
    [data-testid="stDataFrame"] [role="grid"] {
        font-feature-settings: "tnum", "lnum", "zero";
        font-variant-numeric: tabular-nums lining-nums;
    }

    /* ---- Dialogs (reconciliation modal) ------------------------------- */
    div[role="dialog"] {
        background-color: #12151c !important;
        border: 1px solid #232938 !important;
        border-radius: 12px !important;
    }
    div[role="dialog"] h2,
    div[role="dialog"] [data-testid="stMarkdownContainer"] h2 {
        /* Long firm names overflow the default title — clamp to two lines
           with ellipsis, smaller font. */
        font-size: 1.1rem !important;
        font-weight: 600 !important;
        line-height: 1.35 !important;
        margin: 0 0 8px 0 !important;
        display: -webkit-box !important;
        -webkit-line-clamp: 2 !important;
        -webkit-box-orient: vertical !important;
        overflow: hidden !important;
        word-break: break-word !important;
    }

    /* ---- Radio rows inside the reconciliation dialog ------------------ */
    div[role="dialog"] .stRadio > div {
        background-color: #0e1118;
        border: 1px solid #1c2230;
        border-radius: 6px;
        padding: 6px 10px;
    }
    div[role="dialog"] .stRadio label {
        font-size: 0.84rem;
    }

    /* ---- Site footer -------------------------------------------------- */
    .site-footer {
        border-top: 1px solid #1c2230;
        margin: 56px auto 0 auto;
        padding: 22px 12px 32px 12px;
        text-align: center;
        color: #6b7385;
        font-size: 0.76rem;
        line-height: 1.6;
        max-width: 880px;
    }
    .site-footer a { color: #95a3ff; text-decoration: none; }
    .site-footer a:hover { color: #b6c0ff; text-decoration: underline; }
    .site-footer .footer-meta {
        color: #4d5566;
        margin-top: 10px;
        font-size: 0.72rem;
    }

    /* ---- Mega-RIA warning banner ------------------------------------- */
    .mega-banner {
        background: linear-gradient(180deg, rgba(251,191,36,0.08) 0%, rgba(251,191,36,0.04) 100%);
        border: 1px solid rgba(251,191,36,0.35);
        border-left: 3px solid #fbbf24;
        border-radius: 8px;
        padding: 12px 16px;
        margin: 8px 0 16px 0;
        color: #e5e9f0;
        font-size: 0.86rem;
        line-height: 1.55;
    }
    .mega-banner strong { color: #fbbf24; }

    /* ---- Mobile / narrow viewport ------------------------------------ */
    @media (max-width: 640px) {
        .metric-card { min-height: 78px; padding: 10px; }
        .metric-card h2 { font-size: 1.2rem; }
        .section-header { font-size: 0.72rem; }
        .stTabs [data-baseweb="tab"] { padding: 8px 10px; font-size: 0.8rem; }
    }
</style>
"""

st.markdown(_THEME_CSS, unsafe_allow_html=True)


def render_site_footer():
    """Render the site footer. Single dark-theme styling lives in the
    .site-footer rule in the global stylesheet."""
    sec_url = (
        "https://www.sec.gov/data-research/sec-markets-data/"
        "information-about-registered-investment-advisers-exempt-reporting-advisers"
    )
    st.markdown(
        f"""
        <div class="site-footer">
            RIA M&amp;A Calculator &middot;
            Source: <a href="{sec_url}" target="_blank" rel="noopener">SEC Form ADV</a><br>
            All information is derived from publicly available SEC Form ADV filings and is
            provided for informational purposes only. Firm data and other metrics are
            inferred from filing records and are not guaranteed to be accurate or complete.
            Nothing on this site constitutes investment, legal, or professional advice and
            should not be used as the basis for any decision.
            <div class="footer-meta">
                Built by Dhruv Jani &middot; &copy; 2026 Dhruv Jani &middot;
                Site design, code, and original analysis protected by copyright.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# -- Session state -------------------------------------------------------------
if "show_calculator" not in st.session_state:
    st.session_state.show_calculator = False
if "sec_data" not in st.session_state:
    st.session_state.sec_data = None  # sec_lookup.FirmData or None
if "pending_reconcile" not in st.session_state:
    st.session_state.pending_reconcile = False
if "pending_apply" not in st.session_state:
    st.session_state.pending_apply = {}  # {widget_key: value}
if "sec_lookup_history" not in st.session_state:
    st.session_state.sec_lookup_history = []  # epoch seconds of past loads

# Per-session rate limit for SEC lookups. The bulk Form ADV data is itself
# public (we just preprocess it), so this isn't about gating access — it's
# about making automated harvesting of the live UI uneconomical. A human
# evaluating M&A targets won't bump up against 25/30min.
SEC_LOOKUP_LIMIT = 25
SEC_LOOKUP_WINDOW_SEC = 30 * 60

# Module-level IP -> [epoch seconds] history, guarded by a lock so concurrent
# Streamlit script reruns (separate threads in the same process) don't race.
# Used in addition to the per-session history so a single client can't open
# multiple tabs/sessions to bypass the cap.
_IP_LOOKUP_HISTORY: dict[str, list[float]] = {}
_IP_LOOKUP_LOCK = threading.Lock()


def _client_ip() -> "str | None":
    """Best-effort client IP from the X-Forwarded-For header.

    Streamlit ≥1.31 exposes request headers via st.context.headers. Returns
    None if the header is missing or the runtime doesn't support it — callers
    should then fall back to session-only quota."""
    try:
        headers = st.context.headers  # type: ignore[attr-defined]
    except Exception:
        return None
    if not headers:
        return None
    xff = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
    if not xff:
        return None
    first = xff.split(",", 1)[0].strip()
    return first or None


def _ip_quota(ip: str) -> tuple[int, int]:
    """Returns (remaining_for_ip, seconds_until_oldest_expires) for the given IP.

    Prunes the history list in place under the module lock."""
    import time
    now = time.time()
    with _IP_LOOKUP_LOCK:
        hist = [t for t in _IP_LOOKUP_HISTORY.get(ip, []) if now - t < SEC_LOOKUP_WINDOW_SEC]
        _IP_LOOKUP_HISTORY[ip] = hist
        used = len(hist)
        remaining = max(0, SEC_LOOKUP_LIMIT - used)
        wait = 0
        if used >= SEC_LOOKUP_LIMIT:
            wait = int(SEC_LOOKUP_WINDOW_SEC - (now - min(hist)))
    return remaining, wait


def _sec_lookup_quota() -> tuple[bool, int, int]:
    """Returns (allowed, remaining, seconds_until_oldest_expires).

    Combined check: the binding quota is the lower of the session-keyed and
    (when available) the IP-keyed counters. If X-Forwarded-For is absent we
    fall through to session-only — this avoids accidentally permitting more
    than the cap on dev/local runs that lack a proxy header."""
    import time
    now = time.time()
    st.session_state.sec_lookup_history = [
        t for t in st.session_state.sec_lookup_history
        if now - t < SEC_LOOKUP_WINDOW_SEC
    ]
    used = len(st.session_state.sec_lookup_history)
    sess_remaining = max(0, SEC_LOOKUP_LIMIT - used)
    sess_wait = 0
    if used >= SEC_LOOKUP_LIMIT:
        sess_wait = int(SEC_LOOKUP_WINDOW_SEC - (now - min(st.session_state.sec_lookup_history)))

    ip = _client_ip()
    if ip is None:
        return sess_remaining > 0, sess_remaining, sess_wait

    ip_remaining, ip_wait = _ip_quota(ip)
    # Binding constraint is whichever is tighter.
    remaining = min(sess_remaining, ip_remaining)
    wait = max(sess_wait, ip_wait) if remaining == 0 else 0
    return remaining > 0, remaining, wait


def _record_sec_lookup():
    import time
    now = time.time()
    st.session_state.sec_lookup_history.append(now)
    ip = _client_ip()
    if ip is not None:
        with _IP_LOOKUP_LOCK:
            _IP_LOOKUP_HISTORY.setdefault(ip, []).append(now)


def _queue_apply(widget_key: str, value):
    """Stage a value to be written into a widget's session_state on the next
    rerun. We do this in a queue rather than writing directly because Streamlit
    forbids mutating a widget's session_state value after the widget has rendered."""
    st.session_state.pending_apply[widget_key] = value


def _apply_pending():
    """Flush queued values into widget session_state. Call BEFORE rendering
    the affected widgets."""
    for key, value in list(st.session_state.pending_apply.items()):
        st.session_state[key] = value
    st.session_state.pending_apply.clear()

def plotly_layout() -> dict:
    """Return the Plotly layout kwargs for the dark theme. Function (not
    constant) so calling sites can still pass it as **kwargs; kept as a
    function in case we ever reintroduce theme variants."""
    # NB: callers pass their own `title=...` kwarg via update_layout, so we
    # must NOT include `title` here (would collide with multiple kwargs).
    # Plotly's default title font color follows the figure font color set
    # below — same effect with no kwarg collision.
    return dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(18,21,28,0.6)",
        font=dict(color="#e5e9f0", size=12, family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"),
        xaxis=dict(gridcolor="#1c2230", zerolinecolor="#232938", linecolor="#232938"),
        yaxis=dict(gridcolor="#1c2230", zerolinecolor="#232938", linecolor="#232938"),
        margin=dict(l=40, r=40, t=50, b=40),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color="#c5cad6")),
    )

# Indigo / green / amber / red / violet / cyan — matches the .metric-card colors
# and the CSS accent system in _THEME_CSS.
COLORS = ["#7c8cff", "#4ade80", "#fbbf24", "#f87171", "#c084fc", "#67e8f9"]


# -- Helpers -------------------------------------------------------------------
def fmt_dollar(val):
    if abs(val) >= 1e9:
        return f"${val/1e9:,.1f}B"
    if abs(val) >= 1e6:
        return f"${val/1e6:,.1f}M"
    if abs(val) >= 1e3:
        return f"${val/1e3:,.0f}K"
    return f"${val:,.0f}"


def fmt_pct(val):
    return f"{val*100:.1f}%"


def metric_card(label, value, css_class="neutral"):
    return f"""
    <div class="metric-card">
        <h3>{label}</h3>
        <h2 class="{css_class}">{value}</h2>
    </div>
    """


def render_welcome_page():
    """Render the welcome/landing page with the sidebar hidden. Dark-theme only."""
    st.markdown("""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        html, body, .stApp {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
            -webkit-font-smoothing: antialiased;
            font-feature-settings: "ss01", "cv11";
        }
        [data-testid="stSidebar"] { display: none; }
        [data-testid="stSidebarCollapsedControl"] { display: none; }
        .block-container { padding-top: 3.5rem !important; }

        .welcome-container {
            max-width: 940px;
            margin: 0 auto;
            padding: 8px 20px 40px 20px;
        }
        .welcome-eyebrow {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 5px 12px;
            background: rgba(124,140,255,0.10);
            border: 1px solid rgba(124,140,255,0.30);
            border-radius: 999px;
            color: #95a3ff;
            font-size: 0.72rem;
            font-weight: 600;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 20px;
        }
        .welcome-eyebrow .dot {
            width: 6px; height: 6px; border-radius: 50%;
            background: #7c8cff; box-shadow: 0 0 0 3px rgba(124,140,255,0.20);
        }
        .welcome-header {
            text-align: center;
            margin-bottom: 56px;
        }
        .welcome-header h1 {
            color: #ffffff;
            font-size: 3.2rem;
            font-weight: 700;
            letter-spacing: -0.03em;
            margin: 0 0 16px 0;
            line-height: 1.05;
            background: linear-gradient(180deg, #ffffff 0%, #c5cad6 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        @media (max-width: 640px) {
            .welcome-header h1 { font-size: 2.2rem; }
        }
        .welcome-header .subtitle {
            color: #8b94a8;
            font-size: 1.08rem;
            font-weight: 400;
            line-height: 1.6;
            max-width: 580px;
            margin: 0 auto;
        }
        .welcome-header .subtitle strong { color: #e5e9f0; font-weight: 500; }

        .feature-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
            margin-bottom: 48px;
        }
        @media (max-width: 768px) {
            .feature-grid { grid-template-columns: repeat(2, 1fr); }
        }
        @media (max-width: 480px) {
            .feature-grid { grid-template-columns: 1fr; }
        }
        @keyframes cardFadeUp {
            from { opacity: 0; transform: translateY(12px); }
            to   { opacity: 1; transform: translateY(0); }
        }
        .feature-card {
            background: #12151c;
            border: 1px solid #1c2230;
            border-radius: 10px;
            padding: 20px 18px;
            transition: transform 0.20s ease, border-color 0.20s ease, box-shadow 0.20s ease;
            animation: cardFadeUp 0.45s ease both;
        }
        .feature-card:nth-child(1) { animation-delay: 0.02s; }
        .feature-card:nth-child(2) { animation-delay: 0.06s; }
        .feature-card:nth-child(3) { animation-delay: 0.10s; }
        .feature-card:nth-child(4) { animation-delay: 0.14s; }
        .feature-card:nth-child(5) { animation-delay: 0.18s; }
        .feature-card:nth-child(6) { animation-delay: 0.22s; }
        .feature-card:hover {
            transform: translateY(-2px);
            border-color: #2f3646;
            box-shadow: 0 10px 26px rgba(0,0,0,0.30);
        }
        .feature-icon {
            width: 30px;
            height: 30px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.88rem;
            margin-bottom: 14px;
            background: rgba(124,140,255,0.10);
            color: #95a3ff;
        }
        .feature-card h3 {
            color: #ffffff;
            font-size: 0.92rem;
            font-weight: 600;
            margin: 0 0 6px 0;
        }
        .feature-card p {
            color: #8b94a8;
            font-size: 0.82rem;
            line-height: 1.55;
            margin: 0;
        }

        .cta-row {
            display: flex;
            justify-content: center;
            gap: 12px;
            margin-bottom: 28px;
        }
        .cta-hint {
            text-align: center;
            color: #6b7385;
            font-size: 0.78rem;
            margin-bottom: 32px;
        }
        .cta-hint kbd {
            background: #12151c;
            border: 1px solid #232938;
            border-radius: 4px;
            padding: 1px 6px;
            font-size: 0.72rem;
            color: #c5cad6;
            font-family: 'SF Mono', 'Consolas', monospace;
        }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="welcome-container">
        <div class="welcome-header">
            <div class="welcome-eyebrow"><span class="dot"></span>RIA M&amp;A Toolkit</div>
            <h1>Model the deal before<br>you make the offer.</h1>
            <p class="subtitle">
                Buyer-side acquisition economics for Registered Investment Advisors.
                Structure consideration, stress-test returns, and pressure-check financing &mdash;
                with live SEC Form ADV auto-fill for <strong>~16K registered firms</strong>.
            </p>
        </div>
        <div class="feature-grid">
            <div class="feature-card">
                <div class="feature-icon">&#9670;</div>
                <h3>Deal Structuring</h3>
                <p>Upfront cash, seller notes, earnouts, and equity rollover splits</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#9638;</div>
                <h3>Pro Forma P&amp;L</h3>
                <p>5-year forecast with growth, attrition, and cost synergies</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#9686;</div>
                <h3>Return Metrics</h3>
                <p>IRR, cash-on-cash, breakeven year, and DSCR by year</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#9649;</div>
                <h3>Sensitivity Tables</h3>
                <p>Two-way heatmaps across multiple, growth, and attrition</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#9655;</div>
                <h3>Earnout Modeling</h3>
                <p>Floor, cap, cliff vesting, and revenue/AUM/client metrics</p>
            </div>
            <div class="feature-card">
                <div class="feature-icon">&#9744;</div>
                <h3>Debt Analysis</h3>
                <p>Amortization with I/O periods, balloons, and standstill terms</p>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    _l, col_btn, _r = st.columns([1, 1, 1])
    with col_btn:
        if st.button("Open the calculator  →", type="primary", use_container_width=True):
            st.session_state.show_calculator = True
            st.rerun()

    st.markdown(
        '<div class="cta-hint">No sign-up. Inputs are session-only. '
        'Free SEC ADV lookups for ~16K registered RIAs.</div>',
        unsafe_allow_html=True,
    )

    render_site_footer()


def render_instructions_tab():
    """Render the instructions/help tab with navigation guide and scenarios."""
    st.markdown("""
    <style>
        .guide-card {
            background: #12151c;
            border: 1px solid #1c2230;
            border-radius: 10px;
            padding: 22px 24px;
            margin-bottom: 14px;
        }
        .guide-card h3 {
            color: #ffffff;
            font-size: 0.98rem;
            font-weight: 600;
            margin-bottom: 14px;
            padding-bottom: 10px;
            border-bottom: 1px solid #1c2230;
        }
        .guide-card p, .guide-card li {
            color: #a0a8ba;
            font-size: 0.86rem;
            line-height: 1.65;
        }
        .guide-card strong { color: #e5e9f0; }
        .guide-card code {
            background: #0b0d12;
            color: #95a3ff;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.8rem;
            border: 1px solid #1c2230;
        }
        .step-row {
            display: flex;
            align-items: flex-start;
            gap: 14px;
            margin-bottom: 14px;
        }
        .step-num {
            flex-shrink: 0;
            width: 26px;
            height: 26px;
            background: rgba(124,140,255,0.15);
            color: #95a3ff;
            border: 1px solid rgba(124,140,255,0.35);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            font-size: 0.78rem;
            margin-top: 1px;
        }
        .step-content { flex: 1; }
        .step-content strong { color: #e5e9f0; }
        .step-content p {
            color: #a0a8ba;
            font-size: 0.86rem;
            line-height: 1.6;
            margin: 0;
        }
        .scenario-box {
            background: #12151c;
            border: 1px solid #1c2230;
            border-radius: 10px;
            padding: 22px 24px;
            margin-bottom: 14px;
        }
        .scenario-box h4 {
            font-size: 0.95rem;
            font-weight: 600;
            margin-bottom: 16px;
        }
        .scenario-blue h4 { color: #95a3ff; border-left: 3px solid #7c8cff; padding-left: 12px; }
        .scenario-green h4 { color: #4ade80; border-left: 3px solid #4ade80; padding-left: 12px; }
        .scenario-box table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 14px;
        }
        .scenario-box th {
            text-align: left;
            color: #6b7385;
            font-size: 0.70rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            padding: 6px 10px;
            border-bottom: 1px solid #1c2230;
        }
        .scenario-box td {
            color: #e5e9f0;
            font-size: 0.84rem;
            padding: 7px 10px;
            border-bottom: 1px solid rgba(28,34,48,0.7);
        }
        .scenario-box .note {
            color: #8b94a8;
            font-size: 0.80rem;
            font-style: italic;
            line-height: 1.65;
            margin-top: 8px;
        }
        .nav-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 10px;
            margin-top: 8px;
        }
        @media (max-width: 768px) {
            .nav-grid { grid-template-columns: 1fr; }
        }
        .nav-item {
            background: #0e1118;
            border: 1px solid #1c2230;
            border-radius: 6px;
            padding: 12px 14px;
        }
        .nav-item strong {
            color: #ffffff;
            font-size: 0.85rem;
            display: block;
            margin-bottom: 4px;
        }
        .nav-item span {
            color: #8b94a8;
            font-size: 0.78rem;
            line-height: 1.55;
        }
    </style>
    """, unsafe_allow_html=True)

    st.markdown('<div class="section-header">Getting Started</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="guide-card">
        <h3>How to Navigate the Calculator</h3>
        <div class="step-row">
            <div class="step-num">1</div>
            <div class="step-content">
                <p><strong>Sidebar (left panel)</strong> is where all inputs live. Start with
                <strong>Target Firm</strong> at the top &mdash; enter the AUM, revenue, EBITDA,
                and owner comp of the RIA you&rsquo;re evaluating. Scroll down through
                Deal Terms, Financing, and other sections.</p>
            </div>
        </div>
        <div class="step-row">
            <div class="step-num">2</div>
            <div class="step-content">
                <p><strong>Deal Terms</strong> controls structure. You can either enter
                a purchase price directly or pick a revenue multiple and let the calculator
                derive the price. Then allocate the consideration: upfront cash, seller note,
                earnout, and equity rollover should add up to 100%.</p>
            </div>
        </div>
        <div class="step-row">
            <div class="step-num">3</div>
            <div class="step-content">
                <p><strong>Everything updates live.</strong> As you change any input in the sidebar,
                the five analysis tabs on the right recalculate instantly. There&rsquo;s no
                submit button &mdash; just adjust the inputs and watch the outputs change.</p>
            </div>
        </div>
        <div class="step-row">
            <div class="step-num">4</div>
            <div class="step-content">
                <p><strong>Work through the tabs left to right.</strong> Start with
                Deal Summary for headline numbers, then dig into Pro Forma for the P&amp;L,
                Sensitivity for stress testing, Earnout for seller economics,
                and Debt for amortization and DSCR.</p>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="guide-card">
        <h3>What Each Tab Shows You</h3>
        <div class="nav-grid">
            <div class="nav-item">
                <strong>Deal Summary</strong>
                <span>Purchase price multiples (revenue, AUM, EBOC), deal structure
                waterfall chart, IRR, cash-on-cash, breakeven year, DSCR</span>
            </div>
            <div class="nav-item">
                <strong>Pro Forma Financials</strong>
                <span>5-year P&amp;L table with revenue, expenses, debt service, net cash flow.
                Combined entity view if buyer profile is enabled.</span>
            </div>
            <div class="nav-item">
                <strong>Sensitivity Analysis</strong>
                <span>Two heatmaps &mdash; IRR by multiple vs. attrition, and breakeven
                by growth vs. multiple. Use these to find where the deal breaks.</span>
            </div>
            <div class="nav-item">
                <strong>Earnout &amp; Seller Economics</strong>
                <span>Earnout payouts under 3 scenarios, cumulative seller proceeds timeline,
                and seller note amortization schedule.</span>
            </div>
            <div class="nav-item">
                <strong>Debt Analysis</strong>
                <span>Full loan amortization, principal vs. interest breakdown, and DSCR
                by year with 1.25x lender threshold marked.</span>
            </div>
            <div class="nav-item">
                <strong>PDF Export (bottom of page)</strong>
                <span>Scroll past the tabs to the footer. Click &ldquo;Export Full Analysis
                to PDF&rdquo; to download a complete report.</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # -- Scenario walkthroughs ---
    st.markdown('<div class="section-header">Example Scenarios</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="scenario-box scenario-blue">
        <h4>Scenario A &mdash; Large Platform Acquiring a $1B RIA</h4>
        <table>
            <tr><th>Input</th><th>Value</th><th>Rationale</th></tr>
            <tr><td>Target AUM</td><td>$1,000,000,000</td><td>Mid-size RIA with institutional book</td></tr>
            <tr><td>Revenue</td><td>$7,500,000</td><td>~75 bps on AUM (fee-based)</td></tr>
            <tr><td>EBITDA</td><td>$3,000,000</td><td>40% margin, well-run practice</td></tr>
            <tr><td>Owner Comp</td><td>$750,000</td><td>Founder takes above-market salary</td></tr>
            <tr><td>Clients</td><td>350</td><td>HNW-focused, avg $2.9M per household</td></tr>
            <tr><td>Growth Rate</td><td>6%</td><td>Organic + market appreciation</td></tr>
            <tr><td>Attrition</td><td>4%</td><td>Low &mdash; sticky client base</td></tr>
            <tr><td>Purchase Price</td><td>$18,000,000</td><td>~2.4x revenue, in-line for quality book</td></tr>
            <tr><td>Structure</td><td>60 / 20 / 15 / 5</td><td>Cash / Note / Earnout / Rollover</td></tr>
            <tr><td>Financing</td><td>50% self-funded</td><td>$5.4M loan at 6.5% over 7 years</td></tr>
            <tr><td>Integration</td><td>$200K one-time</td><td>Tech migration, compliance transfer</td></tr>
        </table>
        <p class="note">
            <strong>What to look for:</strong> Check Deal Summary for the implied EBOC multiple
            (should be under 5x for a quality deal). Go to Sensitivity Analysis and see how
            IRR holds up if attrition spikes to 8-10% in the first two years &mdash; this is the
            key risk in platform acquisitions where the founder is departing. The Earnout tab
            shows what the seller actually receives under different retention outcomes.
        </p>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="scenario-box scenario-green">
        <h4>Scenario B &mdash; $10B Wealth Platform Acquiring a $500M RIA</h4>
        <table>
            <tr><th>Input</th><th>Value</th><th>Rationale</th></tr>
            <tr><td>Target AUM</td><td>$500,000,000</td><td>Smaller practice, lifestyle RIA</td></tr>
            <tr><td>Revenue</td><td>$4,000,000</td><td>~80 bps blended fee schedule</td></tr>
            <tr><td>EBITDA</td><td>$1,400,000</td><td>35% margin, lean team</td></tr>
            <tr><td>Owner Comp</td><td>$500,000</td><td>Close to market rate already</td></tr>
            <tr><td>Clients</td><td>150</td><td>Avg $3.3M per client, concentrated book</td></tr>
            <tr><td>Growth Rate</td><td>4%</td><td>Modest &mdash; mature practice</td></tr>
            <tr><td>Attrition</td><td>7%</td><td>Higher risk &mdash; personal relationship-driven</td></tr>
            <tr><td>Purchase Price</td><td>$8,000,000</td><td>2.0x revenue (standard tuck-in)</td></tr>
            <tr><td>Structure</td><td>50 / 25 / 20 / 5</td><td>More deferred &mdash; heavier note + earnout</td></tr>
            <tr><td>Financing</td><td>40% self-funded</td><td>$2.4M loan at 7% over 5 years</td></tr>
            <tr><td>Seller Note</td><td>5% rate, 5-yr term</td><td>1-year standstill with interest-only</td></tr>
            <tr><td>Earnout</td><td>Revenue retention, 0% floor, 125% cap</td><td>3-year earnout, annual vesting</td></tr>
            <tr><td>Consulting</td><td>$150K/yr for 2 years</td><td>Transition advisory</td></tr>
            <tr><td>Non-Compete</td><td>$100K over 3 years</td><td>Standard geographic restriction</td></tr>
        </table>
        <p class="note">
            <strong>What to look for:</strong> This is a riskier deal with higher attrition and
            a concentrated book. In the Pro Forma tab, watch Year 1-2 net cash flow &mdash;
            it may go negative with consulting costs and integration drag. The Debt tab
            will show whether DSCR stays above 1.25x. Check if the seller note standstill
            helps early-year cash flow. In Sensitivity, stress test the 7% attrition up
            to 12% &mdash; with only 150 clients, losing a few large households can materially
            change the outcome. Enable the combined entity view in Buyer Profile to see the
            post-acquisition $10.5B platform picture.
        </p>
    </div>
    """, unsafe_allow_html=True)

    # Key assumptions reference
    st.markdown('<div class="section-header">Key Assumptions</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="guide-card">
        <h3>How the Model Works</h3>
        <div class="nav-grid">
            <div class="nav-item">
                <strong>EBOC = EBITDA + (Owner Comp &minus; $200K)</strong>
                <span>Normalizes for the seller&rsquo;s excess comp above market replacement.
                Used as the primary earnings metric for pricing.</span>
            </div>
            <div class="nav-item">
                <strong>IRR uses 6x EBITDA terminal value</strong>
                <span>Computed at year 3, 5, and 7 horizons. Assumes a future sale
                at a 6x multiple of that year&rsquo;s EBITDA.</span>
            </div>
            <div class="nav-item">
                <strong>Client attrition in years 1&ndash;2 only</strong>
                <span>Post-acquisition churn typically peaks in the first two years,
                then stabilizes as clients settle with the new team.</span>
            </div>
            <div class="nav-item">
                <strong>DSCR = EBITDA / Total Debt Service</strong>
                <span>Lenders typically require 1.25x minimum. The calculator
                tracks this each year against both loan and note payments.</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)


def currency_input(label, default, key, help_text=None):
    """Text input that displays and accepts comma-formatted dollar amounts.
    Reformats the field in place after the user submits (Enter/blur) so
    "1000" becomes "1,000" without leaving a stale caption hint behind."""
    if key not in st.session_state:
        st.session_state[key] = f"{default:,.0f}"
    raw = st.sidebar.text_input(label, key=key, help=help_text)
    try:
        cleaned = raw.replace(",", "").replace("$", "").replace(" ", "").strip()
        val = int(float(cleaned))
        formatted = f"{val:,.0f}"
        if raw != formatted:
            _queue_apply(key, formatted)
            st.rerun()
        return val
    except (ValueError, TypeError):
        st.sidebar.error(f"Invalid number: {raw}")
        return default


def count_input(label, default, key, help_text=None):
    """Like currency_input but for plain integer counts (no $ prefix).
    Reformats the field in place after submit so client counts render
    "1,000" not "1000"."""
    if key not in st.session_state:
        st.session_state[key] = f"{default:,}"
    raw = st.sidebar.text_input(label, key=key, help=help_text)
    try:
        cleaned = raw.replace(",", "").replace(" ", "").strip()
        val = int(float(cleaned))
        formatted = f"{val:,}"
        if raw != formatted:
            _queue_apply(key, formatted)
            st.rerun()
        return val
    except (ValueError, TypeError):
        st.sidebar.error(f"Invalid number: {raw}")
        return default


# ==============================================================================
# WELCOME PAGE GATE
# ==============================================================================
if not st.session_state.show_calculator:
    render_welcome_page()
    st.stop()

# ==============================================================================
# SIDEBAR -- Inputs
# ==============================================================================
st.sidebar.markdown("## RIA M&A Calculator")
st.sidebar.markdown("---")

# Flush any queued SEC-driven value updates BEFORE the widgets render.
_apply_pending()

# -- Auto-fill from SEC (Form ADV) ---------------------------------------------
st.sidebar.markdown("### Auto-fill from SEC")


def _fmt_aum_short(aum: float) -> str:
    """$X.XB for ≥$1B, $XXXM otherwise — avoids '$0.0B' rounding loss."""
    if aum >= 1e9:
        return f"${aum/1e9:.1f}B"
    return f"${aum/1e6:.0f}M"


search_query = st.sidebar.text_input(
    "Search by firm name",
    key="sec_search",
    placeholder="e.g., Edelman Financial",
    help="Searches the SEC's bulk Form ADV dataset (~16K SEC-registered RIAs).",
)
_query_clean = (search_query or "").strip()
# Tracks whether the matched-firms branch rendered its own merged
# attribution+quota caption; if so we skip the standalone source caption
# below to avoid duplication.
_sec_source_caption_rendered = False
if _query_clean and len(_query_clean) < 3:
    st.sidebar.caption("Type 3+ characters to search.")
elif _query_clean:
    _adv_df = _load_adv_df()
    if _adv_df.empty:
        # Show why so a future operator can act on the actual failure mode.
        # Status codes are PAT-safe (no token, URL, or exception text in them).
        _status_msg = {
            "no_token": "`ADV_DATA_TOKEN` secret not set on Streamlit Cloud.",
            "bad_config": "One of the `ADV_DATA_*` secrets has invalid characters.",
            "http_401": "PAT is invalid or expired.",
            "http_403": "PAT lacks Contents:read on the data repo.",
            "http_404": "Data repo/file/branch not found.",
            "bad_response": "Data source returned a non-parquet body.",
            "network_error": "Couldn't reach api.github.com.",
            "io_error": "Couldn't write the local cache.",
            "unknown_error": "Unexpected error resolving the data source. Check Streamlit logs.",
        }.get(_adv_resolution_status(), "Data source check failed.")
        st.sidebar.caption(f"⚠ SEC data unavailable — {_status_msg}")
    else:
        _matches = sec_lookup.search_firms(_query_clean, _adv_df, limit=5)
        if _matches:
            # Include a hash of the query in the widget key so a fresh search
            # forces a fresh selectbox (Streamlit doesn't reset the closed-state
            # label when `options` changes underneath a stable key).
            _sel_idx = st.sidebar.selectbox(
                "Matching firms",
                options=list(range(len(_matches))),
                format_func=lambda i: f"{_fmt_aum_short(_matches[i].aum)} — {_matches[i].firm_name}",
                key=f"sec_selected_idx_{hash(_query_clean)}",
            )
            _selected_crd = _matches[_sel_idx].crd
            _allowed, _remaining, _wait_sec = _sec_lookup_quota()
            # Auto-trigger reconciliation when the selectbox lands on a CRD
            # we haven't loaded yet. Removes the "Load SEC data" intermediate
            # click — typing a firm name and picking from the dropdown is
            # enough. `last_loaded_crd` is updated after Apply OR Cancel so
            # the dialog doesn't re-open until the user picks a different
            # firm.
            if _allowed and _selected_crd != st.session_state.get("last_loaded_crd"):
                _record_sec_lookup()
                st.session_state.sec_data = sec_lookup.get_firm_data(
                    _selected_crd, _adv_df
                )
                st.session_state.pending_reconcile = True
                st.session_state.last_loaded_crd = _selected_crd
                st.rerun()
            if not _allowed:
                _wait_label = (
                    f"~{_wait_sec // 60}m" if _wait_sec >= 60
                    else f"~{max(_wait_sec, 1)}s"
                )
                st.sidebar.error(
                    f"Limit: {SEC_LOOKUP_LIMIT} lookups per "
                    f"{SEC_LOOKUP_WINDOW_SEC // 60} min (resets in {_wait_label})."
                )
            else:
                # Merged attribution + quota line — one caption is calmer
                # than two stacked ones and conserves sidebar real estate.
                st.sidebar.caption(
                    f"Source: [SEC Form ADV](https://www.sec.gov/data-research/sec-markets-data/"
                    f"information-about-registered-investment-advisers-exempt-reporting-advisers) "
                    f"(public) · {_remaining}/{SEC_LOOKUP_LIMIT} lookups left."
                )
                _sec_source_caption_rendered = True
        else:
            # Styled empty-state explaining our coverage and pointing
            # state-only firms to IAPD instead of dead-ending the user.
            # Escape '$' as '\$' — Streamlit otherwise interprets unescaped
            # dollar signs as MathJax delimiters and renders the surrounded
            # text in italic math font, mangling "$100M+ AUM".
            st.sidebar.info(
                "No match in our dataset. We currently cover ~16K "
                "SEC-registered RIAs (\\$100M+ AUM). State-registered firms "
                "(<\\$100M AUM) may be added soon — for now try the SEC's "
                "[IAPD search](https://adviserinfo.sec.gov)."
            )

if not _sec_source_caption_rendered:
    st.sidebar.caption(
        "_Source: [SEC Form ADV](https://www.sec.gov/data-research/sec-markets-data/"
        "information-about-registered-investment-advisers-exempt-reporting-advisers) (public)._"
    )

# Loaded-firm banner + clear button. Sits at the bottom of the lookup block so
# it's anchored to the lookup widget, not Target Firm.
if st.session_state.sec_data is not None:
    _sec = st.session_state.sec_data
    _as_of = f" · as of {_sec.as_of_date}" if _sec.as_of_date else ""
    # Custom-styled chip so the loaded-firm state reads as a callout (not a
    # caption lost between other captions).
    st.sidebar.markdown(
        f"""
        <div style="background: rgba(74,222,128,0.06);
                    border: 1px solid rgba(74,222,128,0.30);
                    border-radius: 6px;
                    padding: 8px 10px;
                    margin: 4px 0 6px 0;
                    font-size: 0.78rem;
                    color: #c5cad6;
                    line-height: 1.4;">
            <span style="color:#4ade80; font-weight:600;">● SEC loaded</span><br>
            <span style="color:#e5e9f0;">{_html.escape(_sec.firm_name)}</span>
            <span style="color:#6b7385; font-size:0.72rem;">{_as_of}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    # "Report incorrect data" mailto. urllib.parse.quote handles ampersands,
    # accents, etc. in firm names so the subject line stays well-formed.
    _report_subject = urllib.parse.quote(
        f"ADV data issue: {_sec.firm_name} (CRD {_sec.crd})"
    )
    st.sidebar.markdown(
        f"<div style='font-size:0.72rem; margin: -2px 0 6px 0;'>"
        f"<a href='mailto:dhruvjani7@gmail.com?subject={_report_subject}' "
        f"style='color:#8b94a8;'>Report incorrect data</a></div>",
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Clear SEC data", key="sec_clear_btn", use_container_width=True):
        st.session_state.sec_data = None
        st.session_state.pending_reconcile = False
        st.rerun()

st.sidebar.markdown("---")


def _current_aum_int() -> int:
    """Parse the current AUM widget value to int, for reconciliation comparison."""
    raw = st.session_state.get("aum", "500,000,000")
    try:
        return int(float(str(raw).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        return 500_000_000


# Widget keys backed by currency_input() — text_input wrappers that require
# their session_state value to be a comma-formatted string, not an int.
_CURRENCY_INPUT_KEYS = {"aum", "revenue", "ebitda", "owner_comp", "purchase_price"}
# Plain-integer text inputs (no $ prefix) — queue values as comma strings.
_COUNT_INPUT_KEYS = {"num_clients"}


def _sec_field_badge(field_key: str, sec_value, fmt_fn, label: str):
    """Render a small caption + revert button below an input.
    Clicking the button queues the SEC value for the input on the next rerun.

    Currency inputs need the queued value to be a comma-formatted string;
    number_input and slider inputs need their native types. Mismatching
    these types crashes the widget on the next rerun with a TypeError on
    `text_input_proto.value = widget_state.value`."""
    if st.session_state.sec_data is None or sec_value is None:
        return
    col1, col2 = st.sidebar.columns([3, 1])
    col1.caption(f"SEC: {fmt_fn(sec_value)}")
    if col2.button("↺", key=f"revert_{field_key}", help=f"Use SEC value for {label}"):
        if field_key in _CURRENCY_INPUT_KEYS or field_key in _COUNT_INPUT_KEYS:
            _queue_apply(field_key, f"{int(sec_value):,}")
        else:
            _queue_apply(field_key, sec_value)
        st.rerun()


# -- Target Firm ---------------------------------------------------------------
st.sidebar.markdown("### Target Firm")
_sec = st.session_state.sec_data

aum = currency_input("AUM ($)", 500_000_000, "aum")
if _sec is not None:
    _sec_field_badge("aum", int(_sec.aum), lambda v: f"${v:,.0f}", "AUM")

annual_revenue = currency_input("Annual Revenue ($)", 4_000_000, "revenue")
# Above ~$10B AUM the 0.75% blended-fee assumption breaks down (institutional
# share classes, performance fees, family-office economics, etc.), so we
# refuse to offer the estimate rather than risk anchoring the user on a
# wrong number.
_REV_ESTIMATE_AUM_CAP = 10e9
if _sec is not None:
    if _sec.aum > _REV_ESTIMATE_AUM_CAP:
        st.sidebar.caption(
            "⚠ Revenue estimate disabled — blended fee rates vary too "
            "much above $10B AUM. Enter manually."
        )
    else:
        _sec_field_badge(
            "revenue", int(_sec.estimated_revenue),
            # Revenue isn't in ADV — this is AUM × 0.75% as a rough proxy. Loud
            # disclaimer so it never lands in an IC memo as "filed revenue."
            lambda v: f"${v:,.0f} · est only (AUM × 0.75%, not filed)", "Revenue",
        )

ebitda = currency_input("EBITDA ($)", 1_600_000, "ebitda")
owner_comp = currency_input("Owner's Compensation ($)", 500_000, "owner_comp")

num_clients = count_input("Number of Clients", 200, "num_clients")
if _sec is not None and _sec.num_clients and _sec.num_clients > 0:
    _sec_field_badge(
        "num_clients", int(_sec.num_clients), lambda v: f"{v:,}", "Clients"
    )

if "rev_growth_pct" not in st.session_state:
    st.session_state["rev_growth_pct"] = 5.0
# Slider tops out at 30%; anything beyond is so anomalous that the user
# should manually enter rather than have the AUM-proxy auto-fill it.
_GROWTH_SLIDER_MAX = 30.0
rev_growth_pct = st.sidebar.slider(
    "Revenue Growth Rate (%)", -10.0, _GROWTH_SLIDER_MAX, step=0.5, key="rev_growth_pct",
    help="The pro forma applies this to revenue. SEC auto-fill uses YoY AUM "
         "change as a proxy — it's directionally right but isn't filed revenue growth.",
)
rev_growth = rev_growth_pct / 100
if _sec is not None and _sec.growth_rate is not None:
    _raw_pct = _sec.growth_rate * 100
    _capped_pct = round(max(0.0, min(_GROWTH_SLIDER_MAX, _raw_pct)), 1)
    if _raw_pct > _GROWTH_SLIDER_MAX:
        _badge_fmt = lambda v, raw=_raw_pct: f"{raw:.1f}% YoY AUM (slider caps at {_GROWTH_SLIDER_MAX:.0f}%)"
    elif _raw_pct < 0:
        _badge_fmt = lambda v, raw=_raw_pct: f"{raw:.1f}% YoY AUM (negative — slider clamps to 0)"
    else:
        _badge_fmt = lambda v: f"{v:.1f}% YoY AUM (proxy)"
    _sec_field_badge("rev_growth_pct", _capped_pct, _badge_fmt, "Growth")

pct_recurring = st.sidebar.slider("% Recurring Revenue", 50, 100, 90, 5)
attrition_rate = st.sidebar.slider("Client Attrition Rate (%)", 0.0, 20.0, 5.0, 0.5) / 100


# -- Reconciliation dialog (shown once after each successful lookup) -----------
def _render_reconcile_dialog():
    sec = st.session_state.sec_data
    if sec is None:
        return

    # Truncate the title so long firm names ("Edelman Financial Engines, LLC")
    # don't overflow Streamlit's modal title. The full name lives in the
    # subtitle below.
    _short_name = sec.firm_name if len(sec.firm_name) <= 42 else sec.firm_name[:40].rstrip(" ,.") + "…"

    @st.dialog(f"Apply SEC data — {_short_name}")
    def _dlg():
        # Full firm name + as-of date in a single subtitle line.
        st.markdown(
            f"<div style='color:#c5cad6; font-size:0.92rem; font-weight:500; "
            f"margin: -4px 0 4px 0;'>{_html.escape(sec.firm_name)}</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            f"Filing as of {sec.as_of_date or 'unknown'}. "
            f"Each field defaults to **Keep mine** — flip to SEC only for fields "
            f"you want overwritten."
        )

        decisions: dict[str, tuple[str, object]] = {}

        def _row(key: str, label: str, current_display: str, sec_value, sec_display: str):
            st.markdown(f"**{label}**")
            choice = st.radio(
                label,
                options=[f"Keep mine: {current_display}", f"SEC: {sec_display}"],
                index=0,  # default to "Keep mine" — never silently overwrite user inputs
                horizontal=True,
                key=f"reconcile_radio_{key}",
                label_visibility="collapsed",
            )
            decisions[key] = (choice, sec_value)

        _row("aum", "AUM",
             f"${_current_aum_int():,}",
             int(sec.aum), f"${int(sec.aum):,}")

        if sec.num_clients and sec.num_clients > 0:
            _row("num_clients", "Number of Clients",
                 f"{int(st.session_state.get('num_clients', 200)):,}",
                 int(sec.num_clients), f"{int(sec.num_clients):,}")

        if sec.aum > _REV_ESTIMATE_AUM_CAP:
            # Don't queue a revenue apply at all for very large firms — the
            # 0.75% blended-fee assumption is too unreliable above $10B.
            st.warning(
                "Revenue estimate disabled — blended fee rates vary too "
                "much above $10B AUM. Enter manually."
            )
        else:
            est_rev = int(sec.estimated_revenue)
            try:
                cur_rev = int(float(str(st.session_state.get('revenue', '4,000,000'))
                                     .replace(',', '').replace('$', '').strip()))
            except (ValueError, TypeError):
                cur_rev = 4_000_000
            _row("revenue", "Annual Revenue · estimate only (AUM × 0.75%, not filed)",
                 f"${cur_rev:,}", est_rev, f"${est_rev:,}")

        if sec.growth_rate is not None:
            raw_pct = sec.growth_rate * 100
            growth_pct = round(max(0.0, min(_GROWTH_SLIDER_MAX, raw_pct)), 1)
            cur_growth = float(st.session_state.get("rev_growth_pct", 5.0))
            if raw_pct > _GROWTH_SLIDER_MAX:
                sec_disp = f"{raw_pct:.1f}% YoY AUM (slider caps at {_GROWTH_SLIDER_MAX:.0f}%)"
            elif raw_pct < 0:
                sec_disp = f"{raw_pct:.1f}% YoY AUM (negative — applied as 0%)"
            else:
                sec_disp = f"{growth_pct:.1f}% (YoY AUM proxy)"
            _row("rev_growth_pct", "Revenue Growth · AUM proxy (not filed revenue growth)",
                 f"{cur_growth:.1f}%", growth_pct, sec_disp)

        st.markdown("---")
        c1, c2 = st.columns(2)
        if c1.button("Apply selected", type="primary", use_container_width=True):
            applied = 0
            applied_keys: set[str] = set()
            for key, (choice, sec_value) in decisions.items():
                if choice.startswith("SEC:"):
                    if key in ("aum", "revenue", "num_clients"):
                        _queue_apply(key, f"{int(sec_value):,}")
                    else:
                        _queue_apply(key, sec_value)
                    applied += 1
                    applied_keys.add(key)
            # When the user accepts SEC revenue, auto-scale the purchase
            # price to 2× revenue (standard RIA M&A multiple). Without this,
            # a $326B AUM firm would keep the default $8M purchase price
            # and every downstream multiple (Rev Multiple, % of AUM, IRR,
            # EBOC multiple) would render as ~0. We only auto-suggest;
            # the user can still override in the sidebar.
            if "revenue" in applied_keys:
                applied_revenue = next(
                    sv for k, (c, sv) in decisions.items()
                    if k == "revenue" and c.startswith("SEC:")
                )
                _queue_apply("purchase_price", f"{int(2 * applied_revenue):,}")
            st.session_state.pending_reconcile = False
            if applied:
                st.toast(
                    f"Applied {applied} SEC value{'s' if applied != 1 else ''}"
                    + (" + auto-scaled purchase price (2× revenue)." if "revenue" in applied_keys else "."),
                    icon="✅",
                )
            st.rerun()
        if c2.button("Cancel", use_container_width=True):
            # Cancel = full bail-out. Previously this only cleared
            # pending_reconcile, leaving the green SEC banner, the per-field
            # revert badges, and the Clear-SEC-data button visible — a
            # half-applied state that confused users. Wiping sec_data
            # restores the sidebar to its pre-lookup shape.
            st.session_state.pending_reconcile = False
            st.session_state.sec_data = None
            st.rerun()

    _dlg()


if st.session_state.pending_reconcile and st.session_state.sec_data is not None:
    _render_reconcile_dialog()

st.sidebar.markdown("---")

# -- Deal Terms ----------------------------------------------------------------
st.sidebar.markdown("### Deal Terms")
price_method = st.sidebar.radio("Purchase Price Method", ["Enter Price", "Select Multiple"])
if price_method == "Enter Price":
    purchase_price = currency_input("Purchase Price ($)", 8_000_000, "purchase_price")
else:
    rev_multiple = st.sidebar.slider("Revenue Multiple", 1.0, 5.0, 2.0, 0.1)
    purchase_price = int(annual_revenue * rev_multiple)
    st.sidebar.info(f"Implied Price: {fmt_dollar(purchase_price)}")

# Explicit keys so the "Apply suggested splits" button on Tab 1 can write
# values into these sliders via the _queue_apply / _apply_pending pattern.
for _k, _v in (("pct_upfront_cash", 60), ("pct_seller_note", 20),
               ("pct_earnout", 15), ("pct_equity_rollover", 5)):
    if _k not in st.session_state:
        st.session_state[_k] = _v
pct_upfront_cash = st.sidebar.slider("% Upfront Cash", 0, 100, step=5, key="pct_upfront_cash") / 100
pct_seller_note = st.sidebar.slider("% Seller Note", 0, 100, step=5, key="pct_seller_note") / 100
pct_earnout = st.sidebar.slider("% Earnout", 0, 100, step=5, key="pct_earnout") / 100
pct_equity_rollover = st.sidebar.slider("% Equity Rollover", 0, 100, step=5, key="pct_equity_rollover") / 100

deal_total = pct_upfront_cash + pct_seller_note + pct_earnout + pct_equity_rollover
if abs(deal_total - 1.0) > 0.01:
    st.sidebar.warning(f"Deal structure sums to {deal_total*100:.0f}% (should be 100%)")

st.sidebar.markdown("---")

# -- Seller Note Terms ---------------------------------------------------------
st.sidebar.markdown("### Seller Note Terms")
note_rate = st.sidebar.slider("Interest Rate (%)", 0.0, 10.0, 5.0, 0.25) / 100
note_term = st.sidebar.number_input("Amortization Term (years)", value=5, min_value=1, max_value=10, key="note_term")
note_standstill = st.sidebar.number_input(
    "Standstill Period (years)", value=0, min_value=0, max_value=5, key="note_standstill",
    help="Deferred start -- no principal payments during this period",
)
note_io_standstill = False
if note_standstill > 0:
    note_io_standstill = st.sidebar.checkbox(
        "Pay interest during standstill",
        value=True, key="note_io",
        help="If unchecked, interest accrues (PIK) and is added to the balance",
    )

st.sidebar.markdown("---")

# -- Earnout Terms -------------------------------------------------------------
st.sidebar.markdown("### Earnout Terms")
earnout_period = st.sidebar.number_input("Earnout Period (years)", value=3, min_value=1, max_value=5)
earnout_metric = st.sidebar.selectbox(
    "Performance Metric",
    ["Revenue Retention", "AUM Retention", "Client Retention"],
    help="The KPI used to measure earnout achievement each year",
)
earnout_floor = st.sidebar.slider(
    "Earnout Floor (%)", 0, 100, 0, 5,
    help="Minimum payout % per year regardless of performance",
)
earnout_cap = st.sidebar.slider(
    "Earnout Cap (%)", 100, 150, 125, 5,
    help="Maximum payout % per year (allows upside above target)",
)
earnout_cliff = st.sidebar.checkbox(
    "Cliff Vesting",
    value=False,
    help="If checked, entire earnout pays out at end of period based on avg performance (vs. annual payouts)",
)

st.sidebar.markdown("---")

# -- Financing -----------------------------------------------------------------
st.sidebar.markdown("### Financing")
pct_self_funded = st.sidebar.slider("% Self-Funded (of upfront cash)", 0, 100, 50, 5) / 100
loan_rate = st.sidebar.slider("Loan Interest Rate (%)", 0.0, 12.0, 6.5, 0.25) / 100
loan_term = st.sidebar.number_input("Loan Term (years)", value=7, min_value=1, max_value=15, key="loan_term")

with st.sidebar.expander("Advanced Loan Options"):
    loan_io_years = st.number_input(
        "Interest-Only Period (years)", value=0, min_value=0, max_value=5, key="loan_io",
        help="Pay only interest for this many years before amortization begins",
    )
    loan_balloon = st.checkbox(
        "Balloon Payment at Maturity",
        value=False, key="loan_balloon",
        help="Amortize over a longer schedule with remaining balance due at term end",
    )
    loan_amort_years = loan_term
    if loan_balloon:
        loan_amort_years = st.number_input(
            "Amortization Schedule (years)", value=15, min_value=loan_term + 1, max_value=30, key="loan_amort_yrs",
            help="Payments calculated on this schedule; remaining balance due at loan term",
        )

st.sidebar.markdown("---")

# -- Transition Compensation ---------------------------------------------------
st.sidebar.markdown("### Seller Transition Terms")
consulting_annual = currency_input(
    "Annual Consulting Fee ($)", 150_000, "consulting_fee",
    help_text="Post-close consulting/advisory agreement with seller",
)
consulting_years = st.sidebar.number_input(
    "Consulting Duration (years)", value=2, min_value=0, max_value=5, key="consult_yrs",
)
noncompete_total = currency_input(
    "Non-Compete Payment Total ($)", 100_000, "noncompete",
    help_text="Total non-compete consideration paid to seller",
)
noncompete_years = st.sidebar.number_input(
    "Non-Compete Period (years)", value=3, min_value=0, max_value=7, key="nc_yrs",
)

st.sidebar.markdown("---")

# -- Integration ---------------------------------------------------------------
st.sidebar.markdown("### Integration Assumptions")
st.sidebar.caption(
    "Full framework lives in the Integration Strategy tab. "
    "Toggle below to override with flat values."
)
use_manual_integration = st.sidebar.checkbox(
    "Manual override (flat inputs)", value=False, key="manual_integration",
    help="If on, use the flat synergy/cost inputs below. If off, the "
         "ramped Part-E schedule from the Integration Strategy tab is used.",
)
integration_costs = currency_input("One-Time Integration Costs ($)", 150_000, "integration")
annual_synergies = currency_input("Expected Annual Cost Synergies ($)", 100_000, "synergies")
additional_staff = st.sidebar.number_input("Additional Staff Needed", value=1, min_value=0, max_value=10)
integration_months = st.sidebar.number_input("Integration Timeline (months)", value=12, min_value=3, max_value=36)

st.sidebar.markdown("---")

# -- Tax -----------------------------------------------------------------------
st.sidebar.markdown("### Tax Impact")
tax_rate = st.sidebar.slider(
    "Buyer's Marginal Tax Rate (%)", 0, 50, 0, 1,
    help="Applied to interest deductions for after-tax cash flow view",
) / 100

st.sidebar.markdown("---")

# -- Buyer Profile -------------------------------------------------------------
st.sidebar.markdown("### Buyer Profile (Optional)")
show_combined = st.sidebar.checkbox("Show Combined Entity View", value=False)
buyer_aum = currency_input("Buyer's AUM ($)", 1_000_000_000, "buyer_aum") if show_combined else 0
buyer_revenue = currency_input("Buyer's Revenue ($)", 8_000_000, "buyer_rev") if show_combined else 0
buyer_margin = st.sidebar.slider("Buyer's EBITDA Margin (%)", 0, 60, 35, 5) / 100 if show_combined else 0


# ==============================================================================
# CALCULATIONS
# ==============================================================================
# Replacement cost auto-scales with AUM: $200K for sub-$200M, $400K for
# $200M-$1B, $600K+ for $1B+. The flat $200K constant was systematically
# inflating EBOC for mid-size and large firms and depressing their EBOC
# multiple.
replacement_cost = auto_replacement_cost(aum)
eboc = compute_eboc(ebitda, owner_comp, replacement_cost=replacement_cost)
multiples = implied_multiples(purchase_price, annual_revenue, aum, eboc)

# Multi-factor valuation band — answers "what is this firm worth?" before
# the user has to pick a price. Mid value is base 7.0× EBITDA × stacked
# multipliers (recurring, size, growth, margin, book/geo/key-person).
valuation = compute_valuation_band(
    revenue=annual_revenue, ebitda=ebitda, owner_comp=owner_comp,
    aum=aum, pct_recurring=pct_recurring, growth_rate=rev_growth,
    replacement_cost=replacement_cost,
)

# Suggested deal structure based on firm profile (banker memo, Part C).
suggested_deal = suggest_deal_structure(
    aum=aum, pct_recurring=pct_recurring, growth_rate=rev_growth,
)

# --- Integration synergy schedule (banker-MD memo Part E) ---------------------
# Defaults live in session_state so the Integration Strategy tab can update
# them and the pro forma re-renders on next run. If manual override is on,
# pass None to build_pro_forma and it falls back to the flat inputs.
_default_expense_base = max(0.0, float(annual_revenue) - float(ebitda))
_int_expense_base = st.session_state.get("int_expense_base", _default_expense_base)
_int_cost_takeout_pct = st.session_state.get("int_cost_takeout_pct", 0.14)
_int_revenue_uplift_pct = st.session_state.get("int_revenue_uplift_pct", 0.07)
_int_capture_multiple = st.session_state.get("int_capture_multiple", 1.25)
# has_compliance_team and has_cco_redundancy were two checkboxes for the same
# capability — a CCO sits inside the compliance function. We surface one
# checkbox in the UI now and feed both keys to keep the synergy weighting
# unchanged downstream.
_int_buyer_profile = {
    "has_compliance_team": st.session_state.get("int_buyer_compliance", True),
    "has_cco_redundancy": st.session_state.get("int_buyer_compliance", True),
    "has_tech_stack": st.session_state.get("int_buyer_tech", True),
    "has_planning_shop": st.session_state.get("int_buyer_planning", False),
    "has_back_office": st.session_state.get("int_buyer_backoffice", True),
}

synergy_schedule = compute_synergy_schedule(
    target_revenue=annual_revenue,
    target_expense_base=_int_expense_base,
    buyer_profile=_int_buyer_profile,
    cost_takeout_pct=_int_cost_takeout_pct,
    revenue_uplift_pct=_int_revenue_uplift_pct,
    capture_cost_multiple=_int_capture_multiple,
    years=7,
)
_active_synergy_schedule = None if use_manual_integration else synergy_schedule

pro_forma = build_pro_forma(
    revenue=annual_revenue, ebitda=ebitda, owner_comp=owner_comp,
    growth_rate=rev_growth, attrition_rate=attrition_rate, aum=aum,
    purchase_price=purchase_price, pct_upfront=pct_upfront_cash,
    pct_seller_note=pct_seller_note, note_rate=note_rate, note_term=note_term,
    pct_earnout=pct_earnout, earnout_period=earnout_period,
    pct_equity_rollover=pct_equity_rollover, pct_self_funded=pct_self_funded,
    loan_rate=loan_rate, loan_term=loan_term,
    integration_costs=integration_costs, annual_synergies=annual_synergies,
    additional_staff=additional_staff, years=7,
    loan_io_years=loan_io_years, loan_balloon=loan_balloon,
    loan_amort_years=loan_amort_years,
    note_standstill_years=note_standstill,
    note_io_during_standstill=note_io_standstill,
    consulting_annual=consulting_annual, consulting_years=consulting_years,
    noncompete_total=noncompete_total, noncompete_years=noncompete_years,
    tax_rate=tax_rate,
    replacement_cost=replacement_cost,
    synergy_schedule=_active_synergy_schedule,
)

# Build schedules separately so they can be passed into compute_irr_and_returns
# for the terminal-value debt subtraction. Without this, a 60%-leverage Y5
# exit shows IRR as if all the debt magically disappeared at closing.
_debt_amount = purchase_price * pct_upfront_cash * (1 - pct_self_funded)
_loan_sched_for_irr = build_loan_amortization(
    _debt_amount, loan_rate, loan_term,
    io_years=loan_io_years, balloon=loan_balloon, amort_years=loan_amort_years,
)
_note_sched_for_irr = build_seller_note_amortization(
    purchase_price * pct_seller_note, note_rate, note_term,
    standstill_years=note_standstill, io_during_standstill=note_io_standstill,
)

returns = compute_irr_and_returns(
    pro_forma, purchase_price, pct_upfront_cash, pct_self_funded, pct_equity_rollover,
    pct_recurring=pct_recurring,
    loan_schedule=_loan_sched_for_irr,
    note_schedule=_note_sched_for_irr,
    pct_seller_note=pct_seller_note,
)

dscr = compute_dscr(pro_forma)
pro_forma["dscr"] = dscr

debt_amount = purchase_price * pct_upfront_cash * (1 - pct_self_funded)
loan_amort = build_loan_amortization(
    debt_amount, loan_rate, loan_term,
    io_years=loan_io_years, balloon=loan_balloon,
    amort_years=loan_amort_years,
)

seller_note_principal = purchase_price * pct_seller_note
note_amort = build_seller_note_amortization(
    seller_note_principal, note_rate, note_term,
    standstill_years=note_standstill,
    io_during_standstill=note_io_standstill,
)

earnout_scenarios = compute_earnout_scenarios(
    purchase_price, pct_earnout, earnout_period, annual_revenue,
    aum, num_clients, rev_growth, attrition_rate,
    earnout_metric=earnout_metric,
    earnout_floor_pct=earnout_floor,
    earnout_cap_pct=earnout_cap,
    earnout_cliff=earnout_cliff,
)

seller_proceeds = compute_seller_total_proceeds(
    purchase_price, pct_upfront_cash, pct_seller_note, note_rate, note_term,
    earnout_scenarios,
    note_standstill_years=note_standstill,
    note_io_during_standstill=note_io_standstill,
    consulting_annual=consulting_annual, consulting_years=consulting_years,
    noncompete_total=noncompete_total, noncompete_years=noncompete_years,
)

# ==============================================================================
# MAIN PANEL
# ==============================================================================
_hdr_left, _hdr_right = st.columns([6, 1])
with _hdr_left:
    st.markdown(
        """
        <div style="margin: 0 0 6px 0;">
            <div style="font-size: 1.75rem; font-weight: 700; color: #ffffff;
                        letter-spacing: -0.02em; line-height: 1.15;">
                RIA M&amp;A Calculator
            </div>
            <div style="font-size: 0.92rem; color: #8b94a8; margin-top: 4px;">
                Buyer-side acquisition economics for Registered Investment Advisors
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with _hdr_right:
    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)
    if st.button("← Home", key="back_home", use_container_width=True):
        st.session_state.show_calculator = False
        st.rerun()

# Out-of-scope warning for mega-RIAs. When AUM > $10B the revenue proxy is
# disabled (fee structures vary too much) so we couldn't auto-scale the
# purchase price either — the analytics below would render as ~0 against
# the default $8M. Custom .mega-banner (vs st.warning's loud yellow block)
# is prominent without being alarmist.
if st.session_state.sec_data is not None and st.session_state.sec_data.aum > 10e9:
    _mega = st.session_state.sec_data
    st.markdown(
        f"""
        <div class="mega-banner">
            <strong>Heads up &mdash;</strong> {_html.escape(_mega.firm_name)}
            ({fmt_dollar(_mega.aum)} AUM) is above this calculator&rsquo;s typical
            deal-size range (~\\$100M&ndash;\\$10B AUM). Revenue and purchase price
            weren&rsquo;t auto-scaled. Enter a realistic deal size in the sidebar
            before reading the analytics.
        </div>
        """.replace("\\$", "$"),
        unsafe_allow_html=True,
    )

# Compliance disclaimer — small but always visible above the tabs so a user
# evaluating output never has to hunt for it.
st.caption(
    "Estimates derived from SEC Form ADV public filings. Not investment advice. "
    "Revenue estimated at 0.75% of AUM — actual figures vary by fee structure."
)

tab1, tab2, tab3, tab4, tab5, tab7, tab6 = st.tabs([
    "Deal Summary", "Pro Forma Financials", "Sensitivity Analysis",
    "Earnout & Seller Economics", "Debt Analysis",
    "Integration Strategy", "Instructions",
])

# ==============================================================================
# TAB 1 -- Deal Summary
# ==============================================================================
with tab1:
    # ---- Valuation Band (banker-MD memo, Part B) ----
    # The headline answer to "what is this firm worth?" — a defensible
    # range built from a multi-factor model the user can stress-test, NOT
    # a single price the user has to guess at.
    st.markdown('<div class="section-header">Estimated Valuation</div>', unsafe_allow_html=True)
    vb1, vb2, vb3 = st.columns(3)
    with vb1:
        st.markdown(metric_card("Low", fmt_dollar(valuation["low"]), "neutral"), unsafe_allow_html=True)
    with vb2:
        st.markdown(metric_card("Mid", fmt_dollar(valuation["mid"]), "accent"), unsafe_allow_html=True)
    with vb3:
        st.markdown(metric_card("High", fmt_dollar(valuation["high"]), "neutral"), unsafe_allow_html=True)
    _f = valuation["factors"]
    # Two clean lines: methodology + factor breakdown + verdict. Avoiding
    # inline bold mid-sentence keeps the typography consistent with the
    # rest of the Streamlit captions on the page.
    # Escape $ signs so Streamlit's markdown doesn't treat them as MathJax
    # delimiters and render the surrounded text in italic math font.
    _adj = fmt_dollar(valuation['adj_ebitda']).replace("$", r"\$")
    _repl = fmt_dollar(valuation['replacement_cost']).replace("$", r"\$")
    st.caption(
        f"Multiple: {valuation['final_multiple']:.1f}× adjusted EBITDA "
        f"(base 7.0× × stacked multipliers). "
        f"Adjusted EBITDA {_adj} = EBITDA + Owner Comp − {_repl} replacement. "
        f"Band width ±{valuation['band_width']*100:.0f}%."
    )
    st.caption(
        "Factors — "
        f"Recurring {_f['recurring']:.2f}× · "
        f"Size {_f['size']:.2f}× · "
        f"Growth {_f['growth']:.2f}× · "
        f"Margin {_f['margin']:.2f}× · "
        f"Book {_f['book_quality']:.2f}× · "
        f"Geo {_f['geography']:.2f}× · "
        f"Key-person {_f['key_person']:.2f}×"
    )
    if valuation["low"] <= purchase_price <= valuation["high"]:
        _verdict = f"Your entered price ({fmt_dollar(purchase_price)}) is in band."
    elif purchase_price < valuation["low"]:
        _verdict = (f"Your entered price ({fmt_dollar(purchase_price)}) is below band "
                    "— possible bargain or hidden risk.")
    else:
        _verdict = (f"Your entered price ({fmt_dollar(purchase_price)}) is above band "
                    "— verify upside drivers or expect margin compression.")
    st.caption(_verdict)
    if aum > 10e9:
        st.caption(
            "Mega-RIA caveat: at $10B+ AUM, valuations are bespoke and driven by "
            "enterprise dynamics (PE rolls, equity stakes, sum-of-the-parts). Recent "
            "aggregator transactions priced at 16-22× EBITDA — see Hellman & Friedman / "
            "Edelman, Stone Point / Mariner, GTCR / Captrust for precedent. Band above "
            "should be read directionally only."
        )

    # ---- Suggested Deal Structure (banker memo, Part C) ----
    # Recommended upfront/note/earnout/rollover mix for this firm profile.
    # User can adopt with one click or keep adjusting sliders manually.
    st.markdown('<div class="section-header">Suggested Deal Structure</div>', unsafe_allow_html=True)
    sd = suggested_deal
    sd1, sd2, sd3, sd4 = st.columns(4)
    with sd1:
        st.markdown(metric_card("Upfront Cash", fmt_pct(sd['upfront']), "accent"), unsafe_allow_html=True)
    with sd2:
        st.markdown(metric_card("Seller Note", fmt_pct(sd['note']), "neutral"), unsafe_allow_html=True)
    with sd3:
        st.markdown(metric_card("Earnout", fmt_pct(sd['earnout']), "neutral"), unsafe_allow_html=True)
    with sd4:
        st.markdown(metric_card("Equity Rollover", fmt_pct(sd['rollover']), "neutral"), unsafe_allow_html=True)
    # Escape $ in the rationale strings so $200M / $1B references don't
    # trigger MathJax italics.
    _rationale_safe = sd['rationale'].replace("$", r"\$")
    st.caption(
        f"Profile · {sd['profile'].replace('_', ' ').title()}. {_rationale_safe} "
        f"Recommended note rate {sd['note_rate']*100:.1f}% over {sd['note_term']} years, "
        f"{sd['earnout_period']}-year earnout capped at {sd['earnout_cap_pct']}%, "
        f"{sd['standstill_years']}-year note standstill if bank-financed."
    )
    _apply_col, _ = st.columns([1, 3])
    with _apply_col:
        if st.button("Apply suggested splits", key="apply_suggested_deal", type="primary", use_container_width=True):
            # Queue the slider keys; _apply_pending() picks them up next rerun.
            _queue_apply("pct_upfront_cash", int(round(sd['upfront'] * 100)))
            _queue_apply("pct_seller_note", int(round(sd['note'] * 100)))
            _queue_apply("pct_earnout", int(round(sd['earnout'] * 100)))
            _queue_apply("pct_equity_rollover", int(round(sd['rollover'] * 100)))
            st.toast(f"Applied {sd['profile'].replace('_', ' ')} structure.", icon="✅")
            st.rerun()

    # ---- Comps Overlay (banker memo, Part D) ----
    # Anchor the firm's implied EBITDA multiple against a curated database
    # so a banker can read "where does this deal price vs comps" at a glance.
    comps_df = _load_comps_df()
    if len(comps_df) > 0:
        st.markdown('<div class="section-header">Comparable Transactions</div>', unsafe_allow_html=True)
        band = lookup_comps_band(aum, pct_recurring, comps_df)
        deal_ebitda_mult = (purchase_price / valuation["adj_ebitda"]) if valuation["adj_ebitda"] > 0 else 0

        ev = band.get("ev_ebitda")
        if ev is None:
            st.caption(
                f"Too few comps in the {band['aum_tier']} x {band['recurring_tier']} slice "
                "to produce a meaningful band. Showing the full transaction table below."
            )
        else:
            if ev["p25"] <= deal_ebitda_mult <= ev["p75"]:
                deal_class, verdict = "positive", "in band"
            elif abs(deal_ebitda_mult - ev["median"]) < 0.5:
                deal_class, verdict = "neutral", "near band edge"
            elif deal_ebitda_mult > ev["p75"]:
                deal_class, verdict = "negative", "above band"
            else:
                deal_class, verdict = "negative", "below band"

            cb1, cb2, cb3, cb4 = st.columns(4)
            with cb1:
                st.markdown(metric_card("P25 EV/EBITDA", f"{ev['p25']:.1f}x", "neutral"), unsafe_allow_html=True)
            with cb2:
                st.markdown(metric_card("Median EV/EBITDA", f"{ev['median']:.1f}x", "accent"), unsafe_allow_html=True)
            with cb3:
                st.markdown(metric_card("P75 EV/EBITDA", f"{ev['p75']:.1f}x", "neutral"), unsafe_allow_html=True)
            with cb4:
                st.markdown(
                    metric_card(f"Your Deal ({verdict})", f"{deal_ebitda_mult:.1f}x", deal_class),
                    unsafe_allow_html=True,
                )

            _slice_label = f"{band['aum_tier']} AUM x {band['recurring_tier']} recurring"
            _fallback_note = " (widened — primary slice was thin)" if band["fallback"] else ""
            st.caption(
                f"Band drawn from N = {band['n_used']} deals in the {_slice_label} slice"
                f"{_fallback_note}. EV/EBITDA shown; EV/Revenue available in the table below."
            )

        plot_df = comps_df.dropna(subset=["ev_ebitda_multiple", "seller_aum"]).copy()
        if len(plot_df) > 0:
            try:
                fig_scatter = px.scatter(
                    plot_df,
                    x="seller_aum",
                    y="ev_ebitda_multiple",
                    color="recurring_tier",
                    symbol="channel",
                    hover_data={
                        "buyer": True, "seller": True,
                        "date": "|%Y-%m-%d", "seller_aum": ":,.0f",
                        "ev_ebitda_multiple": ":.1f",
                        "ev_revenue_multiple": ":.2f",
                    },
                    log_x=True,
                    color_discrete_sequence=COLORS,
                    labels={
                        "seller_aum": "Seller AUM ($, log)",
                        "ev_ebitda_multiple": "EV / EBITDA Multiple",
                        "recurring_tier": "Recurring",
                        "channel": "Channel",
                    },
                )
                fig_scatter.update_traces(marker=dict(size=10, opacity=0.78,
                                                     line=dict(color="rgba(255,255,255,0.15)", width=0.5)))
                if valuation["adj_ebitda"] > 0:
                    fig_scatter.add_trace(go.Scatter(
                        x=[aum], y=[deal_ebitda_mult],
                        mode="markers",
                        name="Your Deal",
                        marker=dict(symbol="star", size=22, color="#fbbf24",
                                    line=dict(color="#0f1115", width=1.5)),
                        hovertemplate=(
                            "Your Deal<br>AUM: $%{x:,.0f}<br>"
                            "EV/EBITDA: %{y:.1f}x<extra></extra>"
                        ),
                    ))
                fig_scatter.update_layout(**plotly_layout(),
                                          title="Peer Transactions — EV/EBITDA by Seller AUM",
                                          height=420)
                st.plotly_chart(fig_scatter, use_container_width=True)
            except Exception as _e:
                st.caption(f"(Peer scatter unavailable: {type(_e).__name__})")

        with st.expander("Browse transaction database", expanded=False):
            f1, f2 = st.columns(2)
            aum_tier_opts = ["All"] + sorted(comps_df["aum_tier"].dropna().unique().tolist())
            channel_opts = ["All"] + sorted(comps_df["channel"].dropna().unique().tolist())
            with f1:
                _aum_filter = st.selectbox("AUM tier", aum_tier_opts, key="comps_aum_filter")
            with f2:
                _ch_filter = st.selectbox("Channel", channel_opts, key="comps_channel_filter")

            tbl = comps_df.copy()
            if _aum_filter != "All":
                tbl = tbl[tbl["aum_tier"] == _aum_filter]
            if _ch_filter != "All":
                tbl = tbl[tbl["channel"] == _ch_filter]

            display = pd.DataFrame({
                "Date": tbl["date"].dt.strftime("%Y-%m-%d"),
                "Buyer": tbl["buyer"],
                "Seller": tbl["seller"],
                "Seller AUM": tbl["seller_aum"].apply(fmt_dollar),
                "EV/EBITDA": tbl["ev_ebitda_multiple"].apply(
                    lambda v: f"{v:.1f}x" if pd.notna(v) else "—"
                ),
                "EV/Revenue": tbl["ev_revenue_multiple"].apply(
                    lambda v: f"{v:.2f}x" if pd.notna(v) else "—"
                ),
            }).sort_values("Date", ascending=False)
            st.dataframe(display, use_container_width=True, hide_index=True)

        st.caption(
            "Comps reflect publicly disclosed terms; private mid-market deals "
            "skew unrepresented and may trade 0.5-1.0x lower than headline "
            f"aggregator transactions. N = {len(comps_df)}."
        )

    st.markdown('<div class="section-header">Purchase Price & Implied Multiples</div>', unsafe_allow_html=True)

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        # Price is the user's anchor — render with accent color so the eye
        # lands there first across the row.
        st.markdown(metric_card("Price", fmt_dollar(purchase_price), "accent"), unsafe_allow_html=True)
    with c2:
        st.markdown(metric_card("Rev Multiple", f"{multiples['revenue_multiple']:.2f}x", "neutral"), unsafe_allow_html=True)
    with c3:
        st.markdown(metric_card("% of AUM", f"{multiples['aum_multiple']:.2f}%", "neutral"), unsafe_allow_html=True)
    with c4:
        st.markdown(metric_card("EBOC Multiple", f"{multiples['eboc_multiple']:.2f}x", "neutral"), unsafe_allow_html=True)
    with c5:
        price_per_client = purchase_price / num_clients if num_clients else 0
        st.markdown(metric_card("Price / Client", fmt_dollar(price_per_client), "neutral"), unsafe_allow_html=True)

    st.markdown('<div class="section-header">EBOC Calculation</div>', unsafe_allow_html=True)
    ec1, ec2, ec3, ec4 = st.columns(4)
    with ec1:
        st.markdown(metric_card("EBITDA", fmt_dollar(ebitda), "neutral"), unsafe_allow_html=True)
    with ec2:
        # Owner Comp Adj uses the AUM-scaled replacement cost, not the flat
        # $200K constant — otherwise EBOC display disagrees with the engine.
        st.markdown(metric_card("Owner Comp Adj.", fmt_dollar(owner_comp - replacement_cost), "neutral"), unsafe_allow_html=True)
    with ec3:
        st.markdown(metric_card("EBOC", fmt_dollar(eboc), "positive"), unsafe_allow_html=True)
    with ec4:
        margin = ebitda / annual_revenue if annual_revenue else 0
        st.markdown(metric_card("EBITDA Margin", fmt_pct(margin), "positive" if margin > 0.3 else "neutral"), unsafe_allow_html=True)

    st.markdown('<div class="section-header">Deal Structure</div>', unsafe_allow_html=True)

    components = ["Upfront Cash", "Seller Note", "Earnout", "Equity Rollover"]
    values = [
        purchase_price * pct_upfront_cash,
        purchase_price * pct_seller_note,
        purchase_price * pct_earnout,
        purchase_price * pct_equity_rollover,
    ]

    fig_waterfall = go.Figure(go.Waterfall(
        name="Deal Structure",
        orientation="v",
        x=components + ["Total"],
        y=values + [0],
        measure=["relative"] * len(components) + ["total"],
        connector=dict(line=dict(color="#2d3748")),
        increasing=dict(marker=dict(color=COLORS[0])),
        totals=dict(marker=dict(color=COLORS[1])),
        text=[fmt_dollar(v) for v in values] + [fmt_dollar(purchase_price)],
        textposition="outside",
    ))
    fig_waterfall.update_layout(**plotly_layout(), title="Deal Structure Waterfall", height=400)
    st.plotly_chart(fig_waterfall, use_container_width=True)

    st.markdown('<div class="section-header">Key Return Metrics</div>', unsafe_allow_html=True)
    k1, k2, k3, k4 = st.columns(4)

    # IRR / CoC come back as None when the buyer has no equity at risk
    # (100% leverage or 0% upfront) — show 'n/a' so a junior analyst can't
    # screenshot a misleading thousands-of-percent figure.
    irr5 = returns.get("irr_yr5")
    if irr5 is None:
        irr_display, irr_class = "n/a", "neutral"
    else:
        irr_display = fmt_pct(irr5)
        irr_class = "positive" if irr5 > 0.15 else ("neutral" if irr5 > 0 else "negative")
    with k1:
        st.markdown(metric_card("5-Year IRR", irr_display, irr_class), unsafe_allow_html=True)
    with k2:
        coc5 = returns.get("coc_yr5")
        if coc5 is None:
            coc_display, coc_class = "n/a", "neutral"
        else:
            coc_display, coc_class = f"{coc5:.2f}x", "positive" if coc5 > 1 else "negative"
        st.markdown(metric_card("5-Yr Cash/Cash", coc_display, coc_class), unsafe_allow_html=True)
    with k3:
        st.markdown(metric_card("Breakeven Year", str(returns["breakeven_year"]), "neutral"), unsafe_allow_html=True)
    with k4:
        # DSCR is NaN in years with no debt — show 'n/a' instead of 0.00x
        # which a credit reviewer reads as a covenant breach.
        yr1_dscr = dscr.iloc[0] if len(dscr) > 0 else float("nan")
        import math as _m
        if _m.isnan(yr1_dscr):
            dscr_display, dscr_class = "n/a", "neutral"
        else:
            dscr_display, dscr_class = f"{yr1_dscr:.2f}x", "positive" if yr1_dscr > 1.25 else "negative"
        st.markdown(metric_card("Year 1 DSCR", dscr_display, dscr_class), unsafe_allow_html=True)

    # Transition comp summary if applicable
    if consulting_annual > 0 or noncompete_total > 0:
        st.markdown('<div class="section-header">Seller Transition Costs (Buyer Expense)</div>', unsafe_allow_html=True)
        t1, t2, t3 = st.columns(3)
        total_consulting = consulting_annual * consulting_years
        with t1:
            st.markdown(metric_card("Consulting Total", fmt_dollar(total_consulting), "neutral"), unsafe_allow_html=True)
        with t2:
            st.markdown(metric_card("Non-Compete Total", fmt_dollar(noncompete_total), "neutral"), unsafe_allow_html=True)
        with t3:
            st.markdown(metric_card("Total Transition", fmt_dollar(total_consulting + noncompete_total), "negative"), unsafe_allow_html=True)


# ==============================================================================
# TAB 2 -- Pro Forma Financials
# ==============================================================================
with tab2:
    st.markdown('<div class="section-header">5-Year Pro Forma P&L</div>', unsafe_allow_html=True)

    display_pf = pro_forma[pro_forma["year"] <= 5].copy()
    pf_cols = ["year", "revenue", "expenses", "transition_comp", "integration_costs",
               "ebitda", "debt_service", "seller_note_payment"]

    # Conditionally show tax benefit column
    if tax_rate > 0:
        pf_cols += ["tax_benefit", "net_cash_flow"]
        col_names = ["Year", "Revenue", "Expenses", "Transition", "Integration",
                     "EBITDA", "Debt Svc", "Note Pmt", "Tax Benefit", "Net Cash Flow"]
    else:
        pf_cols += ["net_cash_flow"]
        col_names = ["Year", "Revenue", "Expenses", "Transition", "Integration",
                     "EBITDA", "Debt Svc", "Note Pmt", "Net Cash Flow"]

    display_pf_fmt = display_pf[pf_cols].copy()
    for col in display_pf_fmt.columns:
        if col != "year":
            display_pf_fmt[col] = display_pf_fmt[col].apply(lambda x: f"${x:,.0f}")
    display_pf_fmt.columns = col_names
    st.dataframe(display_pf_fmt, use_container_width=True, hide_index=True)

    # Combined entity view
    if show_combined and buyer_revenue > 0:
        st.markdown('<div class="section-header">Combined Entity Pro Forma</div>', unsafe_allow_html=True)
        combined = display_pf[["year", "revenue", "ebitda"]].copy()
        combined["buyer_revenue"] = buyer_revenue * ((1 + 0.03) ** combined["year"])
        combined["buyer_ebitda"] = combined["buyer_revenue"] * buyer_margin
        combined["combined_revenue"] = combined["revenue"] + combined["buyer_revenue"]
        combined["combined_ebitda"] = combined["ebitda"] + combined["buyer_ebitda"]
        combined["combined_aum"] = [
            aum * ((1 + rev_growth - (attrition_rate if yr <= 2 else 0)) ** yr)
            + buyer_aum * ((1 + 0.03) ** yr)
            for yr in combined["year"]
        ]

        comb_fmt = combined[["year", "combined_revenue", "combined_ebitda", "combined_aum"]].copy()
        for col in comb_fmt.columns:
            if col != "year":
                comb_fmt[col] = comb_fmt[col].apply(lambda x: f"${x:,.0f}")
        comb_fmt.columns = ["Year", "Combined Revenue", "Combined EBITDA", "Combined AUM"]
        st.dataframe(comb_fmt, use_container_width=True, hide_index=True)

    # Revenue and cash flow chart
    st.markdown('<div class="section-header">Revenue & Cash Flow Trajectory</div>', unsafe_allow_html=True)
    fig_pf = go.Figure()
    pf5 = pro_forma[pro_forma["year"] <= 5]
    fig_pf.add_trace(go.Scatter(
        x=pf5["year"], y=pf5["revenue"], name="Revenue",
        line=dict(color=COLORS[0], width=3), mode="lines+markers",
    ))
    fig_pf.add_trace(go.Scatter(
        x=pf5["year"], y=pf5["net_cash_flow"], name="Net Cash Flow",
        line=dict(color=COLORS[1], width=3), mode="lines+markers",
    ))
    fig_pf.add_trace(go.Scatter(
        x=pf5["year"], y=pf5["ebitda"], name="EBITDA",
        line=dict(color=COLORS[2], width=2, dash="dash"), mode="lines+markers",
    ))
    if tax_rate > 0:
        fig_pf.add_trace(go.Scatter(
            x=pf5["year"], y=pf5["pretax_cash_flow"], name="Pre-Tax Cash Flow",
            line=dict(color=COLORS[4], width=2, dash="dot"), mode="lines+markers",
        ))
    fig_pf.update_layout(
        **plotly_layout(),
        title="5-Year Financial Trajectory",
        yaxis_title="Dollars ($)",
        xaxis_title="Year",
        height=450,
    )
    st.plotly_chart(fig_pf, use_container_width=True)


# ==============================================================================
# TAB 3 -- Sensitivity Analysis
# ==============================================================================
with tab3:
    base_params = dict(
        revenue=annual_revenue, ebitda=ebitda, owner_comp=owner_comp,
        growth_rate=rev_growth, attrition_rate=attrition_rate, aum=aum,
        purchase_price=purchase_price, pct_upfront=pct_upfront_cash,
        pct_seller_note=pct_seller_note, note_rate=note_rate, note_term=note_term,
        pct_earnout=pct_earnout, earnout_period=earnout_period,
        pct_equity_rollover=pct_equity_rollover, pct_self_funded=pct_self_funded,
        loan_rate=loan_rate, loan_term=loan_term,
        integration_costs=integration_costs, annual_synergies=annual_synergies,
        additional_staff=additional_staff, years=7,
        loan_io_years=loan_io_years, loan_balloon=loan_balloon,
        loan_amort_years=loan_amort_years,
        note_standstill_years=note_standstill,
        note_io_during_standstill=note_io_standstill,
        consulting_annual=consulting_annual, consulting_years=consulting_years,
        noncompete_total=noncompete_total, noncompete_years=noncompete_years,
        tax_rate=tax_rate,
        synergy_schedule=_active_synergy_schedule,
    )

    st.markdown('<div class="section-header">IRR Sensitivity: Revenue Multiple vs. Client Attrition</div>', unsafe_allow_html=True)

    multiples_range = [round(x, 1) for x in np.arange(1.0, 4.1, 0.5)]
    attrition_range = [round(x, 2) for x in np.arange(0.0, 0.16, 0.03)]

    irr_table = sensitivity_irr(base_params, multiples_range, attrition_range)

    fig_hm1 = go.Figure(go.Heatmap(
        z=irr_table.values * 100,
        x=[f"{a*100:.0f}%" for a in irr_table.columns],
        y=[f"{m:.1f}x" for m in irr_table.index],
        colorscale=[[0, "#fc8181"], [0.5, "#fefcbf"], [1, "#48bb78"]],
        text=[[f"{v*100:.1f}%" for v in row] for row in irr_table.values],
        texttemplate="%{text}",
        textfont=dict(size=11),
        colorbar=dict(title="IRR %"),
    ))
    fig_hm1.update_layout(
        **plotly_layout(),
        title="5-Year IRR by Multiple & Attrition Rate",
        xaxis_title="Client Attrition Rate",
        yaxis_title="Revenue Multiple",
        height=400,
    )
    st.plotly_chart(fig_hm1, use_container_width=True)

    st.markdown('<div class="section-header">Breakeven Sensitivity: Growth Rate vs. Revenue Multiple</div>', unsafe_allow_html=True)

    growth_range = [round(x, 2) for x in np.arange(0.0, 0.11, 0.02)]
    multiples_range2 = [round(x, 1) for x in np.arange(1.0, 4.1, 0.5)]

    be_table = sensitivity_breakeven(base_params, growth_range, multiples_range2)

    fig_hm2 = go.Figure(go.Heatmap(
        z=be_table.values,
        x=[f"{m:.1f}x" for m in be_table.columns],
        y=[f"{g*100:.0f}%" for g in be_table.index],
        colorscale=[[0, "#48bb78"], [0.5, "#fefcbf"], [1, "#fc8181"]],
        text=[[f"Yr {int(v)}" if v < 8 else ">7" for v in row] for row in be_table.values],
        texttemplate="%{text}",
        textfont=dict(size=11),
        colorbar=dict(title="Years"),
    ))
    fig_hm2.update_layout(
        **plotly_layout(),
        title="Breakeven Year by Growth Rate & Multiple",
        xaxis_title="Revenue Multiple",
        yaxis_title="Revenue Growth Rate",
        height=400,
    )
    st.plotly_chart(fig_hm2, use_container_width=True)


# ==============================================================================
# TAB 4 -- Earnout & Seller Economics
# ==============================================================================
with tab4:
    total_earnout_amount = purchase_price * pct_earnout

    st.markdown('<div class="section-header">Earnout Payout Scenarios</div>', unsafe_allow_html=True)

    # Show earnout config
    cfg1, cfg2, cfg3, cfg4 = st.columns(4)
    with cfg1:
        st.markdown(metric_card("Metric", earnout_metric.split()[0], "neutral"), unsafe_allow_html=True)
    with cfg2:
        st.markdown(metric_card("Floor", f"{earnout_floor}%", "neutral"), unsafe_allow_html=True)
    with cfg3:
        st.markdown(metric_card("Cap", f"{earnout_cap}%", "neutral"), unsafe_allow_html=True)
    with cfg4:
        st.markdown(metric_card("Vesting", "Cliff" if earnout_cliff else "Annual", "neutral"), unsafe_allow_html=True)

    e1, e2, e3 = st.columns(3)
    for idx, (col, sc) in enumerate(zip([e1, e2, e3], earnout_scenarios)):
        with col:
            color = ["neutral", "positive", "negative"][idx]
            st.markdown(metric_card(
                sc["scenario"],
                f"{fmt_dollar(sc['total_payout'])} ({sc['pct_of_max']:.0f}%)",
                color,
            ), unsafe_allow_html=True)

    # Earnout bar chart
    fig_earnout = go.Figure()
    for idx, sc in enumerate(earnout_scenarios):
        fig_earnout.add_trace(go.Bar(
            x=list(range(1, len(sc["yearly_payouts"]) + 1)),
            y=sc["yearly_payouts"],
            name=sc["scenario"],
            marker_color=COLORS[idx],
        ))
    fig_earnout.update_layout(
        **plotly_layout(),
        title="Earnout Payouts by Year & Scenario",
        xaxis_title="Year", yaxis_title="Earnout Payment ($)",
        barmode="group", height=400,
    )
    st.plotly_chart(fig_earnout, use_container_width=True)

    # Seller total proceeds timeline
    st.markdown('<div class="section-header">Total Seller Proceeds Timeline</div>', unsafe_allow_html=True)
    st.caption("Includes: upfront cash + seller note payments + earnout + consulting fees + non-compete payments")

    fig_seller = go.Figure()
    for sc_name in seller_proceeds["scenario"].unique():
        sc_data = seller_proceeds[seller_proceeds["scenario"] == sc_name]
        fig_seller.add_trace(go.Scatter(
            x=sc_data["year"], y=sc_data["cumulative_proceeds"],
            name=sc_name, mode="lines+markers",
        ))
    fig_seller.add_hline(y=purchase_price, line_dash="dash", line_color="#fc8181",
                         annotation_text="Purchase Price")
    fig_seller.update_layout(
        **plotly_layout(),
        title="Cumulative Seller Proceeds (All Sources)",
        xaxis_title="Year", yaxis_title="Cumulative Proceeds ($)",
        height=400,
    )
    st.plotly_chart(fig_seller, use_container_width=True)

    # Seller note amortization
    st.markdown('<div class="section-header">Seller Note Amortization Schedule</div>', unsafe_allow_html=True)
    if len(note_amort) > 0:
        if note_standstill > 0:
            st.caption(f"Standstill period: {note_standstill} years ({'interest-only' if note_io_standstill else 'PIK accrual'})")
        note_fmt = note_amort.copy()
        for col in note_fmt.columns:
            if col not in ("year",):
                note_fmt[col] = note_fmt[col].apply(lambda x: f"${x:,.0f}")
        note_fmt.columns = ["Year", "Beginning Balance", "Payment", "Interest", "Principal", "Ending Balance", "Balloon"]
        st.dataframe(note_fmt, use_container_width=True, hide_index=True)
    else:
        st.info("No seller note in deal structure.")


# ==============================================================================
# TAB 5 -- Debt Analysis
# ==============================================================================
with tab5:
    st.markdown('<div class="section-header">Loan Amortization Schedule</div>', unsafe_allow_html=True)

    # Show loan config summary
    loan_features = []
    if loan_io_years > 0:
        loan_features.append(f"{loan_io_years}-year IO period")
    if loan_balloon:
        loan_features.append(f"Balloon at maturity (amortized over {loan_amort_years} yrs)")
    if loan_features:
        st.caption("Loan features: " + " | ".join(loan_features))

    if len(loan_amort) > 0:
        loan_fmt = loan_amort.copy()
        has_balloon = "balloon_payment" in loan_fmt.columns and loan_fmt["balloon_payment"].sum() > 0
        fmt_cols = [c for c in loan_fmt.columns if c != "year"]
        for col in fmt_cols:
            loan_fmt[col] = loan_fmt[col].apply(lambda x: f"${x:,.0f}")

        if has_balloon:
            loan_fmt.columns = ["Year", "Beg Balance", "Payment", "Interest", "Principal", "End Balance", "Balloon"]
        else:
            loan_fmt = loan_fmt.drop(columns=["balloon_payment"], errors="ignore")
            loan_fmt.columns = ["Year", "Beg Balance", "Payment", "Interest", "Principal", "End Balance"]
        st.dataframe(loan_fmt, use_container_width=True, hide_index=True)

        # Principal vs Interest chart
        st.markdown('<div class="section-header">Principal vs. Interest Over Time</div>', unsafe_allow_html=True)
        fig_debt = go.Figure()
        fig_debt.add_trace(go.Bar(
            x=loan_amort["year"], y=loan_amort["principal_paid"],
            name="Principal", marker_color=COLORS[0],
        ))
        fig_debt.add_trace(go.Bar(
            x=loan_amort["year"], y=loan_amort["interest"],
            name="Interest", marker_color=COLORS[3],
        ))
        if "balloon_payment" in loan_amort.columns and loan_amort["balloon_payment"].sum() > 0:
            fig_debt.add_trace(go.Bar(
                x=loan_amort["year"], y=loan_amort["balloon_payment"],
                name="Balloon", marker_color=COLORS[2],
            ))
        fig_debt.update_layout(
            **plotly_layout(),
            title="Loan Amortization: Principal vs Interest",
            xaxis_title="Year", yaxis_title="Dollars ($)",
            barmode="stack", height=400,
        )
        st.plotly_chart(fig_debt, use_container_width=True)
    else:
        st.info("No debt financing in deal structure (100% self-funded).")

    # DSCR by year
    st.markdown('<div class="section-header">Debt Service Coverage Ratio (DSCR)</div>', unsafe_allow_html=True)

    max_debt_yr = max(loan_term, note_term + note_standstill)
    dscr_data = pro_forma[["year", "dscr"]].copy()
    dscr_data = dscr_data[dscr_data["year"] <= max_debt_yr]

    if len(dscr_data) > 0 and dscr_data["dscr"].sum() > 0:
        fig_dscr = go.Figure()
        fig_dscr.add_trace(go.Bar(
            x=dscr_data["year"], y=dscr_data["dscr"],
            marker_color=[COLORS[1] if d >= 1.25 else COLORS[3] for d in dscr_data["dscr"]],
            text=[f"{d:.2f}x" for d in dscr_data["dscr"]],
            textposition="outside",
        ))
        fig_dscr.add_hline(y=1.25, line_dash="dash", line_color="#ed8936",
                           annotation_text="1.25x Target")
        fig_dscr.update_layout(
            **plotly_layout(),
            title="DSCR by Year",
            xaxis_title="Year", yaxis_title="DSCR",
            height=400, showlegend=False,
        )
        st.plotly_chart(fig_dscr, use_container_width=True)
    else:
        st.info("No debt service to analyze.")


# ==============================================================================
# TAB 7 -- Integration Strategy (banker-MD memo, Part E)
# ==============================================================================
with tab7:
    st.markdown(
        '<div class="section-header">Integration Strategy</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "What the deal is worth *after* you close it. Two levers create value: "
        "cost takeout (eliminate overlapping seats, systems, and overhead) and "
        "revenue uplift (harmonize fees, sell additional services to the "
        "acquired book). Both ramp over years 1-3; both cost money to capture."
    )

    if use_manual_integration:
        st.warning(
            "Manual override is ON in the sidebar. The pro forma is using your "
            "flat integration cost and annual synergy inputs, not the schedule "
            "below. Turn the override off to see this framework drive the deal."
        )

    # ---- Inputs ----
    # Streamlit forbids passing BOTH value= and a key= that's already in
    # session_state. We use widget-specific keys (_w_*) and write the canonical
    # value back to the shared session_state key after the widget renders.
    st.markdown("#### Inputs")
    in1, in2 = st.columns(2)
    with in1:
        st.markdown("**Target firm**")
        _auto_expense = max(0.0, float(annual_revenue) - float(ebitda))
        _default_exp = int(st.session_state.get("int_expense_base", _auto_expense))
        _int_expense_base_w = st.number_input(
            "Target's annual expenses ($)",
            value=_default_exp,
            min_value=0,
            step=50_000,
            key="_w_int_expense_base",
            help="The target firm's total operating expenses before owner "
                 "comp adjustments. We pre-fill this from Revenue − EBITDA "
                 "as a starting point; override with the actual figure from "
                 "the target's financials if you have it.",
        )
        st.session_state["int_expense_base"] = _int_expense_base_w
        st.caption(
            f"Starting value: ${_auto_expense:,.0f} "
            f"(your inputs: Revenue ${annual_revenue:,.0f} − EBITDA ${ebitda:,.0f}). "
            "Edit if diligence shows a different number."
        )
    with in2:
        st.markdown("**What the buyer already has** (limits overlap synergies)")
        _bp_compliance = st.checkbox(
            "Compliance & CCO function",
            value=st.session_state.get("int_buyer_compliance", True),
            key="_w_int_buyer_compliance",
            help="If the buyer already has its own Chief Compliance Officer, "
                 "legal team, and ADV registration, the target's redundant "
                 "headcount can be cut.")
        _bp_tech = st.checkbox(
            "Overlapping tech stack (CRM, portfolio accounting)",
            value=st.session_state.get("int_buyer_tech", True),
            key="_w_int_buyer_tech")
        _bp_backoffice = st.checkbox(
            "Back-office operations team",
            value=st.session_state.get("int_buyer_backoffice", True),
            key="_w_int_buyer_backoffice")
        _bp_planning = st.checkbox(
            "Financial planning shop",
            value=st.session_state.get("int_buyer_planning", False),
            key="_w_int_buyer_planning",
            help="A buyer with an in-house planning team can sell additional "
                 "services to the acquired book, expanding wallet share.")
        st.session_state["int_buyer_compliance"] = _bp_compliance
        st.session_state["int_buyer_tech"] = _bp_tech
        st.session_state["int_buyer_backoffice"] = _bp_backoffice
        st.session_state["int_buyer_planning"] = _bp_planning

    sl1, sl2, sl3 = st.columns(3)
    with sl1:
        _cot = st.slider(
            "Cost takeout (% of target expenses)",
            min_value=10.0, max_value=18.0,
            value=float(st.session_state.get("int_cost_takeout_pct", 0.14)) * 100,
            step=0.5, key="_w_int_cost_takeout_pct",
            help="Realistic capture from eliminating overlapping seats, "
                 "tech licenses, office space, and back-office workflows. "
                 "Industry benchmark: 10-18% of the target's expense base.",
        ) / 100
        st.session_state["int_cost_takeout_pct"] = _cot
    with sl2:
        _rev = st.slider(
            "Revenue uplift (% of target revenue)",
            min_value=5.0, max_value=10.0,
            value=float(st.session_state.get("int_revenue_uplift_pct", 0.07)) * 100,
            step=0.5, key="_w_int_revenue_uplift_pct",
            help="Fee harmonization on the acquired book + additional "
                 "services (planning, tax, insurance) + brand-driven referrals. "
                 "Realistic by year 3: 5-10% of target revenue.",
        ) / 100
        st.session_state["int_revenue_uplift_pct"] = _rev
    with sl3:
        _cap = st.slider(
            "One-time capture cost (× annual synergy run-rate)",
            min_value=1.0, max_value=1.5,
            value=float(st.session_state.get("int_capture_multiple", 1.25)),
            step=0.05, key="_w_int_capture_multiple",
            help="Severance, tech migration, advisor retention bonuses, "
                 "and dual-running systems during the transition. Industry "
                 "rule of thumb: \\$1.00-1.50 of one-time spend per \\$1.00 of "
                 "annual run-rate synergy. Spread 60/30/10 across years 1-3.",
        )
        st.session_state["int_capture_multiple"] = _cap

    # Rebuild for this tab's display.
    _live_buyer_profile = {
        "has_compliance_team": _bp_compliance,
        "has_cco_redundancy": _bp_compliance,  # CCO is part of the compliance function
        "has_tech_stack": _bp_tech,
        "has_planning_shop": _bp_planning,
        "has_back_office": _bp_backoffice,
    }
    schedule = compute_synergy_schedule(
        target_revenue=annual_revenue,
        target_expense_base=_int_expense_base_w,
        buyer_profile=_live_buyer_profile,
        cost_takeout_pct=_cot,
        revenue_uplift_pct=_rev,
        capture_cost_multiple=_cap,
        years=7,
    )

    st.markdown("---")

    # ---- Headline tiles ----
    st.markdown(
        '<div class="section-header">Run-Rate Synergies (Y3+)</div>',
        unsafe_allow_html=True,
    )
    h1, h2, h3, h4 = st.columns(4)
    with h1:
        st.markdown(metric_card(
            "Cost Takeout (run-rate)",
            fmt_dollar(schedule.attrs["runrate_cost_takeout"]),
            "accent",
        ), unsafe_allow_html=True)
    with h2:
        st.markdown(metric_card(
            "Revenue Uplift (run-rate)",
            fmt_dollar(schedule.attrs["runrate_revenue_uplift"]),
            "accent",
        ), unsafe_allow_html=True)
    with h3:
        st.markdown(metric_card(
            "Total Capture Cost (one-time)",
            fmt_dollar(schedule.attrs["total_capture_cost"]),
            "neutral",
        ), unsafe_allow_html=True)
    with h4:
        _npv = schedule.attrs["synergy_npv"]
        st.markdown(metric_card(
            "Synergy NPV @ 10%",
            fmt_dollar(_npv),
            "accent" if _npv > 0 else "neutral",
        ), unsafe_allow_html=True)

    _cap_factor = schedule.attrs["capability_factor"]
    _total_rr = schedule.attrs["runrate_total_synergy"]
    _pct_rev = (_total_rr / annual_revenue * 100) if annual_revenue else 0
    st.caption(
        f"Buyer-capability factor: {_cap_factor:.0%} of addressable. "
        f"Total Y3+ run-rate synergy: {fmt_dollar(_total_rr)} "
        f"({_pct_rev:.1f}% of target revenue). "
        f"Ramp 20% Y1 / 60% Y2 / 90% Y3 / 100% Y4+. "
        f"Capture cost spread 60/30/10 across Y1-Y3."
    )

    # ---- Ramp chart ----
    st.markdown(
        '<div class="section-header">Year-by-Year Ramp</div>',
        unsafe_allow_html=True,
    )
    fig_int = go.Figure()
    fig_int.add_trace(go.Bar(
        x=schedule["year"], y=schedule["cost_takeout"],
        name="Cost Takeout", marker_color="#48bb78",
    ))
    fig_int.add_trace(go.Bar(
        x=schedule["year"], y=schedule["revenue_uplift"],
        name="Revenue Uplift", marker_color="#4299e1",
    ))
    fig_int.add_trace(go.Bar(
        x=schedule["year"], y=-schedule["capture_cost"],
        name="Capture Cost", marker_color="#fc8181",
    ))
    fig_int.add_trace(go.Scatter(
        x=schedule["year"], y=schedule["net_synergy"],
        name="Net Synergy", mode="lines+markers",
        line=dict(color="#f6e05e", width=3),
    ))
    fig_int.update_layout(
        **plotly_layout(),
        barmode="relative",
        title="Synergy Realization vs. Capture Cost by Year",
        xaxis_title="Year",
        yaxis_title="$ Impact on EBITDA",
        height=420,
    )
    st.plotly_chart(fig_int, use_container_width=True)

    # ---- Detail table ----
    st.markdown(
        '<div class="section-header">Synergy Schedule</div>',
        unsafe_allow_html=True,
    )
    disp = schedule.copy()
    for col in ["cost_takeout", "revenue_uplift", "capture_cost",
                "net_synergy", "cumulative_synergy"]:
        disp[col] = disp[col].apply(lambda v: f"${v:,.0f}")
    disp.columns = ["Year", "Cost Takeout", "Revenue Uplift",
                    "Capture Cost", "Net Synergy", "Cumulative"]
    st.dataframe(disp, use_container_width=True, hide_index=True)

    # ---- Worked example ----
    st.markdown("---")
    st.markdown("**Worked example**")
    st.caption(
        "A \\$400M-AUM target with \\$3.2M revenue and \\$2M of annual expenses, "
        "acquired by a \\$1B+ buyer with a full compliance/tech/back-office "
        "stack: at default settings (14% cost takeout, 7% revenue uplift, "
        "1.25× capture), expect roughly \\$500K/year of run-rate synergy "
        "by year 3 (≈15% of target revenue) for a one-time integration "
        "spend of ~\\$630K. The schedule above adjusts those numbers for "
        "your specific inputs."
    )


# ==============================================================================
# TAB 6 -- Instructions
# ==============================================================================
with tab6:
    render_instructions_tab()


# ==============================================================================
# FOOTER -- PDF Export
# ==============================================================================
st.markdown("---")
st.markdown("### Export Analysis")

if st.button("Export Full Analysis to PDF", type="primary"):
    try:
        from pdf_export import generate_pdf
        pdf_bytes = generate_pdf(
            purchase_price=purchase_price,
            multiples=multiples,
            eboc=eboc,
            pro_forma=pro_forma,
            returns=returns,
            loan_amort=loan_amort,
            note_amort=note_amort,
            earnout_scenarios=earnout_scenarios,
            dscr=dscr,
            inputs=dict(
                aum=aum, revenue=annual_revenue, ebitda=ebitda,
                owner_comp=owner_comp, num_clients=num_clients,
                growth_rate=rev_growth, pct_recurring=pct_recurring,
                attrition_rate=attrition_rate, pct_upfront=pct_upfront_cash,
                pct_seller_note=pct_seller_note, pct_earnout=pct_earnout,
                pct_equity_rollover=pct_equity_rollover,
                pct_self_funded=pct_self_funded, loan_rate=loan_rate,
                loan_term=loan_term, integration_costs=integration_costs,
                consulting_annual=consulting_annual, consulting_years=consulting_years,
                noncompete_total=noncompete_total, noncompete_years=noncompete_years,
                tax_rate=tax_rate,
            ),
            synergy_schedule=_active_synergy_schedule,
        )
        st.download_button(
            label="Download PDF",
            data=pdf_bytes,
            file_name="ria_ma_analysis.pdf",
            mime="application/pdf",
        )
    except ImportError:
        st.warning("PDF export requires the `fpdf2` package. Install with: pip install fpdf2")
    except Exception as e:
        # Don't surface raw exception text — it can leak file paths or
        # other internals into the browser. Log type + message server-side
        # via Streamlit Cloud's container logs; show a generic message.
        import logging
        logging.exception("PDF generation failed (%s)", type(e).__name__)
        st.error("PDF generation failed. Please try again or contact support if the issue persists.")

render_site_footer()
