"""
Deal Memo Mode -- on-screen 5-page memo for the RIA M&A Calculator.

Renders the memo as five stacked `.memo-page` cards inside a Streamlit tab
and wires a PDF download to `pdf_export.generate_memo_pdf`. All insight
severities, texts, and the verdict come from `insights.py` (single source
of truth); this module never re-derives them. What it does own:

- `MEMO_CSS`         -- the `.memo-*` stylesheet block (spec appendix),
                        pasted into `_THEME_CSS` at integration time.
- `build_memo_data`  -- one format-agnostic digest of everything the memo
                        shows, consumed by both the HTML renderer here and
                        the PDF renderer in `pdf_export.generate_memo_pdf`.
- `render_memo_tab`  -- the Streamlit entry point.

No Streamlit import at module level: `pdf_export` imports the digest
builder from here, and keeping this module UI-free keeps that clean.
"""

import html as _html
import math
import re
from dataclasses import replace
from datetime import date

try:
    from insights import (Insight, compute_all_insights, select_insight,
                          verdict_from_insights, render_insight,
                          render_verdict_bar)
    _HAS_INSIGHTS = True
except ImportError:  # standalone import only -- insights.py ships alongside
    Insight = None
    compute_all_insights = None
    select_insight = None
    verdict_from_insights = None
    render_insight = None
    render_verdict_bar = None
    _HAS_INSIGHTS = False


# ==============================================================================
# CSS -- spec appendix, memo block verbatim. Injected ONCE into _THEME_CSS.
# ==============================================================================
MEMO_CSS = """
/* ============ MEMO ============ */
.memo-page{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  max-width:760px;margin:0 auto 24px auto;background:#12151c;
  border:1px solid #1c2230;border-radius:10px;padding:40px 48px;color:#c5cad6;
}
.memo-pagenum{
  float:right;font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:0.72rem;color:#6b7385;font-feature-settings:"tnum","lnum";
}
.memo-kicker{
  font-size:0.72rem;font-weight:600;letter-spacing:0.10em;text-transform:uppercase;
  color:#8b94a8;margin:0 0 6px 0;
}
.memo-title{font-size:1.5rem;font-weight:600;color:#ffffff;margin:0 0 4px 0;letter-spacing:-0.01em;line-height:1.25;}
.memo-meta{
  font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:0.78rem;color:#8b94a8;margin:0 0 20px 0;font-feature-settings:"tnum","lnum";
}
.memo-pagehead{
  display:flex;justify-content:space-between;align-items:baseline;
  border-bottom:1px solid #1c2230;padding-bottom:10px;margin-bottom:22px;
  font-size:0.78rem;color:#8b94a8;
}
.memo-rule{border:none;border-top:1px solid #1c2230;margin:22px 0;}
.memo-h{
  font-size:0.78rem;font-weight:600;text-transform:uppercase;letter-spacing:0.10em;
  color:#e5e9f0;margin:26px 0 14px 0;display:flex;align-items:center;gap:10px;
}
.memo-h::before{content:"";width:3px;height:14px;background:#7c8cff;border-radius:2px;}
.memo-para{font-size:0.92rem;line-height:1.65;color:#c5cad6;margin:14px 0;max-width:62ch;}
.memo-caption{font-size:0.78rem;line-height:1.55;color:#8b94a8;margin:8px 0 0 0;}
.memo-disclaimer{font-size:0.72rem;line-height:1.5;color:#6b7385;margin:18px 0 0 0;
  padding:10px 12px;border:1px solid #1c2230;border-radius:6px;}
.memo-verdict{
  display:flex;align-items:center;gap:12px;background:#0e1118;
  border:1px solid #1c2230;border-left:3px solid #6b7385;border-radius:6px;
  padding:14px 18px;margin:18px 0;
}
.memo-verdict--go{border-left-color:#4ade80;}
.memo-verdict--caution{border-left-color:#fbbf24;}
.memo-verdict--reconsider{border-left-color:#f87171;}
.memo-verdict--incomplete{border-left-color:#6b7385;}
.memo-verdict-dot{width:10px;height:10px;border-radius:50%;flex:0 0 10px;background:#6b7385;}
.memo-verdict--go .memo-verdict-dot{background:#4ade80;}
.memo-verdict--caution .memo-verdict-dot{background:#fbbf24;}
.memo-verdict--reconsider .memo-verdict-dot{background:#f87171;}
.memo-verdict--incomplete .memo-verdict-dot{background:#6b7385;}
.memo-verdict-word{font-size:1.05rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#ffffff;}
.memo-verdict--go .memo-verdict-word{color:#4ade80;}
.memo-verdict--caution .memo-verdict-word{color:#fbbf24;}
.memo-verdict--reconsider .memo-verdict-word{color:#f87171;}
.memo-verdict--incomplete .memo-verdict-word{color:#8b94a8;}
.memo-verdict-counts{
  margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:0.74rem;color:#8b94a8;font-feature-settings:"tnum","lnum";
}
.memo-stat-strip{display:flex;gap:12px;margin:14px 0;flex-wrap:wrap;}
.memo-stat{
  flex:1 1 150px;background:#0e1118;border:1px solid #1c2230;border-radius:8px;padding:12px 14px;
}
.memo-stat h4{
  font-size:0.66rem;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;
  color:#8b94a8;margin:0 0 6px 0;white-space:nowrap;
}
.memo-stat .memo-stat-val{
  font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:1.2rem;font-weight:500;color:#ffffff;
  font-feature-settings:"tnum","lnum","zero";font-variant-numeric:tabular-nums lining-nums;
  display:flex;align-items:center;gap:8px;
}
.memo-stat-dot{width:6px;height:6px;border-radius:50%;flex:0 0 6px;background:#6b7385;}
.memo-stat-dot--positive{background:#4ade80;}
.memo-stat-dot--caution{background:#fbbf24;}
.memo-stat-dot--negative{background:#f87171;}
.memo-band{position:relative;height:48px;margin:30px 0 4px 0;}
.memo-band-track{position:absolute;top:22px;left:0;right:0;height:4px;background:#1c2230;border-radius:2px;}
.memo-band-seg{position:absolute;top:18px;height:12px;background:#2f3646;border-radius:3px;}
.memo-band-tick{position:absolute;top:14px;width:1px;height:20px;background:#8b94a8;}
.memo-band-marker{position:absolute;top:12px;width:3px;height:24px;border-radius:1.5px;transform:translateX(-1.5px);background:#6b7385;}
.memo-band-marker--positive{background:#4ade80;}
.memo-band-marker--caution{background:#fbbf24;}
.memo-band-marker--negative{background:#f87171;}
.memo-band-bidlabel{
  position:absolute;top:-22px;transform:translateX(-50%);white-space:nowrap;
  font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:0.72rem;font-feature-settings:"tnum","lnum";
}
.memo-band-bidlabel--positive{color:#4ade80;}
.memo-band-bidlabel--caution{color:#fbbf24;}
.memo-band-bidlabel--negative{color:#f87171;}
.memo-band-axis{
  position:absolute;top:40px;transform:translateX(-50%);white-space:nowrap;
  font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:0.72rem;color:#8b94a8;font-feature-settings:"tnum","lnum";
}
.memo-structure{margin:14px 0;}
.memo-structure-row{display:flex;align-items:center;gap:12px;margin:8px 0;}
.memo-structure-label{
  flex:0 0 88px;font-size:0.72rem;font-weight:600;text-transform:uppercase;
  letter-spacing:0.06em;color:#8b94a8;
}
.memo-structure-bar{flex:1;display:flex;gap:2px;height:28px;border-radius:4px;overflow:hidden;}
.memo-structure-seg{
  display:flex;align-items:center;justify-content:center;min-width:2%;
  font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:0.72rem;font-feature-settings:"tnum","lnum";
}
.memo-structure-seg--upfront{background:#7c8cff;color:#0b0d12;}
.memo-structure-seg--note{background:#8b94a8;color:#0b0d12;}
.memo-structure-seg--earnout{background:#6b7385;color:#e5e9f0;}
.memo-structure-seg--rollover{background:#2f3646;color:#e5e9f0;}
.memo-legend{display:flex;gap:18px;margin:8px 0 0 100px;font-size:0.72rem;color:#8b94a8;flex-wrap:wrap;}
.memo-legend span{display:inline-flex;align-items:center;gap:6px;}
.memo-legend i{width:8px;height:8px;border-radius:2px;display:inline-block;}
.memo-table{width:100%;border-collapse:collapse;margin:14px 0;}
.memo-table th{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  font-size:0.7rem;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;
  color:#8b94a8;text-align:right;padding:0 10px 8px 10px;border-bottom:1px solid #1c2230;
}
.memo-table th:first-child,.memo-table td:first-child{text-align:left;padding-left:0;}
.memo-table td{
  font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:0.86rem;color:#e5e9f0;text-align:right;padding:9px 10px;
  border-bottom:1px solid #1c2230;
  font-feature-settings:"tnum","lnum","zero";font-variant-numeric:tabular-nums lining-nums;
}
.memo-table td.memo-td-text{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  font-size:0.82rem;color:#c5cad6;text-align:left;
}
.memo-table td.memo-td-neg{color:#f87171;}
.memo-table td.memo-td-est{color:#fbbf24;font-size:0.72rem;}
.memo-table td.memo-td-conf{color:#8b94a8;font-size:0.72rem;}
.memo-bars{position:relative;display:flex;align-items:flex-end;gap:12px;height:96px;margin:34px 0 6px 0;}
.memo-bar{width:56px;border-radius:2px;background:#7c8cff;position:relative;}
.memo-bar--neg{background:#f87171;align-self:flex-start;}
.memo-bar-label{
  position:absolute;left:50%;transform:translateX(-50%);top:-18px;white-space:nowrap;
  font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-size:0.72rem;color:#8b94a8;font-feature-settings:"tnum","lnum";
}
.memo-bar--neg .memo-bar-label{top:auto;bottom:-18px;}
.memo-bars-axis{border:none;border-top:1px solid #1c2230;margin:0;}
.memo-bars-years{display:flex;gap:12px;margin-top:6px;}
.memo-bars-years span{
  width:56px;text-align:center;font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:0.72rem;color:#8b94a8;
}
.memo-bullets{list-style:none;margin:10px 0;padding:0;}
.memo-bullets li{
  position:relative;padding:6px 0 6px 18px;font-size:0.88rem;line-height:1.55;color:#c5cad6;
}
.memo-bullets li::before{
  content:"";position:absolute;left:2px;top:14px;width:6px;height:6px;
  border-radius:50%;background:#8b94a8;
}
.memo-empty{
  text-align:center;border:1px solid #1c2230;border-radius:8px;
  background:#0e1118;padding:28px;margin:16px 0;
}
.memo-empty-title{font-size:0.95rem;font-weight:500;color:#e5e9f0;margin:0 0 6px 0;}
.memo-empty-sub{font-size:0.82rem;color:#8b94a8;line-height:1.55;margin:0;max-width:48ch;display:inline-block;}
.memo-footer{
  display:flex;justify-content:space-between;border-top:1px solid #1c2230;
  margin-top:30px;padding-top:14px;font-size:0.72rem;color:#6b7385;
}
@media (max-width:640px){
  .memo-page{padding:24px 20px;}
  .memo-stat-strip{flex-direction:column;}
  .memo-structure-row{flex-wrap:wrap;}
  .memo-legend{margin-left:0;}
  .memo-bars{gap:8px;}
  .memo-bar,.memo-bars-years span{width:40px;}
}
"""


