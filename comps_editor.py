"""User-editable comparable-transactions database (comps editor).

The bundled CSV at data/ria_ma_comps.csv ships with a small set of
confirmed deals — publicly disclosed or arithmetically derivable terms,
documented row by row in data/SOURCES.md — plus a larger set of clearly
flagged estimates. This module gives the app one working copy of that
table which the user can extend, correct, or prune during a session:

    get_active_comps_df()  -> the frame app.py passes to lookup_comps_band
    render_comps_editor()  -> the editor UI, mounted inside the existing
                              "Browse transaction database" expander

Session-state keys (all prefixed _comps_):
    _comps_custom_df          the user's working copy (set after any edit)
    _comps_editor_state       st.data_editor widget state (its delta log)
    _comps_include_estimated  the "include estimates in benchmark" toggle
    _comps_reset              the reset button

Importable without Streamlit side effects: nothing here touches the
Streamlit runtime at import time, so `import comps_editor` is safe from
tests and scripts. Edits live in session state only — the CSV on disk is
never written.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

_CSV_PATH = Path(__file__).parent / "data" / "ria_ma_comps.csv"

_COLUMNS = [
    "date", "buyer", "seller", "seller_aum",
    "ev_revenue_multiple", "ev_ebitda_multiple",
    "aum_tier", "recurring_tier", "channel",
    "is_estimated", "source_url", "notes",
]

# Keep in sync with _aum_tier_for / _recurring_tier_for in calculations.py.
_AUM_TIERS = ["<200M", "200M-500M", "500M-1B", "1B-5B", "5B+"]
_RECURRING_TIERS = ["<70%", "70-90%", "90%+"]
_NUMERIC_COLS = ("seller_aum", "ev_revenue_multiple", "ev_ebitda_multiple")

_EDITOR_KEY = "_comps_editor_state"


def _flag_is_estimated(v) -> bool:
    """Same semantics as lookup_comps_band: strings other than 'false'
    are estimated, NaN (unknown provenance) is estimated, bools pass
    through. Only an explicit False marks a row confirmed."""
    if isinstance(v, str):
        return v.strip().lower() != "false"
    if pd.isna(v):
        return True
    return bool(v)


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dtypes so the editor, the charts, and lookup_comps_band
    all see the same shapes no matter where a row came from (bundled CSV,
    an edited cell, or a freshly added row)."""
    df = df.reindex(columns=_COLUMNS)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in _NUMERIC_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["is_estimated"] = df["is_estimated"].map(_flag_is_estimated).astype(bool)
    return df


@st.cache_data(show_spinner=False)
def _load_base_csv(path_str: str, mtime: float) -> pd.DataFrame:
    """Load the bundled CSV once per (path, mtime). The mtime keys the
    cache, so a refreshed file on disk busts it without a manual clear.
    Missing or unreadable file -> empty frame with the expected columns,
    so the rest of the app keeps rendering."""
    path = Path(path_str)
    if not path.exists():
        return _coerce_types(pd.DataFrame(columns=_COLUMNS))
    try:
        df = pd.read_csv(path)
    except Exception:
        return _coerce_types(pd.DataFrame(columns=_COLUMNS))
    return _coerce_types(df)


def _base_df() -> pd.DataFrame:
    mtime = _CSV_PATH.stat().st_mtime if _CSV_PATH.exists() else 0.0
    return _load_base_csv(str(_CSV_PATH), mtime)


def get_active_comps_df() -> pd.DataFrame:
    """The comps frame the rest of the app should use: the bundled table
    plus any in-session edits. Always returns a copy so callers can't
    mutate the cache or the session copy by accident."""
    custom = st.session_state.get("_comps_custom_df")
    if custom is not None:
        return custom.copy()
    return _base_df().copy()


def _apply_editor_deltas() -> None:
    """on_change callback for the data editor: replay its delta log
    (edited / deleted / added rows) onto the bundled frame and stash the
    result in _comps_custom_df. Callbacks run before the rerun's script
    body, so the benchmark at the top of the page picks up an edit in the
    same rerun that renders it — no one-edit lag."""
    state = st.session_state.get(_EDITOR_KEY)
    if not state:
        st.session_state.pop("_comps_custom_df", None)
        return

    df = _base_df().copy().reset_index(drop=True)

    for idx, changes in state.get("edited_rows", {}).items():
        for col, val in changes.items():
            if col in df.columns:
                df.loc[int(idx), col] = val

    deleted = [int(i) for i in state.get("deleted_rows", [])]
    if deleted:
        df = df.drop(index=[i for i in deleted if i in df.index])

    added = state.get("added_rows", [])
    if added:
        add_df = pd.DataFrame(added)
        # A new row is an estimate until it cites a source. Presuming rows real
        # let an unsourced guess feed the confirmed-only benchmark and print
        # "Confirmed" in an IC memo — the exact provenance failure this whole
        # comps pass exists to end. Supplying a source_url flips it to confirmed.
        if "is_estimated" not in add_df.columns:
            add_df["is_estimated"] = True
        else:
            add_df["is_estimated"] = add_df["is_estimated"].fillna(True)
        if "source_url" in add_df.columns:
            _sourced = add_df["source_url"].fillna("").astype(str).str.strip() != ""
            add_df.loc[_sourced & add_df["is_estimated"].isna(), "is_estimated"] = False
        add_df = add_df.reindex(columns=_COLUMNS)
        df = pd.concat([df, add_df], ignore_index=True)

    st.session_state["_comps_custom_df"] = _coerce_types(df.reset_index(drop=True))


