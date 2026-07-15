"""
Deal persistence for the RIA M&A Calculator — share links + saved deals.

Two independent layers:

1. Share links (zero-auth). The full deal state is serialized to JSON,
   zlib-compressed, and base64url-encoded into a `?d=...` query param.
   Anyone opening the link gets the exact modeled deal — no account needed.
   `apply_deal_from_url()` decodes the param on app start and queues a full
   input restore through app.py's `_queue_apply` / `_apply_pending` pattern.

2. Saved deals (auth-gated). A storage-adapter pair behind the `DealStore`
   ABC: `SQLiteStore` (stdlib, file at data/deals.db — dev/tests only; the
   Streamlit Cloud filesystem is EPHEMERAL, so anything saved there dies on
   the next container restart) and `SupabaseStore` (activated when
   st.secrets has SUPABASE_URL + SUPABASE_KEY; the real production store).
   All queries are scoped by the signed-in email at the application layer.

This module must import cleanly WITHOUT Streamlit so tests can exercise
capture/restore/encode/store logic headlessly — hence the guarded import
and the injectable `session` / `queue_fn` parameters.
"""

import base64
import json
import logging
import math
import sqlite3
import uuid
import zlib
import html as _html
from abc import ABC, abstractmethod
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

# Operator visibility. Streamlit Cloud surfaces stderr in the app logs;
# without this, Supabase outages, quota exhaustion and schema drift are
# all silent. Never log PII (emails, deal economics) — message only.
_log = logging.getLogger(__name__)

try:
    import streamlit as st
except ImportError:  # headless contexts (tests, tooling) — UI fns become no-ops
    st = None

# Where share links point. Streamlit reads the param back via st.query_params.
APP_BASE_URL = "https://ria-ma-calc.streamlit.app"

# Bump when the meaning of a manifest key changes (not when keys are merely
# added — unknown keys are ignored on restore, absent keys are skipped, so
# additions are forward- and backward-compatible within v1).
SCHEMA_VERSION = 1

# Dev-only SQLite location (gitignored; ephemeral on Streamlit Cloud).
_SQLITE_DEFAULT_PATH = Path(__file__).parent / "data" / "deals.db"