# ==============================================================================
# Formatters (shared by screen and PDF; PDF runs pdf_safe() over the output)
# ==============================================================================
_SEVS = ("positive", "caution", "negative")

DSCR_COVENANT = 1.25

_PROFILE_LABELS = {
    "key_person_concentrated": "Key-person",
    "small_tuck_in": "Tuck-in",
    "platform": "Platform",
    "high_growth": "High growth",
    "mid_market": "Mid-market",
}

_STANDING_BULLETS = [
    "Top-10 client concentration is not modeled — request the client-level AUM schedule.",
    "CCO continuity and compliance staffing post-close — confirm before LOI.",
    "Fee-schedule harmonization with the buyer's standard grid — quantify repricing attrition.",
]


def _is_num(v):
    return isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v))


def fmt_money(v):
    """$500M / $11.7M / $524K / $8.0B -- memo-wide dollar format."""
    if not _is_num(v):
        return "n/a"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a/1e9:,.1f}B"
    if a >= 100e6:
        return f"{sign}${a/1e6:,.0f}M"
    if a >= 1e6:
        return f"{sign}${a/1e6:,.1f}M"
    if a >= 1e3:
        return f"{sign}${a/1e3:,.0f}K"
    return f"{sign}${a:,.0f}"


def fmt_money_table(v):
    """Pro forma table format: $X.XXM under $10M, $X.XM above."""
    if not _is_num(v):
        return "n/a"
    a = abs(v)
    sign = "−" if v < 0 else ""
    if a >= 10e6:
        return f"{sign}${a/1e6:,.1f}M"
    return f"{sign}${a/1e6:,.2f}M"


def fmt_mult(v):
    return f"{v:.1f}×" if _is_num(v) else "n/a"


def fmt_dscr(v):
    return f"{v:.2f}×" if _is_num(v) else "n/a"


def fmt_irr(v):
    return f"{v*100:.1f}%" if _is_num(v) else "n/a"


def _h(text):
    """HTML-escape and neutralize dollars so Streamlit's KaTeX never fires."""
    return _html.escape(str(text), quote=False).replace("$", "&#36;")


# fpdf2's core fonts are latin-1, NOT ascii -- U+00D7 (×) and U+00B7 (·)
# encode natively, so folding them was self-inflicted damage: every multiple in
# the hero PDF printed "8.0x" and every dot separator became a hyphen. Fold only
# what latin-1 genuinely cannot carry (verified against the codec, not assumed).
_LATIN1_MAP = {
    "—": "--", "–": "-", "−": "-", "≈": "~",
    "≤": "<=", "≥": ">=", "→": "->",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...",
}