def _channel_options(df: pd.DataFrame) -> list:
    """Selectbox options: the channels already in the data plus the
    common labels, so new rows can pick a fresh-but-sane category."""
    common = {
        "aggregator", "pe-platform", "platform",
        "hybrid-bd", "institutional", "cpa-channel", "bank",
    }
    seen = (
        set(df["channel"].dropna().astype(str))
        if "channel" in df.columns else set()
    )
    return sorted(common | seen)


def render_comps_editor() -> None:
    """Mount the comps editor inside app.py's "Browse transaction
    database" expander: the include-estimates toggle, the editable table,
    a confirmed-vs-estimated tally, and a reset button."""
    base = _base_df()

    st.toggle(
        "Include estimated rows in the benchmark",
        value=False,
        key="_comps_include_estimated",
        help=(
            "Off by default. Confirmed rows come from publicly disclosed "
            "deal terms — sources and arithmetic in data/SOURCES.md. "
            "Estimated rows are modeled guesses: useful context, not "
            "evidence. The P25 / median / P75 band above only uses "
            "estimates while this is on."
        ),
    )

    st.data_editor(
        base,
        key=_EDITOR_KEY,
        on_change=_apply_editor_deltas,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        column_config={
            "date": st.column_config.DateColumn(
                "Date", help="Announcement or close date.",
            ),
            "buyer": st.column_config.TextColumn("Buyer"),
            "seller": st.column_config.TextColumn("Seller"),
            "seller_aum": st.column_config.NumberColumn(
                "Seller AUM ($)", min_value=0.0, format="%.0f",
                help="Assets under management at deal time, in dollars.",
            ),
            "ev_revenue_multiple": st.column_config.NumberColumn(
                "EV / Revenue", min_value=0.0, format="%.2fx",
                help="Leave blank when revenue wasn't disclosed.",
            ),
            "ev_ebitda_multiple": st.column_config.NumberColumn(
                "EV / EBITDA", min_value=0.0, format="%.1fx",
                help="Leave blank when EBITDA wasn't disclosed.",
            ),
            "aum_tier": st.column_config.SelectboxColumn(
                "AUM tier", options=_AUM_TIERS,
                help="Which size bucket the benchmark slices on.",
            ),
            "recurring_tier": st.column_config.SelectboxColumn(
                "Recurring tier", options=_RECURRING_TIERS,
                help="Share of revenue that recurs (advisory fees).",
            ),
            "channel": st.column_config.SelectboxColumn(
                "Channel", options=_channel_options(base),
            ),
            # default=True: a row you just typed is an estimate until you say
            # otherwise. default=False meant five typed guesses rendered as an
            # "N=5 confirmed" band beneath a legend certifying every row was
            # "stated in, or derived from, a cited primary source" — a
            # fabricated comp presented as a disclosed one, in a document
            # built to be forwarded.
            "is_estimated": st.column_config.CheckboxColumn(
                "Estimated?", default=True,
                help=(
                    "On by default: a new row is an estimate until you cite a "
                    "source for the multiple. Untick only when the figure is "
                    "disclosed or reliably derived, and paste the source. "
                    "Estimated rows sit out of the benchmark unless the "
                    "toggle above is on, and never feed the Deal Memo."
                ),
            ),
            "source_url": st.column_config.LinkColumn(
                "Source", help="Link to the disclosure backing the numbers.",
            ),
            "notes": st.column_config.TextColumn("Notes", width="large"),
        },
    )

    active = get_active_comps_df()
    if len(active) > 0:
        n_est = int(active["is_estimated"].map(_flag_is_estimated).sum())
    else:
        n_est = 0
    n_conf = len(active) - n_est
    st.caption(
        f"Working set: {n_conf} deal{'s' if n_conf != 1 else ''} with "
        f"disclosed terms and {n_est} flagged estimate"
        f"{'s' if n_est != 1 else ''}. Rows you add or change live in this "
        "browser session only — the bundled file never changes."
    )

    if st.button(
        "Reset to bundled data",
        key="_comps_reset",
        help="Throw away every in-session edit and reload the shipped table.",
    ):
        st.session_state.pop("_comps_custom_df", None)
        st.session_state.pop(_EDITOR_KEY, None)
        st.rerun()