# ==============================================================================
# DEAL-STATE MANIFEST — the single source of truth
# ==============================================================================
# Maps every persistable widget/session key in app.py to a type tag that
# drives capture normalization and restore coercion:
#
#   "currency"  currency_input() text widgets — session value MUST be a
#               comma-formatted string ("1,234,567"); captured as int.
#   "count"     count_input() text widgets — same comma-string contract.
#   "float"     float sliders (and canonical Integration-tab fractions).
#   "int"       int sliders and number_inputs.
#   "bool"      checkboxes.
#   "str"       radio/selectbox — restored only if the value is in
#               _STR_OPTIONS (a tampered link must never crash a widget).
#
# MAINTENANCE: when app.py gains a new keyed input that belongs to the deal
# (not transient UI), add it here with the right tag, add its bounds to
# _NUMERIC_RANGES if it's a slider/number_input, add options to _STR_OPTIONS
# if it's a radio/selectbox, and add a mirror to _INTEGRATION_MIRRORS if it
# uses the Integration tab's _w_-shadow pattern. Keys deliberately EXCLUDED:
# sec_search / sec_selected_idx_* / reconcile_radio_* / buttons (transient),
# comps_aum_filter / comps_channel_filter (view-only filters whose options
# are data-dependent), _w_int_* (widget shadows — restored as mirrors of the
# canonical int_* keys, never captured), show_calculator (welcome-page gate —
# set directly by apply_deal_from_url, not part of the deal).
DEAL_STATE_KEYS = {
    # -- Target Firm (sidebar top) --------------------------------------------
    "aum": "currency",
    "revenue": "currency",
    "ebitda": "currency",
    "owner_comp": "currency",
    "num_clients": "count",
    "rev_growth_pct": "float",
    "pct_recurring": "int",
    "attrition_rate": "float",
    # -- Deal Structure expander ----------------------------------------------
    "price_method": "str",
    "purchase_price": "currency",
    "rev_multiple": "float",
    "pct_upfront_cash": "int",
    "pct_seller_note": "int",
    "pct_earnout": "int",
    "pct_equity_rollover": "int",
    "note_rate": "float",
    "note_term": "int",
    "note_standstill": "int",
    "note_io": "bool",
    "earnout_period": "int",
    "earnout_metric": "str",
    "earnout_floor": "int",
    "earnout_cap": "int",
    "earnout_cliff": "bool",
    # -- Debt & Tax expander ----------------------------------------------------
    "pct_self_funded": "int",
    "loan_rate": "float",
    "loan_term": "int",
    "loan_io": "int",  # interest-only YEARS (number_input), not a bool
    "loan_balloon": "bool",
    "loan_amort_yrs": "int",
    "tax_rate": "int",
    # -- Advanced expander ------------------------------------------------------
    "consulting_fee": "currency",
    "consult_yrs": "int",
    "noncompete": "currency",
    "nc_yrs": "int",
    "manual_integration": "bool",
    "integration": "currency",
    "synergies": "currency",
    "additional_staff": "int",
    "integration_months": "int",
    "show_combined": "bool",
    "buyer_aum": "currency",
    "buyer_rev": "currency",
    "buyer_margin": "int",
    # -- Integration tab (canonical keys; _w_int_* shadows restored as mirrors) --
    "int_expense_base": "int",
    "int_cost_takeout_pct": "float",   # FRACTION (0.10–0.18); widget shows ×100
    "int_revenue_uplift_pct": "float",  # FRACTION (0.05–0.10); widget shows ×100
    "int_capture_multiple": "float",
    "int_buyer_compliance": "bool",
    "int_buyer_tech": "bool",
    "int_buyer_backoffice": "bool",
    "int_buyer_planning": "bool",
}

# Fixed option lists for "str" keys. Restore drops values not in the list —
# an out-of-options session value makes st.radio/st.selectbox raise.
_STR_OPTIONS = {
    "price_method": ("Enter Price", "Select Multiple"),
    "earnout_metric": ("Revenue Retention", "AUM Retention", "Client Retention"),
}

# Widget bounds for sliders/number_inputs, copied from app.py definitions.
# Restore clamps into range because Streamlit raises StreamlitAPIException on
# out-of-range session values — a tampered or stale link must degrade, not
# brick the app. (None = unbounded on that side.)
_NUMERIC_RANGES = {
    "rev_growth_pct": (-10.0, 30.0),
    "pct_recurring": (0, 100),
    "attrition_rate": (0.0, 20.0),
    "rev_multiple": (1.0, 5.0),
    "pct_upfront_cash": (0, 100),
    "pct_seller_note": (0, 100),
    "pct_earnout": (0, 100),
    "pct_equity_rollover": (0, 100),
    "note_rate": (0.0, 10.0),
    "note_term": (1, 10),
    "note_standstill": (0, 5),
    "earnout_period": (1, 5),
    "earnout_floor": (0, 100),
    "earnout_cap": (100, 150),
    "pct_self_funded": (0, 100),
    "loan_rate": (0.0, 12.0),
    "loan_term": (1, 15),
    "loan_io": (0, 5),
    "loan_amort_yrs": (2, 30),  # dynamic min loan_term+1 handled in restore
    "consult_yrs": (0, 5),
    "nc_yrs": (0, 7),
    "additional_staff": (0, 10),
    "integration_months": (3, 36),
    "tax_rate": (0, 50),
    "buyer_margin": (0, 60),
    "int_expense_base": (0, None),
    "int_cost_takeout_pct": (0.10, 0.18),
    "int_revenue_uplift_pct": (0.05, 0.10),
    "int_capture_multiple": (1.0, 1.5),
}