def pdf_safe(text):
    """Force memo copy down to latin-1, the widest set fpdf2 core fonts print."""
    s = str(text)
    for k, v in _LATIN1_MAP.items():
        s = s.replace(k, v)
    # Anything still outside latin-1 is a copy bug upstream; drop it rather than
    # raise mid-render, but keep every glyph the font can actually set.
    return s.encode("latin-1", "ignore").decode("latin-1")


def _sev(ins):
    """Normalized severity string for an Insight, tolerant of enums."""
    if ins is None:
        return "muted"
    raw = str(getattr(ins, "severity", "") or "").lower()
    raw = raw.rsplit(".", 1)[-1].strip()
    return raw if raw in _SEVS + ("info",) else "muted"


def _plain(ins):
    """Insight text stripped of HTML tags and entities -> plain unicode."""
    if ins is None:
        return ""
    txt = str(getattr(ins, "text", "") or "")
    txt = re.sub(r"<[^>]+>", "", txt)
    return _html.unescape(txt).strip()


def _find_insight(insights, key):
    """Tolerant kind lookup: exact match first, then substring."""
    for ins in insights:
        if str(getattr(ins, "kind", "")).lower() == key:
            return ins
    for ins in insights:
        if key in str(getattr(ins, "kind", "")).lower():
            return ins
    return None


def _band_positions(low, mid, high, bid=None):
    """Spec 2.3 axis math -> percent positions, clamped to [0, 100]."""
    vals = [v for v in (low, mid, high) if _is_num(v)]
    if not vals:
        return None
    lo, hi = min(vals), max(vals)
    if _is_num(bid) and bid > 0:
        lo, hi = min(lo, bid), max(hi, bid)
    axis_min, axis_max = lo * 0.92, hi * 1.08
    span = axis_max - axis_min
    if span <= 0:
        return None

    def pos(x):
        return round(min(100.0, max(0.0, (x - axis_min) / span * 100.0)), 1)

    return {
        "low": pos(low), "mid": pos(mid), "high": pos(high),
        "bid": pos(bid) if (_is_num(bid) and bid > 0) else None,
    }


# ==============================================================================
# Digest builder -- everything both renderers need, derived exactly once
# ==============================================================================
def is_zero_state(ctx):
    inp = ctx.get("inputs") or {}
    total = sum(float(inp.get(k) or 0) for k in ("aum", "revenue", "ebitda"))
    return total <= 0


def _breakeven_parts(raw):
    """Return (display, severity, kind) for the breakeven value.

    kind: 'year' (int), 'over' ('> 7'), or 'na' (no equity invested).
    """
    if raw is None:
        return "n/a", "muted", "na"
    if isinstance(raw, str):
        if raw.strip().startswith(">"):
            return "> 7", "negative", "over"
        return "n/a", "muted", "na"
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return "n/a", "muted", "na"
    if n <= 5:
        return f"Year {n}", "positive", "year"
    if n <= 7:
        return f"Year {n}", "caution", "year"
    return f"Year {n}", "negative", "year"


def _min_dscr(pro_forma):
    """(min_dscr, year) over non-NaN years, or (None, None) with no debt."""
    if pro_forma is None or len(pro_forma) == 0:
        return None, None
    df = pro_forma
    if "dscr" in df.columns:
        series = df["dscr"]
    else:
        total_debt = df["debt_service"] + df["seller_note_payment"]
        series = df["ebitda"] / total_debt.replace(0, float("nan"))
    series = series.dropna()
    if len(series) == 0:
        return None, None
    idx = series.idxmin()
    return float(series.loc[idx]), int(df.loc[idx, "year"])


def _row_is_estimated(row):
    """Provenance flag for one comps row. Mirrors the engine's reading:
    an explicit False/0/NO is confirmed, a blank or unreadable flag is
    unknown provenance and counts as estimated."""
    v = row.get("is_estimated")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().upper()
        if s in ("FALSE", "0", "NO"):
            return False
        if s in ("TRUE", "1", "YES"):
            return True
        return True  # unreadable flag: unknown provenance
    if v is not None:
        try:
            f = float(v)  # numpy bools land here too
        except (TypeError, ValueError):
            return True
        if math.isnan(f):
            return True  # blank flag: unknown provenance, matches engine
        return bool(f)
    notes = row.get("notes")
    return isinstance(notes, str) and notes.strip().upper().startswith("ESTIMATED")


def _comps_digest(ctx, extras, deal_mult, comps_ins):
    """Digest of the comps lookup for page 5.

    Keys off the confirmed-aware `lookup_comps_band` shape: the band is now
    built from confirmed rows only (estimates excluded by default) and the
    result carries n_confirmed / n_estimated_excluded. When those keys are
    present, every row the band used is confirmed, so the slice's confirmed
    count is n_used. Older-shape dicts fall back to counting slice_df rows.

    The display table prefers extras["comps_table_df"] — an estimate-
    inclusive frame integration may pass for display only — so estimated
    deals still list (flagged Est.) without ever feeding the band."""
    comps = ctx.get("comps")
    out = {
        "available": False, "band_ok": False, "n_confirmed": 0, "n_used": 0,
        "fallback": False, "p25": None, "median": None, "p75": None,
        "deal_mult": deal_mult, "strip": None, "rows": [],
        "sev": _sev(comps_ins),
    }
    if not isinstance(comps, dict):
        return out
    out["available"] = True
    out["n_used"] = int(comps.get("n_used") or 0)
    out["fallback"] = bool(comps.get("fallback"))

    slice_df = comps.get("slice_df")
    # Always count confirmed rows from the slice the band was actually built
    # from. The old shortcut (n_confirmed = n_used whenever the key existed)
    # silently held only when estimates were excluded: with the editor's
    # include-estimates toggle ON, lookup_comps_band returns n_used=17 /
    # n_confirmed=4 (a whole-FRAME count), so the memo reported 17 confirmed,
    # set band_ok, drew a band from mostly-synthesized rows, and rendered
    # "no reliable band" directly beneath it. Provenance is per-row; count it.
    if slice_df is not None and len(slice_df) > 0:
        out["n_confirmed"] = sum(
            0 if _row_is_estimated(r.to_dict()) else 1
            for _, r in slice_df.iterrows()
        )
    elif "n_confirmed" in comps and not comps.get("includes_estimated"):
        # No slice frame to inspect, but the band excluded estimates by
        # construction, so the frame-level confirmed count is safe to trust.
        out["n_confirmed"] = out["n_used"]

    table_df = (extras or {}).get("comps_table_df")
    if table_df is None:
        table_df = slice_df
    rows = []
    if table_df is not None and len(table_df) > 0:
        sdf = table_df.sort_values("date", ascending=False).head(10)
        for _, r in sdf.iterrows():
            rec = r.to_dict()
            mult = rec.get("ev_ebitda_multiple")
            rows.append({
                "date": str(rec.get("date", ""))[:10],
                "buyer": str(rec.get("buyer", "") or ""),
                "seller": str(rec.get("seller", "") or ""),
                "aum": float(rec.get("seller_aum") or 0),
                "mult": float(mult) if _is_num(mult) else None,
                "est": _row_is_estimated(rec),
            })
    out["rows"] = rows

    ev = comps.get("ev_ebitda")
    if isinstance(ev, dict) and out["n_confirmed"] >= 5:
        out["band_ok"] = True
        out["p25"], out["median"], out["p75"] = ev["p25"], ev["median"], ev["p75"]
        out["strip"] = _band_positions(ev["p25"], ev["median"], ev["p75"], deal_mult)
    return out


