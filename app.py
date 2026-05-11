"""
RIA M&A Calculator — Streamlit Application
Buyer-side economics for acquiring a Registered Investment Advisor.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from calculations import (
    compute_eboc, implied_multiples, build_pro_forma,
    compute_irr_and_returns, build_loan_amortization,
    build_seller_note_amortization, compute_dscr,
    compute_earnout_scenarios, compute_seller_total_proceeds,
    sensitivity_irr, sensitivity_breakeven,
)
import sec_lookup
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

# -- Page config ---------------------------------------------------------------
st.set_page_config(page_title="RIA M&A Calculator", page_icon="📊", layout="wide")

# -- Theme state (initialized BEFORE CSS injection so the first paint is correct)
if "theme" not in st.session_state:
    st.session_state.theme = "dark"

_DARK_CSS = """
<style>
    .stApp { background-color: #0e1117; }
    .metric-card {
        background: linear-gradient(135deg, #1a1f2e 0%, #16192b 100%);
        border: 1px solid #2d3748;
        border-radius: 12px;
        padding: 16px 12px;
        text-align: center;
        margin: 5px;
        min-height: 110px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .metric-card h3 {
        color: #8b95a5;
        font-size: 0.7rem;
        font-weight: 600;
        margin-bottom: 8px;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .metric-card h2 {
        color: #e2e8f0;
        font-size: 1.5rem;
        font-weight: 700;
        margin: 0;
        white-space: nowrap;
    }
    .metric-card .positive { color: #48bb78; }
    .metric-card .negative { color: #fc8181; }
    .metric-card .neutral { color: #63b3ed; }
    .section-header {
        color: #e2e8f0;
        border-bottom: 2px solid #4a90d9;
        padding-bottom: 8px;
        margin: 25px 0 15px 0;
        font-size: 1.1rem;
        font-weight: 600;
    }
    div[data-testid="stSidebar"] {
        background-color: #131722;
    }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        background-color: #1a1f2e;
        border-radius: 8px 8px 0 0;
        padding: 10px 20px;
        color: #8b95a5;
    }
    .stTabs [aria-selected="true"] {
        background-color: #2d3748;
        color: #e2e8f0;
    }
    /* Compact sidebar inputs */
    div[data-testid="stSidebar"] .stTextInput > div > div > input {
        font-family: 'SF Mono', 'Consolas', monospace;
        font-size: 0.95rem;
    }
    /* Constrain tooltip width inside the sidebar so the help-icon popover
       doesn't overflow left at narrow viewports. */
    div[data-testid="stSidebar"] div[role="tooltip"] {
        max-width: 240px;
        white-space: normal;
        word-wrap: break-word;
    }
    /* Site footer (dark) */
    .site-footer {
        border-top: 1px solid #2d3748;
        margin: 36px auto 0 auto;
        padding: 18px 12px 28px 12px;
        text-align: center;
        color: #8b95a5;
        font-size: 0.78rem;
        line-height: 1.55;
        max-width: 880px;
    }
    .site-footer a { color: #63b3ed; text-decoration: none; }
    .site-footer a:hover { text-decoration: underline; }
    .site-footer .footer-meta { color: #6b7280; margin-top: 8px; }
</style>
"""

_LIGHT_CSS = """
<style>
    .stApp { background-color: #ffffff; color: #1a202c; }
    .metric-card {
        background: linear-gradient(135deg, #f7fafc 0%, #edf2f7 100%);
        border: 1px solid #cbd5e0;
        border-radius: 12px;
        padding: 16px 12px;
        text-align: center;
        margin: 5px;
        min-height: 110px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .metric-card h3 {
        color: #4a5568;
        font-size: 0.7rem;
        font-weight: 600;
        margin-bottom: 8px;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .metric-card h2 {
        color: #1a202c;
        font-size: 1.5rem;
        font-weight: 700;
        margin: 0;
        white-space: nowrap;
    }
    .metric-card .positive { color: #2f855a; }
    .metric-card .negative { color: #c53030; }
    .metric-card .neutral { color: #2b6cb0; }
    .section-header {
        color: #1a202c;
        border-bottom: 2px solid #2b6cb0;
        padding-bottom: 8px;
        margin: 25px 0 15px 0;
        font-size: 1.1rem;
        font-weight: 600;
    }
    div[data-testid="stSidebar"] {
        background-color: #f7fafc;
    }
    div[data-testid="stSidebar"] * { color: #1a202c; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        background-color: #edf2f7;
        border-radius: 8px 8px 0 0;
        padding: 10px 20px;
        color: #4a5568;
    }
    .stTabs [aria-selected="true"] {
        background-color: #ffffff;
        color: #1a202c;
        border: 1px solid #cbd5e0;
        border-bottom: none;
    }
    div[data-testid="stSidebar"] .stTextInput > div > div > input {
        font-family: 'SF Mono', 'Consolas', monospace;
        font-size: 0.95rem;
    }
    div[data-testid="stSidebar"] div[role="tooltip"] {
        max-width: 240px;
        white-space: normal;
        word-wrap: break-word;
    }
    /* Site footer (light) */
    .site-footer {
        border-top: 1px solid #cbd5e0;
        margin: 36px auto 0 auto;
        padding: 18px 12px 28px 12px;
        text-align: center;
        color: #4a5568;
        font-size: 0.78rem;
        line-height: 1.55;
        max-width: 880px;
    }
    .site-footer a { color: #2b6cb0; text-decoration: none; }
    .site-footer a:hover { text-decoration: underline; }
    .site-footer .footer-meta { color: #718096; margin-top: 8px; }
</style>
"""


def _is_light() -> bool:
    return st.session_state.get("theme", "dark") == "light"


# Inject the active theme's CSS.
st.markdown(_LIGHT_CSS if _is_light() else _DARK_CSS, unsafe_allow_html=True)


def render_site_footer():
    """Render the site footer. Safe to call on both the welcome page and
    the calculator page; the .site-footer class adapts to the active theme."""
    sec_url = (
        "https://www.sec.gov/data-research/sec-markets-data/"
        "information-about-registered-investment-advisers-exempt-reporting-advisers"
    )
    st.markdown(
        f"""
        <div class="site-footer">
            RIA M&amp;A Calculator &middot; RAUM in Dollars &middot;
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


def _toggle_theme():
    """Callback for the theme switch button — flip dark <-> light."""
    st.session_state.theme = "light" if st.session_state.get("theme", "dark") == "dark" else "dark"


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
    """Return the Plotly layout kwargs for the currently active theme.

    Replaces a previous module-level PLOTLY_LAYOUT constant so charts re-render
    correctly when the user toggles dark/light mode mid-session."""
    if _is_light():
        return dict(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(247,250,252,0.9)",
            font=dict(color="#1a202c", size=12),
            xaxis=dict(gridcolor="#e2e8f0", zerolinecolor="#cbd5e0"),
            yaxis=dict(gridcolor="#e2e8f0", zerolinecolor="#cbd5e0"),
            margin=dict(l=40, r=40, t=50, b=40),
            legend=dict(bgcolor="rgba(0,0,0,0)"),
        )
    return dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(26,31,46,0.8)",
        font=dict(color="#e2e8f0", size=12),
        xaxis=dict(gridcolor="#2d3748", zerolinecolor="#2d3748"),
        yaxis=dict(gridcolor="#2d3748", zerolinecolor="#2d3748"),
        margin=dict(l=40, r=40, t=50, b=40),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
    )

COLORS = ["#4a90d9", "#48bb78", "#ed8936", "#fc8181", "#9f7aea", "#63b3ed"]


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
    """Render the welcome/landing page with sidebar hidden."""
    # Theme toggle, top-right. Placed BEFORE the welcome container so the
    # absolute hero block can't cover it. Use a 3-column trick to right-align.
    _wl, _wm, _wr = st.columns([8, 1, 1])
    with _wr:
        _icon = "☀️" if _is_light() else "🌙"
        st.button(
            _icon,
            key="welcome_theme_toggle",
            on_click=_toggle_theme,
            help="Toggle day/dark mode",
            use_container_width=True,
        )

    _light = _is_light()
    _bg_card = "#ffffff" if _light else "#1a1f2e"
    _border_card = "#cbd5e0" if _light else "#2d3748"
    _h1_color = "#1a202c" if _light else "#e2e8f0"
    _subtitle_color = "#4a5568" if _light else "#8b95a5"
    _divider_color = "#2b6cb0" if _light else "#4a90d9"
    _card_h3 = "#1a202c" if _light else "#e2e8f0"
    _card_p = "#4a5568" if _light else "#6b7280"
    st.markdown(f"""
    <style>
        [data-testid="stSidebar"] {{ display: none; }}
        [data-testid="stSidebarCollapsedControl"] {{ display: none; }}
        .welcome-container {{
            max-width: 880px;
            margin: 0 auto;
            padding: 20px 20px 40px 20px;
        }}
        .welcome-header {{
            text-align: center;
            margin-bottom: 48px;
        }}
        .welcome-header h1 {{
            color: {_h1_color};
            font-size: 2.4rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            margin-bottom: 12px;
            line-height: 1.2;
        }}
        .welcome-header .subtitle {{
            color: {_subtitle_color};
            font-size: 1.05rem;
            font-weight: 400;
            line-height: 1.6;
            max-width: 560px;
            margin: 0 auto;
        }}
        .welcome-divider {{
            width: 48px;
            height: 2px;
            background-color: {_divider_color};
            margin: 20px auto;
        }}
        .feature-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 14px;
            margin-bottom: 48px;
        }}
        @media (max-width: 768px) {{
            .feature-grid {{ grid-template-columns: repeat(2, 1fr); }}
        }}
        @keyframes cardFadeUp {{
            from {{ opacity: 0; transform: translateY(18px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .feature-card {{
            background: {_bg_card};
            border: 1px solid {_border_card};
            border-radius: 8px;
            padding: 22px 18px;
            transition: transform 0.25s ease, border-color 0.25s ease, box-shadow 0.25s ease;
            animation: cardFadeUp 0.5s ease both;
        }}
        .feature-card:nth-child(1) {{ animation-delay: 0.05s; }}
        .feature-card:nth-child(2) {{ animation-delay: 0.12s; }}
        .feature-card:nth-child(3) {{ animation-delay: 0.19s; }}
        .feature-card:nth-child(4) {{ animation-delay: 0.26s; }}
        .feature-card:nth-child(5) {{ animation-delay: 0.33s; }}
        .feature-card:nth-child(6) {{ animation-delay: 0.40s; }}
        .feature-card:hover {{
            transform: translateY(-3px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.18);
        }}
        .feature-card.fc-blue:hover   {{ border-color: #4a90d9; }}
        .feature-card.fc-green:hover  {{ border-color: #48bb78; }}
        .feature-card.fc-amber:hover  {{ border-color: #ed8936; }}
        .feature-card.fc-purple:hover {{ border-color: #9f7aea; }}
        .feature-card.fc-cyan:hover   {{ border-color: #63b3ed; }}
        .feature-card.fc-rose:hover   {{ border-color: #f687b3; }}
        .feature-icon {{
            width: 32px;
            height: 32px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.9rem;
            margin-bottom: 12px;
        }}
        .fi-blue   {{ background: rgba(74,144,217,0.15); color: #4a90d9; }}
        .fi-green  {{ background: rgba(72,187,120,0.15); color: #48bb78; }}
        .fi-amber  {{ background: rgba(237,137,54,0.15); color: #ed8936; }}
        .fi-purple {{ background: rgba(159,122,234,0.15); color: #9f7aea; }}
        .fi-cyan   {{ background: rgba(99,179,237,0.15); color: #63b3ed; }}
        .fi-rose   {{ background: rgba(246,135,179,0.15); color: #f687b3; }}
        .feature-card h3 {{
            color: {_card_h3};
            font-size: 0.88rem;
            font-weight: 600;
            margin-bottom: 6px;
        }}
        .feature-card p {{
            color: {_card_p};
            font-size: 0.78rem;
            line-height: 1.5;
            margin: 0;
        }}
    </style>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div class="welcome-container">
        <div class="welcome-header">
            <h1>RIA M&A Calculator</h1>
            <div class="welcome-divider"></div>
            <p class="subtitle">
                Buyer-side acquisition modeling for Registered Investment Advisors.
                Structure deals, model financing, and stress-test returns.
            </p>
        </div>
        <div class="feature-grid">
            <div class="feature-card fc-blue">
                <div class="feature-icon fi-blue">&#9670;</div>
                <h3>Deal Structuring</h3>
                <p>Model upfront cash, seller notes, earnouts, and equity rollover splits</p>
            </div>
            <div class="feature-card fc-green">
                <div class="feature-icon fi-green">&#9638;</div>
                <h3>Pro Forma Analysis</h3>
                <p>5-year P&L with revenue growth, attrition, and cost synergies</p>
            </div>
            <div class="feature-card fc-amber">
                <div class="feature-icon fi-amber">&#9686;</div>
                <h3>Return Metrics</h3>
                <p>IRR, cash-on-cash, breakeven analysis, and DSCR tracking</p>
            </div>
            <div class="feature-card fc-purple">
                <div class="feature-icon fi-purple">&#9649;</div>
                <h3>Sensitivity Tables</h3>
                <p>Two-way heatmaps across multiples, growth, and attrition scenarios</p>
            </div>
            <div class="feature-card fc-cyan">
                <div class="feature-icon fi-cyan">&#9655;</div>
                <h3>Earnout Modeling</h3>
                <p>Floor, cap, cliff vesting, and multi-metric performance thresholds</p>
            </div>
            <div class="feature-card fc-rose">
                <div class="feature-icon fi-rose">&#9744;</div>
                <h3>Debt Analysis</h3>
                <p>Amortization schedules with I/O periods, balloon payments, and standstill terms</p>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    _col1, col_btn, _col3 = st.columns([1, 1, 1])
    with col_btn:
        if st.button("Open Calculator", type="primary", use_container_width=True):
            st.session_state.show_calculator = True
            st.rerun()

    render_site_footer()


def render_instructions_tab():
    """Render the instructions/help tab with navigation guide and scenarios."""
    st.markdown("""
    <style>
        .guide-card {
            background: #1a1f2e;
            border: 1px solid #2d3748;
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 16px;
        }
        .guide-card h3 {
            color: #e2e8f0;
            font-size: 1rem;
            font-weight: 600;
            margin-bottom: 14px;
            padding-bottom: 8px;
            border-bottom: 1px solid #2d3748;
        }
        .guide-card p, .guide-card li {
            color: #a0aec0;
            font-size: 0.86rem;
            line-height: 1.7;
        }
        .guide-card strong { color: #e2e8f0; }
        .guide-card code {
            background: #0e1117;
            color: #4a90d9;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.8rem;
        }
        .step-row {
            display: flex;
            align-items: flex-start;
            gap: 14px;
            margin-bottom: 16px;
        }
        .step-num {
            flex-shrink: 0;
            width: 28px;
            height: 28px;
            background: #4a90d9;
            color: #0e1117;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 0.8rem;
            margin-top: 2px;
        }
        .step-content { flex: 1; }
        .step-content strong { color: #e2e8f0; }
        .step-content p {
            color: #a0aec0;
            font-size: 0.86rem;
            line-height: 1.6;
            margin: 0;
        }
        .scenario-box {
            background: linear-gradient(135deg, #1a1f2e 0%, #16192b 100%);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 16px;
        }
        .scenario-box h4 {
            font-size: 0.95rem;
            font-weight: 600;
            margin-bottom: 16px;
        }
        .scenario-blue h4 { color: #4a90d9; border-left: 3px solid #4a90d9; padding-left: 12px; }
        .scenario-green h4 { color: #48bb78; border-left: 3px solid #48bb78; padding-left: 12px; }
        .scenario-box table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 14px;
        }
        .scenario-box th {
            text-align: left;
            color: #8b95a5;
            font-size: 0.72rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            padding: 6px 10px;
            border-bottom: 1px solid #2d3748;
        }
        .scenario-box td {
            color: #e2e8f0;
            font-size: 0.84rem;
            padding: 6px 10px;
            border-bottom: 1px solid rgba(45,55,72,0.5);
        }
        .scenario-box .note {
            color: #8b95a5;
            font-size: 0.78rem;
            font-style: italic;
            line-height: 1.6;
            margin-top: 8px;
        }
        .nav-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
            margin-top: 12px;
        }
        @media (max-width: 768px) {
            .nav-grid { grid-template-columns: 1fr; }
        }
        .nav-item {
            background: #0e1117;
            border: 1px solid #2d3748;
            border-radius: 6px;
            padding: 14px 16px;
        }
        .nav-item strong {
            color: #e2e8f0;
            font-size: 0.84rem;
            display: block;
            margin-bottom: 4px;
        }
        .nav-item span {
            color: #6b7280;
            font-size: 0.76rem;
            line-height: 1.5;
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
    """Text input that displays and accepts comma-formatted dollar amounts."""
    if key not in st.session_state:
        st.session_state[key] = f"{default:,.0f}"
    raw = st.sidebar.text_input(label, key=key, help=help_text)
    try:
        cleaned = raw.replace(",", "").replace("$", "").replace(" ", "").strip()
        val = int(float(cleaned))
        # Show formatted feedback if user typed raw digits
        if raw != f"{val:,.0f}" and raw == cleaned:
            st.sidebar.caption(f"= ${val:,.0f}")
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
            _allowed, _remaining, _wait_sec = _sec_lookup_quota()
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
                if st.sidebar.button("Load SEC data", key="sec_load_btn", type="primary", use_container_width=True):
                    _record_sec_lookup()
                    st.session_state.sec_data = sec_lookup.get_firm_data(
                        _matches[_sel_idx].crd, _adv_df
                    )
                    st.session_state.pending_reconcile = True
                    st.rerun()
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
    _as_of = f" — as of {_sec.as_of_date}" if _sec.as_of_date else ""
    st.sidebar.caption(f"📄 SEC: **{_sec.firm_name}**{_as_of}")
    # "Report incorrect data" mailto. urllib.parse.quote handles ampersands,
    # accents, etc. in firm names so the subject line stays well-formed.
    _report_subject = urllib.parse.quote(
        f"ADV data issue: {_sec.firm_name} (CRD {_sec.crd})"
    )
    st.sidebar.markdown(
        f"<small>[Report incorrect data](mailto:dhruvjani7@gmail.com?subject={_report_subject})</small>",
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Clear SEC data", key="sec_clear_btn"):
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


def _sec_field_badge(field_key: str, sec_value, fmt_fn, label: str):
    """Render a small caption + revert button below an input.
    Clicking the button queues the SEC value for the input on the next rerun."""
    if st.session_state.sec_data is None or sec_value is None:
        return
    col1, col2 = st.sidebar.columns([3, 1])
    col1.caption(f"SEC: {fmt_fn(sec_value)}")
    if col2.button("↺", key=f"revert_{field_key}", help=f"Use SEC value for {label}"):
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

if "num_clients" not in st.session_state:
    st.session_state["num_clients"] = 200
num_clients = st.sidebar.number_input(
    "Number of Clients", step=10, key="num_clients"
)
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
    "Revenue Growth Rate (%)", 0.0, _GROWTH_SLIDER_MAX, step=0.5, key="rev_growth_pct",
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

    @st.dialog(f"Apply SEC data: {sec.firm_name}")
    def _dlg():
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
            _row("revenue", "Annual Revenue · *estimate only* (AUM × 0.75%, not filed)",
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
            _row("rev_growth_pct", "Revenue Growth · *AUM proxy* (not filed revenue growth)",
                 f"{cur_growth:.1f}%", growth_pct, sec_disp)

        st.markdown("---")
        c1, c2 = st.columns(2)
        if c1.button("Apply selected", type="primary", use_container_width=True):
            applied = 0
            for key, (choice, sec_value) in decisions.items():
                if choice.startswith("SEC:"):
                    if key in ("aum", "revenue"):
                        _queue_apply(key, f"{int(sec_value):,}")
                    else:
                        _queue_apply(key, sec_value)
                    applied += 1
            st.session_state.pending_reconcile = False
            if applied:
                st.toast(f"Applied {applied} SEC value{'s' if applied != 1 else ''}.", icon="✅")
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

pct_upfront_cash = st.sidebar.slider("% Upfront Cash", 0, 100, 60, 5) / 100
pct_seller_note = st.sidebar.slider("% Seller Note", 0, 100, 20, 5) / 100
pct_earnout = st.sidebar.slider("% Earnout", 0, 100, 15, 5) / 100
pct_equity_rollover = st.sidebar.slider("% Equity Rollover", 0, 100, 5, 5) / 100

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
eboc = compute_eboc(ebitda, owner_comp)
multiples = implied_multiples(purchase_price, annual_revenue, aum, eboc)

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
)

returns = compute_irr_and_returns(
    pro_forma, purchase_price, pct_upfront_cash, pct_self_funded, pct_equity_rollover,
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
_hdr_left, _hdr_theme, _hdr_right = st.columns([5, 1, 1])
with _hdr_left:
    st.markdown("# RIA M&A Calculator")
    st.markdown("*Buyer-side acquisition economics for Registered Investment Advisors*")
with _hdr_theme:
    st.markdown("<br>", unsafe_allow_html=True)
    _theme_icon = "☀️" if _is_light() else "🌙"
    st.button(
        _theme_icon,
        key="calc_theme_toggle",
        on_click=_toggle_theme,
        help="Toggle day/dark mode",
        use_container_width=True,
    )
with _hdr_right:
    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("Home", key="back_home", use_container_width=True):
        st.session_state.show_calculator = False
        st.rerun()

# Compliance disclaimer — small but always visible above the tabs so a user
# evaluating output never has to hunt for it. st.caption is unobtrusive vs.
# st.info, which would compete with the tab nav for attention.
st.caption(
    "Estimates derived from SEC Form ADV public filings. Not investment advice. "
    "Revenue estimated at 0.75% of AUM — actual figures vary by fee structure."
)

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Deal Summary", "Pro Forma Financials", "Sensitivity Analysis",
    "Earnout & Seller Economics", "Debt Analysis", "Instructions",
])

# ==============================================================================
# TAB 1 -- Deal Summary
# ==============================================================================
with tab1:
    st.markdown('<div class="section-header">Purchase Price & Implied Multiples</div>', unsafe_allow_html=True)

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.markdown(metric_card("Price", fmt_dollar(purchase_price), "neutral"), unsafe_allow_html=True)
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
        st.markdown(metric_card("Owner Comp Adj.", fmt_dollar(owner_comp - 200_000), "neutral"), unsafe_allow_html=True)
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

    irr5 = returns.get("irr_yr5", 0)
    irr_class = "positive" if irr5 > 0.15 else ("neutral" if irr5 > 0 else "negative")
    with k1:
        st.markdown(metric_card("5-Year IRR", fmt_pct(irr5), irr_class), unsafe_allow_html=True)
    with k2:
        coc5 = returns.get("coc_yr5", 0)
        st.markdown(metric_card("5-Yr Cash/Cash", f"{coc5:.2f}x", "positive" if coc5 > 1 else "negative"), unsafe_allow_html=True)
    with k3:
        st.markdown(metric_card("Breakeven Year", str(returns["breakeven_year"]), "neutral"), unsafe_allow_html=True)
    with k4:
        yr1_dscr = dscr.iloc[0] if len(dscr) > 0 else 0
        st.markdown(metric_card("Year 1 DSCR", f"{yr1_dscr:.2f}x", "positive" if yr1_dscr > 1.25 else "negative"), unsafe_allow_html=True)

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
        st.error(f"PDF generation failed: {e}")

render_site_footer()