# Integration tab uses widget-specific _w_int_* keys and writes canonical
# values back to int_* after render (see app.py ~2867-2956). Once the _w_
# widgets have rendered they own their session values, so restoring the
# canonical key alone silently no-ops. We therefore queue BOTH: the canonical
# key AND its widget shadow, scaling fractions to the percent-space the
# widget operates in. Map: canonical -> (widget key, scale multiplier).
_INTEGRATION_MIRRORS = {
    "int_expense_base": ("_w_int_expense_base", 1),
    "int_cost_takeout_pct": ("_w_int_cost_takeout_pct", 100.0),
    "int_revenue_uplift_pct": ("_w_int_revenue_uplift_pct", 100.0),
    "int_capture_multiple": ("_w_int_capture_multiple", 1.0),
    "int_buyer_compliance": ("_w_int_buyer_compliance", None),
    "int_buyer_tech": ("_w_int_buyer_tech", None),
    "int_buyer_backoffice": ("_w_int_buyer_backoffice", None),
    "int_buyer_planning": ("_w_int_buyer_planning", None),
}

# Decode hard limits — a hostile ?d= param must never eat the container.
_MAX_PARAM_CHARS = 20_000
_MAX_DECOMPRESSED_BYTES = 512 * 1024


# ==============================================================================
# Capture / restore
# ==============================================================================
def _to_number(raw) -> float:
    """Parse ints/floats/comma-or-$-formatted strings. Raises on junk.

    Non-finite values raise rather than propagate. Rejecting the JSON literals
    Infinity/NaN at decode was not enough: the JSON *string* "nan" is an
    ordinary string, float() happily parses it, and _clamp_num cannot filter it
    because every comparison against NaN is False. It sailed through all eight
    float keys and hard-crashed the pro-forma engine on attrition_rate — a
    traceback in the face of whoever opened a link they were told to trust.
    This is the chokepoint every numeric key already passes through, so it is
    the right place to say no once.
    """
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, (int, float)):
        v = float(raw)  # OverflowError on huge ints -- caller catches it
    elif isinstance(raw, str):
        v = float(raw.replace(",", "").replace("$", "").strip())
    else:
        raise TypeError(f"not a number: {type(raw).__name__}")
    if not math.isfinite(v):
        raise ValueError(f"non-finite number: {raw!r}")
    return v


def _clamp_num(value, key):
    lo, hi = _NUMERIC_RANGES.get(key, (None, None))
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


def capture_deal_state(session=None) -> dict:
    """Snapshot the current deal from session state.

    Currency/count comma strings are normalized to ints (compact JSON,
    canonical form); absent keys are skipped so a partially-initialized
    session (e.g. Integration tab not yet rendered) still captures cleanly.
    `session` is injectable for tests; defaults to st.session_state.
    """
    if session is None:
        if st is None:
            return {}
        session = st.session_state
    out = {}
    for key, tag in DEAL_STATE_KEYS.items():
        if key not in session:
            continue
        raw = session[key]
        try:
            if tag in ("currency", "count"):
                out[key] = int(_to_number(raw))
            elif tag == "float":
                out[key] = round(float(_to_number(raw)), 6)
            elif tag == "int":
                out[key] = int(round(_to_number(raw)))
            elif tag == "bool":
                out[key] = bool(raw)
            elif tag == "str":
                out[key] = str(raw)
        except (TypeError, ValueError):
            continue  # one weird value must not sink the whole capture
    return out