def _exec_paragraph(d, ctx):
    """Page-1 auto-paragraph, four sentences, spec slot logic. Plain text."""
    firm = d["firm"]
    aum, revenue, ebitda = d["aum"], d["revenue"], d["ebitda"]
    bid = d["bid"]
    val = d["val"]
    parts = []

    # Sentence 1 -- who the target is.
    if revenue > 0:
        margin = ebitda / revenue * 100
        parts.append(
            f"{firm} manages {fmt_money(aum)} and produces {fmt_money(revenue)} "
            f"of revenue at a {margin:.0f}% EBITDA margin."
        )
    else:
        parts.append(
            f"{firm} manages {fmt_money(aum)}, with no revenue entered yet."
        )

    # Sentence 2 -- the bid against the band.
    band_txt = f"{fmt_money(val['low'])}–{fmt_money(val['high'])}"
    if bid <= 0:
        parts.append(
            f"No bid has been entered yet against the {band_txt} computed band."
        )
    elif not d["deal_mult"]:
        parts.append(
            f"The {fmt_money(bid)} bid cannot be benchmarked as a multiple "
            f"because adjusted EBITDA is zero at these inputs."
        )
    else:
        if bid < val["low"]:
            clause = f"below the {band_txt} computed band"
        elif bid <= val["high"]:
            clause = f"inside the {band_txt} computed band"
        else:
            clause = f"{fmt_money(bid - val['high'])} above the {band_txt} computed band"
        parts.append(
            f"The {fmt_money(bid)} bid implies {fmt_mult(d['deal_mult'])} "
            f"adjusted EBITDA, {clause}."
        )

    # Sentence 3 -- returns and breakeven.
    irr5 = d["ret"]["irr5"]
    if irr5 is None:
        parts.append("Buyer equity is zero at these terms, so IRR is undefined.")
    else:
        verb = {"positive": "clears", "caution": "sits inside",
                "negative": "misses"}.get(_sev(d["ins"]["returns"]))
        if verb is None:
            verb = "clears" if irr5 >= 0.22 else ("sits inside" if irr5 >= 0.18 else "misses")
        be_disp, _, be_kind = _breakeven_parts(d["ret"]["breakeven"])
        if be_kind == "year":
            n = int(be_disp.split()[-1])
            be_clause = (f"the deal breaks even in year {n}" if n <= 5
                         else f"breakeven slips to year {n}")
        elif be_kind == "over":
            be_clause = "the deal does not break even within seven years"
        else:
            be_clause = "breakeven is undefined at these terms"
        parts.append(
            f"Five-year IRR of {fmt_irr(irr5)} {verb} typical sponsor hurdles "
            f"and {be_clause}."
        )

    # Sentence 4 -- worst non-returns flag, in prose. Severity comes from
    # insights.py; only the display numbers are read off the engine output.
    parts.append(_worst_flag_sentence(d))
    return " ".join(p for p in parts if p)


def _worst_flag_sentence(d):
    order = ["dscr", "attrition", "synergy", "comps"]
    pick, pick_sev = None, None
    for want in ("negative", "caution"):
        for key in order:
            if _sev(d["ins"].get(key)) == want:
                pick, pick_sev = key, want
                break
        if pick:
            break
    if pick is None:
        return "No covenant or retention flags at these assumptions."

    if pick == "dscr" and d["ret"]["min_dscr"] is not None:
        v, n = d["ret"]["min_dscr"], d["ret"]["min_dscr_year"]
        if pick_sev == "negative":
            return f"Year-{n} DSCR of {fmt_dscr(v)} breaches a {fmt_dscr(DSCR_COVENANT)} covenant."
        return f"Year-{n} DSCR of {fmt_dscr(v)} leaves thin covenant cushion."
    if pick == "comps" and d["comps"]["band_ok"] and d["comps"]["deal_mult"]:
        delta = d["comps"]["deal_mult"] - d["comps"]["p75"]
        if delta > 0:
            return f"The deal prices {delta:.1f}× above the comparable range."
    if pick == "synergy":
        text = _plain(d["ins"]["synergy"])
        if text:
            return "The deal " + text[0].lower() + text[1:]
    # Attrition (and any fallback): the insight text already reads as prose.
    text = _plain(d["ins"].get(pick))
    return text if text else "No covenant or retention flags at these assumptions."


def _risk_bullets(d, ctx):
    """Page-4 open questions per the spec rule table. 3-5 plain-text bullets."""
    inp = ctx.get("inputs") or {}
    bullets = []

    # 1 -- bid above band
    if d["bid"] > d["val"]["high"] > 0:
        bullets.append("Price sits above the computed band — what upside justifies the premium?")
    # 2 -- covenant breach
    v, n = d["ret"]["min_dscr"], d["ret"]["min_dscr_year"]
    if v is not None and v < DSCR_COVENANT:
        bullets.append(
            f"Y{n} DSCR of {fmt_dscr(v)} breaches typical bank covenants "
            f"— restructure debt or raise equity?"
        )
    # 3 -- high attrition
    att = float(inp.get("attrition_rate") or 0)
    if att >= 0.08:
        bullets.append(
            f"{att*100:.0f}% attrition is high for this book — is a key person "
            f"or fee change driving it?"
        )
    # 4 -- weak buyer capabilities (only meaningful with a live schedule)
    if d["syn"]["mode"] == "schedule" and d["syn"]["capability"] is not None \
            and d["syn"]["capability"] < 0.6:
        cf = d["syn"]["capability"]
        bullets.append(
            f"Buyer capabilities cover {cf*100:.0f}% of modeled synergies "
            f"— who actually does the integration work?"
        )
    # 5 -- thin comps. Two distinct states: a band exists but is thin, or no
    # band was drawn at all. Saying "rests on 0 deals" in the second case
    # contradicted page 5, which correctly withholds the band entirely.
    if d["comps"]["available"]:
        if not d["comps"]["band_ok"]:
            bullets.append(
                "No confirmed comps in this slice — the band is withheld; "
                "source a broker quote before pricing."
            )
        elif d["comps"]["fallback"] or d["comps"]["n_used"] < 5:
            where = "after widening tiers" if d["comps"]["fallback"] else "in this slice"
            bullets.append(
                f"Comp band rests on {d['comps']['n_used']} deals {where} "
                f"— validate against a broker quote."
            )
    # 6 -- mega-RIA
    if d["aum"] > 10e9:
        bullets.append(
            f"At {fmt_money(d['aum'])} this is a mega-RIA — the band is "
            f"directional; expect bespoke enterprise pricing."
        )
    # 7 -- no breakeven
    _, _, be_kind = _breakeven_parts(d["ret"]["breakeven"])
    if be_kind == "over":
        bullets.append("Invested cash is not recovered within seven years — what is the exit story?")
    # 8 -- earnout-heavy
    earnout = d["structure_yours"].get("earnout", 0)
    if earnout >= 0.30:
        bullets.append(
            f"{earnout*100:.0f}% of consideration is earnout — will the seller "
            f"accept this much at-risk?"
        )
    # 9 -- thin recurring. rec == 0 is the untouched default, not a real
    # answer: it drives the band's recurring factor to its floor and quietly
    # understates the valuation. Call that out rather than skipping it.
    rec = float(inp.get("pct_recurring") or 0)
    if rec <= 0:
        bullets.append(
            "% recurring revenue is not set — the valuation band assumes the "
            "least favourable case and is understated until you enter it."
        )
    elif rec < 70:
        bullets.append(
            f"Only {rec:.0f}% recurring revenue — how durable is the "
            f"non-recurring book?"
        )

    bullets = bullets[:5]
    for standing in _STANDING_BULLETS:
        if len(bullets) >= 3:
            break
        bullets.append(standing)
    return bullets


def _synergy_digest(ctx):
    sched = ctx.get("synergy_schedule")
    if sched is None or len(sched) == 0:
        inp = ctx.get("inputs") or {}
        ic = float(inp.get("integration_costs") or 0)
        asyn = float(inp.get("annual_synergies") or 0)
        if ic > 0 or asyn > 0:
            note = (
                f"Integration is modeled manually: {fmt_money(ic)} of one-time "
                f"integration cost and {fmt_money(asyn)} of flat annual synergies. "
                f"The ramped synergy schedule is off."
            )
        else:
            note = ("Integration is modeled with manual inputs — the ramped "
                    "synergy schedule is off.")
        return {"mode": "manual", "years": [], "note": note, "capability": None}

    years = []
    for _, r in sched.iterrows():
        yr = int(r["year"])
        if yr > 5:
            break
        years.append((yr, float(r["net_synergy"])))
    attrs = getattr(sched, "attrs", {}) or {}
    runrate = attrs.get("runrate_total_synergy")
    npv = attrs.get("synergy_npv")
    capture = attrs.get("total_capture_cost")
    capability = attrs.get("capability_factor")
    y1_neg = bool(years and years[0][1] < 0)
    note = (
        f"Run-rate synergy reaches {fmt_money(runrate)} by Y3 "
        f"(NPV {fmt_money(npv)} at 10%). Capturing it costs {fmt_money(capture)}, "
        f"front-loaded 60/30/10 across the first three years"
    )
    note += " — Y1 is a net investment." if y1_neg else "."
    return {"mode": "schedule", "years": years, "note": note,
            "capability": float(capability) if _is_num(capability) else None}


def build_memo_data(ctx, extras):
    """Assemble the full memo digest. Both the on-screen renderer and
    `pdf_export.generate_memo_pdf` consume this one structure, so the two
    artifacts can never drift apart."""
    if is_zero_state(ctx):
        return {"zero": True}
    if not _HAS_INSIGHTS:
        raise RuntimeError(
            "insights.py is required to build the deal memo (import failed)."
        )

    extras = extras or {}
    inp = ctx.get("inputs") or {}
    aum = float(inp.get("aum") or 0)
    revenue = float(inp.get("revenue") or 0)
    ebitda = float(inp.get("ebitda") or 0)
    bid = float(ctx.get("purchase_price") or 0)
    valuation = ctx.get("valuation") or {}
    returns = ctx.get("returns") or {}
    pro_forma = ctx.get("pro_forma")

    insights = list(compute_all_insights(ctx) or [])
    verdict_raw, counts_raw = verdict_from_insights(insights)
    verdict = str(verdict_raw or "").strip().lower()
    if verdict not in ("go", "caution", "reconsider"):
        verdict = "caution"
    neg = sum(1 for i in insights if _sev(i) == "negative")
    cau = sum(1 for i in insights if _sev(i) == "caution")
    pos = sum(1 for i in insights if _sev(i) == "positive")

    ins = {k: _find_insight(insights, k)
           for k in ("valuation", "returns", "dscr", "synergy", "comps", "attrition")}

    adj_ebitda = float(valuation.get("adj_ebitda") or 0)
    deal_mult = (bid / adj_ebitda) if (adj_ebitda > 0 and bid > 0) else None
    val = {
        "low": float(valuation.get("low") or 0),
        "mid": float(valuation.get("mid") or 0),
        "high": float(valuation.get("high") or 0),
    }

    # Pro forma rows Y1-Y5. Debt service includes the seller note so the
    # column stays consistent with the DSCR printed beside it.
    pf_rows = []
    if pro_forma is not None and len(pro_forma) > 0:
        for _, r in pro_forma[pro_forma["year"] <= 5].iterrows():
            debt = float(r["debt_service"]) + float(r.get("seller_note_payment", 0))
            dscr_v = r.get("dscr")
            dscr_v = float(dscr_v) if _is_num(dscr_v) else None
            if dscr_v is None and debt > 0:
                dscr_v = float(r["ebitda"]) / debt
            pf_rows.append({
                "year": int(r["year"]), "revenue": float(r["revenue"]),
                "ebitda": float(r["ebitda"]), "debt": debt,
                "ncf": float(r["net_cash_flow"]), "dscr": dscr_v,
            })

    min_dscr, min_dscr_year = _min_dscr(pro_forma)
    irr5 = returns.get("irr_yr5")
    coc5 = returns.get("coc_yr5")
    ret = {
        "irr5": float(irr5) if _is_num(irr5) else None,
        "coc5": float(coc5) if _is_num(coc5) else None,
        "breakeven": returns.get("breakeven_year"),
        "exit_mult": float(returns.get("exit_multiple") or 0),
        "min_dscr": min_dscr, "min_dscr_year": min_dscr_year,
    }

    be_disp, be_sev, _ = _breakeven_parts(ret["breakeven"])
    coc_sev = "muted"
    if ret["coc5"] is not None:
        coc_sev = ("positive" if ret["coc5"] >= 1.5
                   else "caution" if ret["coc5"] >= 1.0 else "negative")
    irr_sev = _sev(ins["returns"]) if ret["irr5"] is not None else "muted"
    dscr_sev = _sev(ins["dscr"]) if min_dscr is not None else "muted"
    stat_dots = {
        "irr": irr_sev if irr_sev in _SEVS else "muted",
        "coc": coc_sev,
        "be": be_sev if be_sev in _SEVS else "muted",
        "dscr": dscr_sev if dscr_sev in _SEVS else "muted",
    }

    suggested = extras.get("suggested_deal") or {}
    actual = extras.get("structure_actual") or {}
    structure_yours = {
        "upfront": float(actual.get("upfront", inp.get("pct_upfront", 0)) or 0),
        "note": float(actual.get("note", inp.get("pct_seller_note", 0)) or 0),
        "earnout": float(actual.get("earnout", inp.get("pct_earnout", 0)) or 0),
        "rollover": float(actual.get("rollover", inp.get("pct_equity_rollover", 0)) or 0),
    }
    structure_rows = []
    if suggested:
        structure_rows.append(("Suggested", [
            ("upfront", float(suggested.get("upfront") or 0)),
            ("note", float(suggested.get("note") or 0)),
            ("earnout", float(suggested.get("earnout") or 0)),
            ("rollover", float(suggested.get("rollover") or 0)),
        ]))
    if sum(structure_yours.values()) > 0:
        structure_rows.append(("Yours", [
            (k, structure_yours[k]) for k in ("upfront", "note", "earnout", "rollover")
        ]))

    firm = (extras.get("firm_name") or "").strip() or "Unnamed target"
    today = date.today()
    date_long = f"{today:%B} {today.day}, {today.year}"
    prepared_for = (extras.get("prepared_for") or "internal review").strip()

    profile_key = str(suggested.get("profile") or "")
    profile_label = _PROFILE_LABELS.get(
        profile_key, profile_key.replace("_", " ").capitalize() or "n/a")

    d = {
        "zero": False,
        "firm": firm,
        "date_long": date_long,
        "meta_line": f"{date_long} · {fmt_money(aum)} AUM · prepared for {prepared_for}",
        "aum": aum, "revenue": revenue, "ebitda": ebitda, "bid": bid,
        "adj_ebitda": adj_ebitda, "deal_mult": deal_mult, "val": val,
        "band": _band_positions(val["low"], val["mid"], val["high"], bid),
        "verdict": verdict,
        "verdict_word": verdict.upper(),
        "verdict_raw": verdict_raw, "counts_raw": counts_raw,
        "counts": (neg, cau, pos),
        # Name the arithmetic: an unqualified verdict word on page 1 of a
        # forwardable memo is advice-shaped. "N of 6 checks scored" says out
        # loud that this is a model screen, not a recommendation.
        "counts_line": (
            "enter EBITDA and a bid to underwrite"
            if verdict_raw == "INCOMPLETE"
            else f"{neg} flags · {cau} caution · {pos} clear "
                 f"· {neg + cau + pos} of 6 checks scored"
        ),
        "insights": insights, "ins": ins,
        "stats_target": [
            ("AUM", fmt_money(aum)),
            ("Revenue", fmt_money(revenue)),
            ("EBITDA", fmt_money(ebitda)),
        ],
        "stats_bid": [
            ("Bid", fmt_money(bid) if bid > 0 else "n/a"),
            ("Implied multiple", fmt_mult(deal_mult)),
            ("Structure profile", profile_label),
        ],
        "profile_label": profile_label,
        "rationale": str(suggested.get("rationale") or ""),
        "structure_rows": structure_rows,
        "structure_yours": structure_yours,
        "pf_rows": pf_rows,
        "ret": ret,
        "stat_dots": stat_dots,
        # The returns insight already carries the "typical sponsor hurdles"
        # hedge in its text; this caption supplies the number and the exit
        # assumption without repeating the phrase. (It used to say it a third
        # time in three consecutive lines — five reviewers flagged it as
        # machine-generated boilerplate in a document meant to be forwarded.)
        "hurdle_caption": (
            f"Hurdle reference: ~18–22% five-year IRR — directional, not a "
            f"market quote. Exit assumes {fmt_mult(ret['exit_mult'])} EBITDA "
            f"at the recurring-revenue level entered."
        ),
        "syn": _synergy_digest(ctx),
        "comps": _comps_digest(ctx, extras, deal_mult,
                               _find_insight(insights, "comps")),
    }
    d["paragraph"] = _exec_paragraph(d, ctx)
    d["bullets"] = _risk_bullets(d, ctx)
    return d