def restore_deal_state(state: dict, queue_fn) -> int:
    """Queue every recognized key in `state` for the next rerun.

    `queue_fn` is app.py's `_queue_apply` (passed in to avoid a circular
    import) — the ONLY safe way to write widget values: staged before the
    widgets render, flushed by `_apply_pending()` on the next run.

    Coercion is driven by the manifest tag; numerics are clamped to widget
    bounds, str values validated against _STR_OPTIONS, unknown keys ignored,
    and each key wrapped in its own try/except so one bad value degrades to
    a skipped field instead of a dead app. Integration-tab canonical keys
    also queue their _w_int_* widget shadows (with ×100 scaling for the two
    percent sliders). Returns the number of CANONICAL keys queued (mirror
    writes aren't counted).
    """
    if not isinstance(state, dict):
        return 0

    # loan_amort_yrs has a dynamic floor of loan_term+1 in app.py — restore
    # both consistently or the number_input raises on render.
    restored_loan_term = None
    if "loan_term" in state:
        try:
            restored_loan_term = _clamp_num(
                int(round(_to_number(state["loan_term"]))), "loan_term"
            )
        # ArithmeticError too: float(10**4000) raises OverflowError before the
        # non-finite guard in _to_number can run, and this pre-pass sits
        # outside the main loop's handler — so a 4000-digit int in loan_term
        # escaped every other layer and took the restore down with it.
        except (TypeError, ValueError, ArithmeticError):
            restored_loan_term = None

    applied = 0
    for key, tag in DEAL_STATE_KEYS.items():
        if key not in state:
            continue
        raw = state[key]
        try:
            if tag in ("currency", "count"):
                value = f"{int(_to_number(raw)):,}"
            elif tag == "float":
                value = float(_clamp_num(float(_to_number(raw)), key))
            elif tag == "int":
                value = int(_clamp_num(int(round(_to_number(raw))), key))
                if key == "loan_amort_yrs" and restored_loan_term is not None:
                    value = max(value, restored_loan_term + 1)
            elif tag == "bool":
                value = bool(raw)
            elif tag == "str":
                value = str(raw)
                if value not in _STR_OPTIONS.get(key, ()):
                    continue
            else:  # pragma: no cover — unreachable with a well-formed manifest
                continue
        # ArithmeticError catches OverflowError: int(float("inf")) raises it,
        # and a share link is untrusted input. One bad key skips that key
        # rather than taking down the whole restore.
        except (TypeError, ValueError, ArithmeticError):
            continue

        queue_fn(key, value)
        applied += 1

        if key in _INTEGRATION_MIRRORS:
            w_key, scale = _INTEGRATION_MIRRORS[key]
            if scale is None:  # checkbox mirror — bool passthrough
                queue_fn(w_key, value)
            elif tag == "int":
                queue_fn(w_key, value)
            else:  # float slider mirror, possibly percent-space
                queue_fn(w_key, round(float(value) * scale, 6))
    return applied


# ==============================================================================
# Share-link codec: json -> zlib(9) -> base64url (unpadded)
# ==============================================================================
def encode_deal_to_param(state: dict) -> str:
    """Encode a captured deal state into a URL-safe query-param value."""
    envelope = {"v": SCHEMA_VERSION, "s": state}
    raw = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


def _reject_non_finite(literal):
    """json.loads parse_constant hook — refuse Infinity/-Infinity/NaN."""
    raise ValueError(f"non-finite JSON literal in payload: {literal}")


def decode_deal_from_param(param) -> "dict | None":
    """Reverse of encode_deal_to_param. Never raises — malformed, truncated,
    oversized, or wrong-version params all return None (share links arrive
    from the open internet; treat them as hostile input)."""
    if not isinstance(param, str):
        return None
    param = param.strip()
    if not param or len(param) > _MAX_PARAM_CHARS:
        return None
    try:
        packed = base64.urlsafe_b64decode((param + "=" * (-len(param) % 4)).encode("ascii"))
        decomp = zlib.decompressobj()
        raw = decomp.decompress(packed, _MAX_DECOMPRESSED_BYTES)
        if decomp.unconsumed_tail:  # payload larger than our cap — refuse
            return None
        # parse_constant rejects the JSON literals Infinity/-Infinity/NaN.
        # Python's json accepts them by default, and int(float("inf")) raises
        # OverflowError deep inside restore — a hand-crafted ?d= link could
        # crash the app for whoever opened it. Refuse the payload instead.
        envelope = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_non_finite,
        )
    except Exception:
        return None
    if not isinstance(envelope, dict) or envelope.get("v") != SCHEMA_VERSION:
        return None
    deal_state = envelope.get("s")
    return deal_state if isinstance(deal_state, dict) else None