# ==============================================================================
# HTML renderers (screen)
# ==============================================================================
APP_URL = "ria-ma-calc.streamlit.app"

# Shown once, on the last page. The memo is built to be printed and forwarded,
# so it has to defend itself in a room nobody from here is standing in: every
# number traces to user inputs and unverified public-data estimates, and the
# verdict word is flag arithmetic, not a recommendation.
MEMO_DISCLAIMER = (
    "All figures derive solely from the inputs entered and from estimates "
    "built on unverified public data. This memo is a modeling aid — not a "
    "valuation opinion, fairness opinion, or investment, legal, or tax "
    "advice. The GO / CAUTION / RECONSIDER verdict is a model output "
    "computed from the checks listed above, not a recommendation to "
    "transact. Verify every input independently before relying on it."
)


def _footer_html(n):
    return (
        '<div class="memo-footer">'
        f'<span>Prepared with RIA M&amp;A Calculator — {APP_URL}</span>'
        f'<span>Page {n} of 5</span></div>'
    )


def _pagehead_html(d, n):
    return (
        f'<div class="memo-pagehead"><span>{_h(d["firm"])}</span>'
        f'<span>{_h(d["date_long"])}</span></div>'
        f'<span class="memo-pagenum">{n} / 5</span>'
    )


def _memo_h(title, note=None):
    note_html = ""
    if note:
        note_html = (
            '<span style="margin-left:auto;font-weight:400;text-transform:none;'
            f'letter-spacing:0.02em;color:#6b7385;">{_h(note)}</span>'
        )
    return f'<div class="memo-h">{_h(title)}{note_html}</div>'


def _stat_tile(label, value, dot_sev=None):
    dot = ""
    if dot_sev is not None:
        cls = f" memo-stat-dot--{dot_sev}" if dot_sev in _SEVS else ""
        dot = f'<span class="memo-stat-dot{cls}"></span>'
    return (
        f'<div class="memo-stat"><h4>{_h(label)}</h4>'
        f'<div class="memo-stat-val">{dot}{_h(value)}</div></div>'
    )


def _insight_html(ins, drop_sub=False):
    """Render an insight via insights.render_insight, with a quiet fallback.

    drop_sub suppresses the hedge sub-label for strips whose page already
    states the same reference in a caption (see the returns strip on page 3);
    the calculator keeps the sub, since it has no caption to lean on."""
    if ins is None:
        return ""
    if drop_sub and isinstance(getattr(ins, "data", None), dict) and ins.data.get("sub_html"):
        ins = replace(ins, data={k: v for k, v in ins.data.items()
                                 if k not in ("sub_html", "sub")})
    try:
        out = render_insight(ins)
        if isinstance(out, str) and out.strip():
            return out
    except Exception:
        pass
    sev = _sev(ins)
    cls = f" insight--{sev}" if sev in _SEVS + ("info",) else ""
    return (
        f'<div class="insight{cls}"><span class="insight-dot"></span>'
        f'<span class="insight-text">{_h(_plain(ins))}</span></div>'
    )


def _verdict_html(d):
    try:
        out = render_verdict_bar(d["verdict_raw"], d["counts_raw"])
        if isinstance(out, str) and out.strip():
            return out
    except TypeError:
        try:
            out = render_verdict_bar(d["insights"])
            if isinstance(out, str) and out.strip():
                return out
        except Exception:
            pass
    except Exception:
        pass
    return (
        f'<div class="memo-verdict memo-verdict--{d["verdict"]}">'
        '<span class="memo-verdict-dot"></span>'
        f'<span class="memo-verdict-word">{d["verdict_word"]}</span>'
        f'<span class="memo-verdict-counts">{_h(d["counts_line"])}</span></div>'
    )


def _band_html(positions, axis_labels, marker_label, sev, track_only=False,
               track_only_label=None):
    """Large band number-line (pages 2 and 5). Inline positions per spec 2.3."""
    parts = ['<div class="memo-band">', '<div class="memo-band-track"></div>']
    if not track_only and positions:
        left = positions["low"]
        width = max(0.0, positions["high"] - positions["low"])
        parts.append(
            f'<div class="memo-band-seg" style="left:{left}%;width:{width}%;"></div>'
        )
        for key in ("low", "mid", "high"):
            parts.append(
                f'<div class="memo-band-tick" style="left:{positions[key]}%;"></div>'
            )
        for key, label in axis_labels:
            parts.append(
                f'<div class="memo-band-axis" style="left:{positions[key]}%;">{label}</div>'
            )
    bid_pos = positions.get("bid") if positions else None
    if bid_pos is not None:
        mcls = f" memo-band-marker--{sev}" if sev in _SEVS else ""
        lcls = f" memo-band-bidlabel--{sev}" if sev in _SEVS else ""
        parts.append(
            f'<div class="memo-band-marker{mcls}" style="left:{bid_pos}%;"></div>'
        )
        if marker_label:
            parts.append(
                f'<div class="memo-band-bidlabel{lcls}" style="left:{bid_pos}%;">'
                f'{marker_label}</div>'
            )
    if track_only and track_only_label:
        parts.append(
            f'<div class="memo-band-axis" style="left:50%;">{_h(track_only_label)}</div>'
        )
    parts.append('</div>')
    return "".join(parts)


_SEG_ORDER = ("upfront", "note", "earnout", "rollover")
_SEG_COLORS = {"upfront": "#7c8cff", "note": "#8b94a8",
               "earnout": "#6b7385", "rollover": "#2f3646"}


def _structure_html(d):
    parts = ['<div class="memo-structure">']
    for label, segs in d["structure_rows"]:
        parts.append('<div class="memo-structure-row">')
        parts.append(f'<div class="memo-structure-label">{_h(label)}</div>')
        parts.append('<div class="memo-structure-bar">')
        for seg, frac in segs:
            pct = frac * 100
            if pct <= 0:
                continue
            text = f"{pct:.0f}" if pct >= 8 else ""
            parts.append(
                f'<div class="memo-structure-seg memo-structure-seg--{seg}" '
                f'style="width:{pct:.1f}%;">{text}</div>'
            )
        parts.append('</div></div>')
    parts.append('</div>')
    legend = ['<div class="memo-legend">']
    for seg in _SEG_ORDER:
        legend.append(
            f'<span><i style="background:{_SEG_COLORS[seg]};"></i>{seg.capitalize()}</span>'
        )
    legend.append('</div>')
    return "".join(parts) + "".join(legend)


def _synergy_bars_html(d):
    years = d["syn"]["years"]
    if not years:
        return ""
    max_abs = max(abs(v) for _, v in years) or 1.0
    heights = [abs(v) / max_abs * 72.0 for _, v in years]
    neg_zone = max(
        (h for (_yr, v), h in zip(years, heights) if v < 0), default=0.0
    )
    extra_mb = "margin-bottom:24px;" if neg_zone > 0 else ""
    parts = [f'<div class="memo-bars" style="{extra_mb}">']
    parts.append(
        f'<div style="position:absolute;left:0;right:0;bottom:{neg_zone:.0f}px;'
        'height:1px;background:#1c2230;"></div>'
    )
    for (yr, v), h in zip(years, heights):
        label = ("−" if v < 0 else "") + f"&#36;{abs(v)/1e6:.1f}M"
        if v < 0:
            mb = max(0.0, neg_zone - h)
            parts.append(
                f'<div class="memo-bar memo-bar--neg" style="height:{h:.0f}px;'
                f'align-self:flex-end;margin-bottom:{mb:.0f}px;">'
                f'<span class="memo-bar-label">{label}</span></div>'
            )
        else:
            parts.append(
                f'<div class="memo-bar" style="height:{max(h, 2.0):.0f}px;'
                f'margin-bottom:{neg_zone:.0f}px;">'
                f'<span class="memo-bar-label">{label}</span></div>'
            )
    parts.append('</div>')
    parts.append('<div class="memo-bars-years">')
    for yr, _v in years:
        parts.append(f'<span>Y{yr}</span>')
    parts.append('</div>')
    return "".join(parts)


def _page1_html(d):
    p = ['<div class="memo-page">']
    p.append('<span class="memo-pagenum">1 / 5</span>')
    p.append('<div class="memo-kicker">Acquisition memo</div>')
    p.append(f'<div class="memo-title">{_h(d["firm"])}</div>')
    p.append(f'<div class="memo-meta">{_h(d["meta_line"])}</div>')
    p.append('<div class="memo-stat-strip">')
    for label, value in d["stats_target"]:
        p.append(_stat_tile(label, value))
    p.append('</div>')
    p.append('<div class="memo-stat-strip">')
    for label, value in d["stats_bid"]:
        p.append(_stat_tile(label, value))
    p.append('</div>')
    p.append(_verdict_html(d))
    p.append(f'<div class="memo-para">{_h(d["paragraph"])}</div>')
    p.append(_footer_html(1))
    p.append('</div>')
    return "".join(p)


def _page2_html(d):
    p = ['<div class="memo-page">', _pagehead_html(d, 2)]
    p.append(_memo_h("Valuation band"))
    if d["band"]:
        axis_labels = [
            ("low", f'Low {fmt_money(d["val"]["low"]).replace("$", "&#36;")}'),
            ("mid", f'Mid {fmt_money(d["val"]["mid"]).replace("$", "&#36;")}'),
            ("high", f'High {fmt_money(d["val"]["high"]).replace("$", "&#36;")}'),
        ]
        marker_label = None
        if d["bid"] > 0:
            marker_label = f'your bid {fmt_money(d["bid"]).replace("$", "&#36;")}'
        p.append(_band_html(d["band"], axis_labels, marker_label,
                            _sev(d["ins"]["valuation"])))
        p.append('<div style="height:14px;"></div>')
    else:
        p.append(
            '<div class="memo-caption">Valuation band unavailable — '
            'adjusted EBITDA is zero at these inputs.</div>'
        )
    p.append(_insight_html(d["ins"]["valuation"]))
    p.append(_memo_h("Deal structure", "upfront → rollover = certainty ramp"))
    if d["structure_rows"]:
        p.append(_structure_html(d))
    if d["rationale"]:
        p.append(f'<div class="memo-caption">{_h(d["rationale"])}</div>')
    p.append(_footer_html(2))
    p.append('</div>')
    return "".join(p)