def get_share_url(state: dict) -> str:
    """Full shareable URL for a captured deal state."""
    return f"{APP_BASE_URL}/?d={encode_deal_to_param(state)}"


# ==============================================================================
# Storage adapters
# ==============================================================================
def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DealStore(ABC):
    """Email-scoped CRUD for saved deals. `state` is a capture_deal_state()
    dict. `list` rows carry id/name/created_at/updated_at, newest first."""

    @abstractmethod
    def save(self, email: str, name: str, state: dict) -> str:
        """Persist a new deal; returns its id."""

    @abstractmethod
    def list(self, email: str) -> "list[dict]":
        """All deals for this email (metadata only), newest first."""

    @abstractmethod
    def load(self, deal_id: str, email: str) -> "dict | None":
        """The stored state dict, or None if the id isn't this email's."""

    @abstractmethod
    def delete(self, deal_id: str, email: str) -> None:
        """Remove a deal (no-op if the id isn't this email's)."""

    @abstractmethod
    def rename(self, deal_id: str, email: str, name: str) -> None:
        """Rename a deal (no-op if the id isn't this email's)."""


class SQLiteStore(DealStore):
    """Local-dev / test store. NOTE: Streamlit Cloud's filesystem is
    EPHEMERAL — data/deals.db (gitignored) vanishes on container restart,
    so this adapter is for local development and tests only. Production
    uses SupabaseStore. Per-operation connections keep it safe across
    Streamlit's threaded reruns."""

    def __init__(self, db_path=None):
        self._path = Path(db_path) if db_path is not None else _SQLITE_DEFAULT_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path)) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deals (
                    id         TEXT PRIMARY KEY,
                    email      TEXT NOT NULL,
                    name       TEXT NOT NULL,
                    state      TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS deals_email_idx ON deals (email)")

    def save(self, email, name, state):
        deal_id = uuid.uuid4().hex
        now = _utc_now_iso()
        with closing(sqlite3.connect(self._path)) as conn, conn:
            conn.execute(
                "INSERT INTO deals (id, email, name, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (deal_id, _norm_email(email), name, json.dumps(state), now, now),
            )
        return deal_id

    def list(self, email):
        with closing(sqlite3.connect(self._path)) as conn:
            rows = conn.execute(
                "SELECT id, name, created_at, updated_at FROM deals "
                "WHERE email = ? ORDER BY updated_at DESC, created_at DESC",
                (_norm_email(email),),
            ).fetchall()
        return [
            {"id": r[0], "name": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]

    def load(self, deal_id, email):
        with closing(sqlite3.connect(self._path)) as conn:
            row = conn.execute(
                "SELECT state FROM deals WHERE id = ? AND email = ?",
                (deal_id, _norm_email(email)),
            ).fetchone()
        if row is None:
            return None
        try:
            state = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return state if isinstance(state, dict) else None

    def delete(self, deal_id, email):
        with closing(sqlite3.connect(self._path)) as conn, conn:
            conn.execute(
                "DELETE FROM deals WHERE id = ? AND email = ?",
                (deal_id, _norm_email(email)),
            )

    def rename(self, deal_id, email, name):
        with closing(sqlite3.connect(self._path)) as conn, conn:
            conn.execute(
                "UPDATE deals SET name = ?, updated_at = ? WHERE id = ? AND email = ?",
                (name, _utc_now_iso(), deal_id, _norm_email(email)),
            )


# Paste into the Supabase SQL editor when creating the project (see the
# integration notes for the full setup checklist).
#
# Security model: the app authenticates to Supabase with the SERVICE-ROLE key
# kept server-side in st.secrets (never shipped to the browser) and scopes
# every query by the OTP-verified email at the application layer. RLS is
# enabled with NO policies as defense-in-depth: if the project's anon key
# ever leaks into client-side code, the table is still completely closed to
# it. (True per-user RLS on an email claim would require forwarding each
# user's Supabase-auth JWT per request — deliberately out of scope for v1.)
SUPABASE_DDL = """
create table if not exists public.deals (
    id         text primary key,
    email      text not null,
    name       text not null,
    state      jsonb not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists deals_email_idx on public.deals (email);

alter table public.deals enable row level security;
-- No RLS policies on purpose: the anon key can touch nothing. The app uses
-- the service-role key (server-side secret), which bypasses RLS, and scopes
-- all queries by email in application code.
"""


class SupabaseStore(DealStore):
    """Production store. The `supabase` package is imported lazily so the
    app still runs (share links, everything else) when it isn't installed."""

    def __init__(self, url: str, key: str):
        from supabase import create_client  # lazy — optional dependency

        self._client = create_client(url, key)

    def save(self, email, name, state):
        deal_id = uuid.uuid4().hex
        now = _utc_now_iso()
        self._client.table("deals").insert(
            {
                "id": deal_id,
                "email": _norm_email(email),
                "name": name,
                "state": state,
                "created_at": now,
                "updated_at": now,
            }
        ).execute()
        return deal_id

    def list(self, email):
        res = (
            self._client.table("deals")
            .select("id, name, created_at, updated_at")
            .eq("email", _norm_email(email))
            .order("updated_at", desc=True)
            .execute()
        )
        return list(res.data or [])

    def load(self, deal_id, email):
        res = (
            self._client.table("deals")
            .select("state")
            .eq("id", deal_id)
            .eq("email", _norm_email(email))
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        state = rows[0].get("state")
        if isinstance(state, str):  # defensive — jsonb normally arrives as dict
            try:
                state = json.loads(state)
            except (TypeError, ValueError):
                return None
        return state if isinstance(state, dict) else None

    def delete(self, deal_id, email):
        (
            self._client.table("deals")
            .delete()
            .eq("id", deal_id)
            .eq("email", _norm_email(email))
            .execute()
        )

    def rename(self, deal_id, email, name):
        (
            self._client.table("deals")
            .update({"name": name, "updated_at": _utc_now_iso()})
            .eq("id", deal_id)
            .eq("email", _norm_email(email))
            .execute()
        )


def _supabase_creds() -> "tuple[str, str] | None":
    """Mirror app.py's graceful st.secrets access — secrets may be entirely
    absent in local dev and that must never raise."""
    try:
        if st is not None and "SUPABASE_URL" in st.secrets and "SUPABASE_KEY" in st.secrets:
            url = str(st.secrets["SUPABASE_URL"]).strip()
            key = str(st.secrets["SUPABASE_KEY"]).strip()
            if url and key:
                return url, key
    except Exception:
        # No secrets file at all is the normal local/unconfigured case, and
        # st.secrets raises rather than returning empty. Debug-level: this is
        # a configuration state, not an incident — logging a traceback here on
        # every render would bury the failures that actually matter.
        _log.debug("Supabase secrets unavailable", exc_info=True)
    return None


def _on_streamlit_cloud() -> bool:
    """Streamlit Community Cloud checks the repo out under /mount/src."""
    return Path("/mount/src").exists()


def _build_store() -> "DealStore | None":
    """Supabase when secrets exist; SQLite for local dev; None on Cloud
    without secrets (saving silently unavailable — better than letting users
    save into an ephemeral container filesystem)."""
    creds = _supabase_creds()
    if creds is not None:
        try:
            return SupabaseStore(*creds)
        except Exception:
            _log.exception("SupabaseStore init failed; saving disabled")
            # Bad secrets or missing package — degrade to "no saving" rather
            # than crash the whole app at import/render time.
            return None
    if not _on_streamlit_cloud():
        return SQLiteStore()
    return None


if st is not None:
    get_store = st.cache_resource(show_spinner=False)(_build_store)
else:  # headless/test context — uncached is fine
    get_store = _build_store


# ==============================================================================
# UI — sidebar save/share controls (session keys are all _persist_-prefixed)
# ==============================================================================
def _invalidate_deals_cache():
    st.session_state.pop("_persist_deals_cache", None)
    st.session_state.pop("_persist_deals_cache_email", None)


def _cached_deals(store: DealStore, email: str) -> "list[dict]":
    """Session-cached deal list so every slider nudge doesn't refire a
    Supabase HTTP call. Invalidated on save/delete and on email change."""
    if (
        "_persist_deals_cache" not in st.session_state
        or st.session_state.get("_persist_deals_cache_email") != email
    ):
        st.session_state["_persist_deals_cache"] = store.list(email)
        st.session_state["_persist_deals_cache_email"] = email
    return st.session_state["_persist_deals_cache"]


def render_save_controls(queue_fn):
    """Sidebar block: share link (always available) + save-this-deal
    (only when auth is configured AND the user is signed in)."""
    if st is None:
        return
    import auth  # lazy — keeps this module importable headlessly

    st.sidebar.markdown("**Share this deal**")
    st.sidebar.code(get_share_url(capture_deal_state()), language=None)
    st.sidebar.caption(
        "Anyone with this link opens this exact deal — no sign-in needed. "
        "The link carries every input you entered, so treat it like the deal "
        "file itself."
    )

    store = get_store()
    if store is None or not auth.auth_available():
        # No auth configured (or Cloud without Supabase) — hide saving
        # entirely instead of dangling a sign-in hint at a hidden widget.
        return
    email = auth.get_verified_email()
    if email is None:
        st.sidebar.caption("Sign in above to save deals.")
        return

    # The placeholder used to read "e.g., Acme Wealth — 2x rev", which invited
    # users to type a target's name into a field that gets stored and shared —
    # defeating the manifest's deliberate exclusion of firm identity.
    name = st.sidebar.text_input(
        "Deal name",
        key="_persist_deal_name",
        placeholder="e.g., Midwest tuck-in — 2x rev",
        help="Only you see this. Avoid the target's name if the deal is "
             "confidential — saved names travel with shared links.",
    )
    if st.sidebar.button("Save this deal", key="_persist_save_btn", use_container_width=True):
        deal_name = (name or "").strip()[:80] or f"Untitled deal — {_utc_now_iso()[:10]}"
        try:
            store.save(email, deal_name, capture_deal_state())
        except Exception:
            _log.exception("deal save failed")
            st.sidebar.error("Couldn't save right now — try again in a moment.")
        else:
            _invalidate_deals_cache()
            queue_fn("_persist_deal_name", "")  # clear the name box next run
            st.toast(f"Saved “{deal_name}”", icon="✅")
            st.rerun()


def render_my_deals(queue_fn):
    """Sidebar list of the signed-in user's saved deals with
    Load / Share / Delete per row."""
    if st is None:
        return
    import auth  # lazy

    store = get_store()
    if store is None or not auth.auth_available():
        return
    email = auth.get_verified_email()
    if email is None:
        return

    st.sidebar.markdown("**My deals**")
    try:
        deals = _cached_deals(store, email)
    except Exception:
        _log.exception("listing saved deals failed")
        st.sidebar.caption("Couldn't reach saved deals — try again in a moment.")
        return
    if not deals:
        st.sidebar.caption("No saved deals yet.")
        return

    for row in deals:
        deal_id = row.get("id")
        name = str(row.get("name") or "Untitled")
        updated = str(row.get("updated_at") or "")[:10]
        st.sidebar.markdown(
            f"<div style='font-size:0.85rem; color:#e5e9f0; margin-top:6px;'>"
            f"{_html.escape(name)}"
            f" <span style='color:#6b7385; font-size:0.72rem;'>· {updated}</span></div>",
            unsafe_allow_html=True,
        )
        c1, c2, c3 = st.sidebar.columns(3)
        if c1.button("Load", key=f"_persist_load_{deal_id}", use_container_width=True):
            try:
                state = store.load(deal_id, email)
            except Exception:
                _log.exception("deal load failed")
                state = None
            if state:
                applied = restore_deal_state(state, queue_fn)
                st.toast(f"Loaded “{name}” — {applied} inputs applied.", icon="✅")
                st.rerun()
            else:
                st.sidebar.error("Couldn't load that deal.")
        if c2.button("Share", key=f"_persist_share_{deal_id}", use_container_width=True):
            # Toggle the inline link open/closed for just this deal.
            st.session_state["_persist_share_open"] = (
                None if st.session_state.get("_persist_share_open") == deal_id else deal_id
            )
        if st.session_state.get("_persist_share_open") == deal_id:
            try:
                state = store.load(deal_id, email)
            except Exception:
                _log.exception("deal load failed")
                state = None
            if state:
                st.sidebar.code(get_share_url(state), language=None)
        if c3.button("Delete", key=f"_persist_del_{deal_id}", use_container_width=True):
            try:
                store.delete(deal_id, email)
            except Exception:
                _log.exception("deal delete failed")
                st.sidebar.error("Couldn't delete that deal.")
            else:
                _invalidate_deals_cache()
                st.toast("Deleted.", icon="✅")
                st.rerun()


# ==============================================================================
# URL entry point — must run BEFORE the welcome gate and _apply_pending()
# ==============================================================================
def apply_deal_from_url(queue_fn) -> bool:
    """Decode a `?d=` share param (once per session) and queue a full restore.

    Call from app.py AFTER `_queue_apply`/`_apply_pending` are defined but
    BEFORE the welcome-page gate — the link must land on the calculator, so
    this also flips show_calculator (a plain, non-widget session key). The
    queued values are flushed by the existing `_apply_pending()` call at the
    top of the sidebar on the rerun this function triggers.

    Returns True when a restore was queued (the st.rerun() means callers
    normally never observe it); False otherwise.
    """
    if st is None:
        return False

    # One-shot toast on the run AFTER a successful restore (a toast fired on
    # the same run as st.rerun() gets wiped before the user sees it).
    applied_n = st.session_state.pop("_persist_url_toast", None)
    if applied_n:
        st.toast(f"Loaded shared deal — {applied_n} inputs applied.", icon="✅")

    if st.session_state.get("_persist_url_applied"):
        return False
    try:
        param = st.query_params.get("d")
    except Exception:
        return False
    if not param:
        return False

    # Mark handled FIRST so a malformed param can't loop us forever.
    st.session_state["_persist_url_applied"] = True
    state = decode_deal_from_param(param)
    try:
        if "d" in st.query_params:
            del st.query_params["d"]
    except Exception:
        pass
    if state is None:
        st.toast(
            "That share link looks damaged — opening a blank calculator instead.",
            icon="⚠️",
        )
        return False

    # Belt and braces: decode already rejects non-finite payloads and restore
    # skips bad keys, but this call sits at app.py top level — anything that
    # escapes here is a stack trace in the recipient's face on a link they were
    # told to trust. Degrade to a blank calculator instead.
    try:
        applied = restore_deal_state(state, queue_fn)
    except Exception:
        _log.exception("share-link restore failed")
        st.toast(
            "That share link looks damaged — opening a blank calculator instead.",
            icon="⚠️",
        )
        return False
    st.session_state["show_calculator"] = True  # skip the welcome page
    st.session_state["_persist_url_toast"] = applied
    st.rerun()
    return True  # pragma: no cover — st.rerun() raises before we get here