def _page3_html(d):
    p = ['<div class="memo-page">', _pagehead_html(d, 3)]
    p.append(_memo_h("Pro forma, five years"))
    if d["pf_rows"]:
        p.append('<table class="memo-table"><thead><tr>')
        for hcol in ("Year", "Revenue", "EBITDA", "Debt service", "Net cash flow", "DSCR"):
            p.append(f'<th>{hcol}</th>')
        p.append('</tr></thead><tbody>')
        for r in d["pf_rows"]:
            p.append(f'<tr><td>Y{r["year"]}</td>')
            for v in (r["revenue"], r["ebitda"], r["debt"], r["ncf"]):
                cls = ' class="memo-td-neg"' if v < 0 else ""
                p.append(f'<td{cls}>{_h(fmt_money_table(v))}</td>')
            p.append(f'<td>{_h(fmt_dscr(r["dscr"]))}</td></tr>')
        p.append('</tbody></table>')
    else:
        p.append('<div class="memo-caption">Pro forma unavailable at these inputs.</div>')
    p.append(_memo_h("Returns"))
    ret, dots = d["ret"], d["stat_dots"]
    be_disp, _, _ = _breakeven_parts(ret["breakeven"])
    dscr_val = "n/a"
    if ret["min_dscr"] is not None:
        dscr_val = f'{fmt_dscr(ret["min_dscr"])} (Y{ret["min_dscr_year"]})'
    p.append('<div class="memo-stat-strip">')
    p.append(_stat_tile("5Y IRR", fmt_irr(ret["irr5"]), dots["irr"]))
    p.append(_stat_tile("5Y cash-on-cash", fmt_mult(ret["coc5"]), dots["coc"]))
    p.append(_stat_tile("Breakeven", be_disp, dots["be"]))
    p.append(_stat_tile("Lowest DSCR", dscr_val, dots["dscr"]))
    p.append('</div>')
    # drop_sub: the hurdle caption below states the ~18-22% reference already.
    p.append(_insight_html(d["ins"]["returns"], drop_sub=True))
    p.append(_insight_html(d["ins"]["dscr"]))
    p.append(f'<div class="memo-caption">{_h(d["hurdle_caption"])}</div>')
    p.append(_footer_html(3))
    p.append('</div>')
    return "".join(p)


def _page4_html(d):
    p = ['<div class="memo-page">', _pagehead_html(d, 4)]
    if d["syn"]["mode"] == "schedule":
        p.append(_memo_h("Synergy ramp"))
        p.append(_synergy_bars_html(d))
        p.append(f'<div class="memo-caption">{_h(d["syn"]["note"])}</div>')
    else:
        p.append(_memo_h("Integration plan"))
        p.append(f'<div class="memo-para">{_h(d["syn"]["note"])}</div>')
    p.append(_insight_html(d["ins"]["synergy"]))
    p.append(_memo_h("Open questions"))
    p.append('<ul class="memo-bullets">')
    for b in d["bullets"]:
        p.append(f'<li>{_h(b)}</li>')
    p.append('</ul>')
    p.append(_footer_html(4))
    p.append('</div>')
    return "".join(p)


def _page5_html(d):
    c = d["comps"]
    p = ['<div class="memo-page">', _pagehead_html(d, 5)]
    p.append(_memo_h("Comps benchmark"))
    marker_label = None
    if c["deal_mult"]:
        marker_label = _h(f'your deal {fmt_mult(c["deal_mult"])}')
    if c["band_ok"] and c["strip"]:
        axis_labels = [
            ("low", _h(f'P25 {fmt_mult(c["p25"])}')),
            ("mid", _h(f'Median {fmt_mult(c["median"])}')),
            ("high", _h(f'P75 {fmt_mult(c["p75"])}')),
        ]
        p.append(_band_html(c["strip"], axis_labels, marker_label, c["sev"]))
        p.append('<div style="height:14px;"></div>')
        p.append(_insight_html(d["ins"]["comps"]))
    else:
        positions = {"bid": 50.0} if c["deal_mult"] else None
        p.append(_band_html(positions, [], marker_label, "muted",
                            track_only=True, track_only_label="no reliable band"))
        p.append('<div style="height:14px;"></div>')
        p.append(
            '<div class="memo-empty">'
            f'<div class="memo-empty-title">Insufficient confirmed comparables '
            f'— N={c["n_confirmed"]}.</div>'
            '<div class="memo-empty-sub">Multiples for most RIA deals are never '
            'disclosed. The band above is withheld rather than built on '
            'estimates.</div></div>'
        )
    if c["rows"]:
        p.append('<table class="memo-table"><thead><tr>')
        for hcol in ("Date", "Buyer", "Seller", "AUM", "EV/EBITDA", "Source"):
            p.append(f'<th>{hcol}</th>')
        p.append('</tr></thead><tbody>')
        for r in c["rows"]:
            src_cls = "memo-td-est" if r["est"] else "memo-td-conf"
            src_txt = "Est." if r["est"] else "Confirmed"
            p.append(
                f'<tr><td>{_h(r["date"])}</td>'
                f'<td class="memo-td-text">{_h(r["buyer"])}</td>'
                f'<td class="memo-td-text">{_h(r["seller"])}</td>'
                f'<td>{_h(fmt_money(r["aum"]))}</td>'
                f'<td>{_h(fmt_mult(r["mult"]))}</td>'
                f'<td class="{src_cls}">{src_txt}</td></tr>'
            )
        p.append('</tbody></table>')
        p.append(
            '<div class="memo-caption">Est. = modeled estimate; the multiple '
            'was not disclosed by the parties and is excluded from the '
            'benchmark above. Confirmed = multiple stated in, or derived '
            'from, a cited primary source.</div>'
        )
    elif not c["available"]:
        p.append(
            '<div class="memo-caption">No comparable-transactions database is '
            'available in this deployment.</div>'
        )
    p.append(f'<div class="memo-disclaimer">{_h(MEMO_DISCLAIMER)}</div>')
    p.append(_footer_html(5))
    p.append('</div>')
    return "".join(p)


_EMPTY_STATE_HTML = (
    '<div style="max-width:760px;margin:0 auto;">'
    '<div class="memo-empty">'
    '<div class="memo-empty-title">No deal to write up yet.</div>'
    '<div class="memo-empty-sub">Enter firm data in the sidebar — the memo '
    'assembles itself as you type.</div>'
    '</div></div>'
)


def render_memo_tab(ctx, extras):
    """Streamlit entry point: the on-screen 5-page memo plus PDF download."""
    import streamlit as st

    if is_zero_state(ctx):
        st.markdown(_EMPTY_STATE_HTML, unsafe_allow_html=True)
        return

    d = build_memo_data(ctx, extras)
    for page_fn in (_page1_html, _page2_html, _page3_html, _page4_html, _page5_html):
        st.markdown(page_fn(d), unsafe_allow_html=True)

    try:
        from pdf_export import generate_memo_pdf
        pdf_bytes = generate_memo_pdf(ctx, extras)
        _c1, _c2, _c3 = st.columns([1, 1, 1])
        with _c2:
            st.download_button(
                "Download memo (PDF)",
                data=pdf_bytes,
                file_name=f"deal_memo_{date.today():%Y-%m-%d}.pdf",
                mime="application/pdf",
                key="_memo_pdf_download",
                use_container_width=True,
            )
    except Exception:
        import logging
        logging.exception("Memo PDF generation failed")
        st.caption(
            "PDF export is unavailable right now — the on-screen memo "
            "above is unaffected."
        )
