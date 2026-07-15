"""
RIA M&A Calculator — Insight layer ("the so-what layer").

Turns computed deal outputs into at most one salient verdict per section,
per the sprint design spec (design-spec.md sections 2.1-2.5). Six insight
kinds — valuation, returns, dscr, synergy, comps, attrition — each carrying
one severity (positive / caution / negative / info) expressed visually as a
3px left border + 6px dot, never by copy volume.

Public API (hard contract — the Deal Memo module codes against these names):

    Insight                       dataclass: kind, severity, text, data
    compute_all_insights(ctx)     -> list[Insight]
    select_insight(insights, s)   -> Optional[Insight]   (per-tab priority)
    verdict_from_insights(list)   -> ("GO"|"CAUTION"|"RECONSIDER", counts)
    render_insight(i, with_microviz=False) -> HTML str
    render_verdict_bar(verdict, counts)    -> HTML str
    INSIGHT_CSS                   -> str, paste-ready for _THEME_CSS

`ctx` keys (all optional except the intent that integration builds it once):

    purchase_price   int/float — the bid.
    valuation        dict from compute_valuation_band (low/mid/high/adj_ebitda).
    returns          dict from compute_irr_and_returns (irr_yr5, breakeven_year).
    pro_forma        DataFrame from build_pro_forma (year, ebitda,
                     debt_service, seller_note_payment, optionally dscr).
    synergy_schedule DataFrame from compute_synergy_schedule, or None when the
                     user has switched to manual-override integration inputs.
                     Key present-and-None => manual mode (info state);
                     key absent          => synergy insight not produced.
    comps            dict from lookup_comps_band or None. n_confirmed /
                     n_estimated_excluded are honored when present (added by
                     the comps workstream) and safely ignored when absent.
    inputs           flat dict with at least: aum, revenue, ebitda,
                     pct_recurring, attrition_rate, growth_rate.
    pro_forma_kwargs (extension) full kwargs dict for calculations.build_pro_forma,
                     required for the synergy-capture and attrition-headroom
                     stress re-runs. Absent => those two insights are not
                     produced (never guessed).
    irr_kwargs       (extension) kwargs for calculations.compute_irr_and_returns
                     minus the pro_forma positional (purchase_price, pct_upfront,
                     pct_self_funded, pct_equity_rollover, pct_recurring,
                     loan_schedule, note_schedule, pct_seller_note).

Every ctx member may be None/absent: a missing input means that insight is
simply not produced. Zero-state (aum, revenue and ebitda all 0) produces [].

Computation functions are Streamlit-free and unit-testable; the render
helpers return plain HTML strings for st.markdown(..., unsafe_allow_html=True).
No session state is used.
"""

from dataclasses import dataclass, field
from typing import Optional
import html as _html
import math as _math

from calculations import build_pro_forma, compute_irr_and_returns


# ------------------------------------------------------------------------
# Thresholds (single source of truth; the memo must not re-derive these)
# ------------------------------------------------------------------------
# ~18-22% is the commonly quoted equity-return hurdle range for lower-middle-
# market PE / sponsor-backed deals; it varies by fund, vintage and leverage.
# Copy always hedges it as "typical sponsor hurdles ~18-22%" — never present
# these two numbers as a precise market constant.
HURDLE_LOW = 0.18
HURDLE_HIGH = 0.22

# ~1.25x is the widely-cited bank covenant floor for RIA acquisition loans
# (SBA and conventional cash-flow lenders commonly underwrite to 1.20-1.30x).
# The spec's copy references "1.25x covenant" verbatim; anywhere else it is
# hedged as "typical bank covenants" — do not present as universal.
DSCR_COVENANT = 1.25
# 1.50x as "comfortable" is a heuristic reading of covenant headroom (a 25%+
# cushion above the ~1.25x floor), not a market-quoted line.
DSCR_COMFORT = 1.50

# Synergy-capture scan grid per spec section 2.5 item 4. capture_cost is kept
# at 100% at every f — conservative: integration spend is committed even if
# the benefits slip.
CAPTURE_GRID = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

# Numerical guard for the stress scans: calculations._compute_irr uses
# Newton's method, which returns unbounded garbage (1e10..1e17 "IRRs") when
# the cash-flow series has no sign change (e.g. every year negative). A
# five-year IRR outside (-99%, +1,000%) on an RIA acquisition is numerical
# noise, not a signal, so such values are treated as "does not clear" in the
# capture scan. This errs conservative — a broken series can never read as
# clearing the hurdle.
IRR_SANITY_CAP = 10.0

# A bid this far under the band's low end stops reading as a bargain and starts
# reading as a question about whether the seller transacts at all. 0.85 is a
# judgement call, not a market statistic — it exists so the model shows some
# skepticism rather than stamping GO on an implausible steal.
DEEP_DISCOUNT_FRACTION = 0.85

# Insight kinds that are meaningless without a bid. Per spec section 2.4 the
# calculator renders them only when purchase_price > 0; compute still emits
# their info states (flagged data["gated"]=True) so the memo layer can reason
# about them without re-deriving.
_BID_DEPENDENT = {"valuation", "comps", "returns", "dscr", "attrition"}

_SEVERITIES = ("positive", "caution", "negative", "info")

_SECTION_ALIASES = {
    "valuation": "valuation", "summary": "valuation", "deal_summary": "valuation",
    "financials": "financials", "pro_forma": "financials",
    "returns": "returns", "stress": "returns", "returns_stress": "returns",
    "integration": "integration", "synergy": "integration",
}


@dataclass
class Insight:
    kind: str        # "valuation" | "returns" | "dscr" | "synergy" | "comps" | "attrition"
    severity: str    # "positive" | "caution" | "negative" | "info"
    text: str        # plain-text sentence, <=8 words, <=2 numbers (spec 2.5)
    data: dict = field(default_factory=dict)  # micro-viz payload + render extras


# ------------------------------------------------------------------------
# Formatting (spec: dollars per fmt_dollar, multiples X.Xx, IRR XX.X%,
# DSCR X.XXx — with the real multiplication sign in copy)
# ------------------------------------------------------------------------

def _fmt_dollar(val: float) -> str:
    """Mirror of app.py fmt_dollar — $X.XB / $X.XM / $XK / $X."""
    v = float(val)
    if abs(v) >= 1e9:
        return f"${v / 1e9:,.1f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.1f}M"
    if abs(v) >= 1e3:
        return f"${v / 1e3:,.0f}K"
    return f"${v:,.0f}"


def _fmt_band_delta(val: float) -> str:
    """Dollar delta measured against a $M-scale valuation band.

    The spec renders the S2 over-band delta ($694K) as "$0.7M", not "$694K",
    in all three places it appears — deltas read in the band's own unit so
    "$0.7M above the $11.7M-$15.8M band" compares like-for-like. We therefore
    format 100K..1B deltas as $X.XM and fall back to _fmt_dollar below 100K
    (where "$0.1M" would overstate precision the other way).
    """
    v = float(val)
    if abs(v) >= 1e9:
        return f"${v / 1e9:,.1f}B"
    if abs(v) >= 100_000:
        return f"${v / 1e6:,.1f}M"
    return _fmt_dollar(v)


def _fmt_mult(val: float) -> str:
    return f"{float(val):.1f}×"


def _fmt_dscr(val: float) -> str:
    return f"{float(val):.2f}×"


def _fmt_irr(val: float) -> str:
    return f"{float(val) * 100:.1f}%"


def _esc(s: str) -> str:
    """HTML-escape plus the two entity substitutions the spec mandates:
    every literal $ becomes &#36; (two raw dollars in one st.markdown string
    trigger KaTeX italics — existing bug class), en-dash becomes &#8211;."""
    return _html.escape(s, quote=False).replace("$", "&#36;").replace("–", "&#8211;")


def _compose(*parts):
    """Build (plain_text, html) from a sequence of literals and numbers.

    Each part is either a plain string or a ("num", "...") tuple; numbers are
    wrapped in <span class="insight-num"> so they render in IBM Plex Mono
    (spec implementation note 4)."""
    plain, htm = [], []
    for p in parts:
        if isinstance(p, tuple):
            plain.append(p[1])
            htm.append(f'<span class="insight-num">{_esc(p[1])}</span>')
        else:
            plain.append(p)
            htm.append(_esc(p))
    return "".join(plain), "".join(htm)


def _num(s: str) -> tuple:
    return ("num", s)


def _mk(kind: str, severity: str, parts, data: Optional[dict] = None,
        sub: Optional[str] = None) -> Insight:
    plain, htm = _compose(*parts)
    d = dict(data or {})
    d["html"] = htm
    if sub:
        d["sub"] = sub
        d["sub_html"] = _esc(sub)
    return Insight(kind=kind, severity=severity, text=plain, data=d)


def irr_is_meaningful(v) -> bool:
    """True only for a finite IRR inside the plausible band (-99%, +1000%).

    THE shared gate for every surface that prints an IRR — the insight strip,
    the Key Return Metrics card, the memo digest, and the PDF. It exists as a
    public helper because clamping only the scorer left four renderers happily
    printing "5-Year IRR = 286746111107328000.0%" in green, right next to the
    red strip saying the number was meaningless. Any new IRR display must call
    this; if it doesn't, it will eventually quote Newton's garbage to a client.
    """
    if v is None:
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    if _math.isnan(f) or _math.isinf(f):
        return False
    return -0.99 < f < IRR_SANITY_CAP


def _f(x, default=None):
    """Coerce to float, returning default on None/NaN/garbage."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if _math.isnan(v) or _math.isinf(v):
        return default
    return v


# ------------------------------------------------------------------------
# Insight builders (one per kind; each returns Insight or None)
# ------------------------------------------------------------------------

def _insight_valuation(ctx: dict) -> Optional[Insight]:
    """Bid vs. the computed valuation band (spec 2.5 item 1)."""
    val = ctx.get("valuation")
    if not isinstance(val, dict):
        return None
    low = _f(val.get("low"))
    mid = _f(val.get("mid"))
    high = _f(val.get("high"))
    if low is None or mid is None or high is None or mid <= 0:
        return None  # band degenerate (e.g. adjusted EBITDA is zero)
    bid = _f(ctx.get("purchase_price"), 0.0) or 0.0
    data = {"low": low, "mid": mid, "high": high, "bid": bid}
    band_sub = f"band {_fmt_dollar(low)}–{_fmt_dollar(high)}"

    if bid <= 0:
        return _mk("valuation", "info",
                   ["Enter a bid to position it here."], data)
    if bid < DEEP_DISCOUNT_FRACTION * low:
        # A bid far under fair value is not a win — it's a question. Either the
        # seller won't transact, or diligence hasn't surfaced why they would.
        # Scoring this "positive" produced GO / zero flags on a 32%-under-band
        # bid, which reads as naive to anyone who has run a process.
        return _mk("valuation", "caution",
                   ["Bid sits ", _num(_fmt_band_delta(low - bid)),
                    " below the band — confirm seller expectations."],
                   data, sub=band_sub)
    if bid < low:
        return _mk("valuation", "positive",
                   ["Bid sits ", _num(_fmt_band_delta(low - bid)), " below the band."],
                   data)
    if bid > high:
        return _mk("valuation", "negative",
                   ["Bid exceeds the band by ", _num(_fmt_band_delta(bid - high)), "."],
                   data, sub=band_sub)
    if bid >= 0.95 * high:
        return _mk("valuation", "caution",
                   ["Bid sits at the top of the band."], data, sub=band_sub)
    # In band, below the 0.95*high caution line.
    if bid <= mid:
        pct = (mid - bid) / mid * 100.0
        return _mk("valuation", "positive",
                   ["Bid is in band, ", _num(f"{pct:.0f}%"), " under mid."], data)
    pct = (bid - mid) / mid * 100.0
    return _mk("valuation", "positive",
               ["Bid is in band, ", _num(f"{pct:.0f}%"), " over mid."], data)


def _insight_returns(ctx: dict) -> Optional[Insight]:
    """5Y IRR vs. the hedged sponsor-hurdle range (spec 2.5 item 2)."""
    ret = ctx.get("returns")
    if not isinstance(ret, dict) or "irr_yr5" not in ret:
        return None
    # The hedge sub is always shown — this is the mandated market-claim hedge.
    sub = "typical sponsor hurdles ~18–22%"
    raw = ret.get("irr_yr5")
    irr = _f(raw)
    data = {"irr_yr5": irr, "hurdle_low": HURDLE_LOW, "hurdle_high": HURDLE_HIGH}
    if irr is None:
        # None means no IRR was computed (no buyer equity at risk). A present
        # but non-finite value is a different animal: the solver diverged, so
        # say so rather than implying the deal simply wasn't levered.
        if raw is None:
            return _mk("returns", "info",
                       ["Returns undefined — no buyer equity invested."], data)
        return _mk("returns", "negative",
                   ["IRR not meaningful — cash flows never turn positive."],
                   data, sub=sub)
    # The engine's Newton solver returns garbage (|IRR| ~1e10+) when the cash
    # flows never change sign — i.e. the deal never pays back. Unguarded, that
    # rendered "IRR 1721053807567.8% clears typical sponsor hurdles" as a
    # POSITIVE insight and softened the verdict from RECONSIDER to CAUTION on
    # a deal that loses money every single year. Same clamp the stress scans
    # use (_irr_clears); a non-finite IRR is a red flag, never a green one.
    if not (-0.99 < irr < IRR_SANITY_CAP):
        return _mk("returns", "negative",
                   ["IRR not meaningful — cash flows never turn positive."],
                   data, sub=sub)
    if irr >= HURDLE_HIGH:
        sev, verb = "positive", " clears typical sponsor hurdles."
    elif irr >= HURDLE_LOW:
        sev, verb = "caution", " sits inside the hurdle range."
    else:
        sev, verb = "negative", " misses typical sponsor hurdles."
    return _mk("returns", sev, ["IRR ", _num(_fmt_irr(irr)), verb], data, sub=sub)


def _dscr_series(pf) -> list:
    """[(year, dscr), ...] for years with debt service; NaN years dropped.

    Prefers a precomputed 'dscr' column (app.py attaches one); otherwise
    derives EBITDA / (debt_service + seller_note_payment), matching
    calculations.compute_dscr semantics (zero-debt years are NaN, never 0)."""
    if "dscr" in pf.columns:
        raw = list(zip(pf["year"], pf["dscr"]))
    elif "ebitda" in pf.columns and "debt_service" in pf.columns:
        notes = pf["seller_note_payment"] if "seller_note_payment" in pf.columns else 0.0
        total_debt = pf["debt_service"] + notes
        vals = []
        for yr, e, d in zip(pf["year"], pf["ebitda"], total_debt):
            vals.append((yr, (e / d) if d else float("nan")))
        raw = vals
    else:
        return []
    out = []
    for yr, v in raw:
        fv = _f(v)
        if fv is not None:
            out.append((int(yr), fv))
    return out


def _insight_dscr(ctx: dict) -> Optional[Insight]:
    """Minimum DSCR across debt years vs. the ~1.25x floor (spec 2.5 item 3)."""
    pf = ctx.get("pro_forma")
    if pf is None or len(pf) == 0 or "year" not in getattr(pf, "columns", []):
        return None
    series = _dscr_series(pf)
    if not series:
        return _mk("dscr", "info",
                   ["No debt service — DSCR not applicable."], {})
    yr, v = min(series, key=lambda t: t[1])
    data = {"min_dscr": v, "year": yr}
    if v >= DSCR_COMFORT:
        return _mk("dscr", "positive",
                   [f"Y{yr} DSCR ", _num(_fmt_dscr(v)),
                    " — comfortable covenant headroom."], data)
    if v >= DSCR_COVENANT:
        return _mk("dscr", "caution",
                   [f"Y{yr} DSCR ", _num(_fmt_dscr(v)),
                    " — thin covenant cushion."], data)
    return _mk("dscr", "negative",
               [f"Y{yr} DSCR ", _num(_fmt_dscr(v)), " breaches ",
                _num(_fmt_dscr(DSCR_COVENANT)), " covenant."], data)


def _stress_irr5(pro_forma_kwargs: dict, irr_kwargs: dict, **overrides):
    """Re-run the real engine with overridden kwargs; return irr_yr5 or the
    full returns dict is not needed — breakeven variant below reuses this."""
    kw = dict(pro_forma_kwargs)
    kw.update(overrides)
    pf = build_pro_forma(**kw)
    return compute_irr_and_returns(pf, **irr_kwargs)


def _irr_clears(irr, hurdle: float) -> bool:
    """True only for a sane, finite IRR at/above the hurdle (see IRR_SANITY_CAP)."""
    v = _f(irr)
    return v is not None and -0.99 < v < IRR_SANITY_CAP and v >= hurdle


def _insight_synergy(ctx: dict) -> Optional[Insight]:
    """Share of modeled synergies needed to clear the hurdle (spec 2.5 item 4).

    Method per spec: rescale the schedule's cost_takeout + revenue_uplift by
    f in CAPTURE_GRID with capture_cost held at 100% (integration spend is
    committed even if benefits slip), re-run the pro forma + IRR, and report
    the smallest f whose 5Y IRR clears HURDLE_LOW."""
    if "synergy_schedule" not in ctx:
        return None
    sched = ctx.get("synergy_schedule")
    if sched is None:
        # Manual-override mode: the user disabled the ramped schedule.
        return _mk("synergy", "info",
                   ["Synergy schedule off — using manual inputs."], {})
    if len(sched) == 0:
        return None
    pk, ik = ctx.get("pro_forma_kwargs"), ctx.get("irr_kwargs")
    if not pk or not ik:
        return None  # cannot honestly re-run the engine — do not guess
    # Zero buyer equity means IRR (and thus needed capture) is undefined; the
    # returns insight already carries that message.
    invested = (_f(ik.get("purchase_price"), 0.0) or 0.0) \
        * (_f(ik.get("pct_upfront"), 0.0) or 0.0) \
        * (_f(ik.get("pct_self_funded"), 0.0) or 0.0)
    if invested <= 0:
        return None

    needed = None
    irr_by_f = {}
    for f in CAPTURE_GRID:
        scaled = sched.copy()
        scaled["cost_takeout"] = scaled["cost_takeout"] * f
        scaled["revenue_uplift"] = scaled["revenue_uplift"] * f
        # capture_cost intentionally NOT scaled (conservative, per spec).
        scaled["net_synergy"] = (scaled["cost_takeout"] + scaled["revenue_uplift"]
                                 - scaled["capture_cost"])
        scaled["cumulative_synergy"] = scaled["net_synergy"].cumsum()
        res = _stress_irr5(pk, ik, synergy_schedule=scaled)
        irr_by_f[f] = res.get("irr_yr5")
        if needed is None and _irr_clears(irr_by_f[f], HURDLE_LOW):
            needed = f
    data = {"needed_capture": needed, "irr_by_capture": irr_by_f}
    if needed == 0.0:
        return _mk("synergy", "positive",
                   ["Clears hurdle with zero synergy capture."], data)
    if needed is not None:
        pct = int(round(needed * 100 / 5.0)) * 5  # displayed to nearest 5%
        return _mk("synergy", "caution",
                   ["Needs ", _num(f"{pct}%"), " of modeled synergies to clear."],
                   data)
    return _mk("synergy", "negative",
               ["Misses hurdle even at full synergy capture."], data)


def _insight_comps(ctx: dict) -> Optional[Insight]:
    """Implied deal multiple vs. the comps band (spec 2.5 item 5)."""
    comps = ctx.get("comps")
    if not isinstance(comps, dict):
        return None
    n_used = comps.get("n_used", 0)
    n_confirmed = comps.get("n_confirmed")  # may be absent (parallel workstream)
    ev = comps.get("ev_ebitda")
    # Thin slice: no band at all, or fewer than 5 CONFIRMED deals when the
    # confirmed count is known — a band built on estimates is withheld rather
    # than shown (honest-empty-state principle, spec page 5).
    thin = ev is None or (n_confirmed is not None and n_confirmed < 5)
    if thin:
        n = n_confirmed if n_confirmed is not None else n_used
        data = {"n_used": n_used}
        if n_confirmed is not None:
            data["n_confirmed"] = n_confirmed
        return _mk("comps", "info",
                   ["Comps too thin here — no reliable band."], data,
                   sub=f"N={n} in this slice")

    val = ctx.get("valuation") or {}
    adj = _f(val.get("adj_ebitda"))
    bid = _f(ctx.get("purchase_price"), 0.0) or 0.0
    if adj is None or adj <= 0 or bid <= 0:
        return None  # implied multiple undefined without EBITDA and a bid
    p25, median, p75 = _f(ev.get("p25")), _f(ev.get("median")), _f(ev.get("p75"))
    if p25 is None or median is None or p75 is None:
        return None
    deal = bid / adj
    sub = (f"N={n_used}, widened tiers" if comps.get("fallback")
           else f"N={n_used} comps")
    data = {"low": p25, "mid": median, "high": p75, "bid": deal, "n_used": n_used}
    if n_confirmed is not None:
        data["n_confirmed"] = n_confirmed

    if deal < p25:
        return _mk("comps", "positive",
                   ["Deal prices ", _num(_fmt_mult(median - deal)),
                    " under the median comp."], data, sub=sub)
    if deal <= p75:
        return _mk("comps", "positive",
                   ["Deal prices in line with comps."], data, sub=sub)
    sev = "caution" if deal <= p75 + 1.0 else "negative"
    return _mk("comps", sev,
               ["Deal prices ", _num(_fmt_mult(deal - p75)),
                " above the comp range."], data, sub=sub)


def _insight_attrition(ctx: dict) -> Optional[Insight]:
    """Attrition headroom before breakeven slips (spec 2.5 item 6).

    Headroom D = largest integer 1..10 such that re-running the pro forma at
    attrition + D percentage points keeps breakeven <= max(base_breakeven, 5).
    The stress inherits the engine's attrition model (full rate Y1-2, decayed
    steady state Y3+) — no additional market claims are made here."""
    ret = ctx.get("returns")
    if not isinstance(ret, dict) or "breakeven_year" not in ret:
        return None
    be = ret.get("breakeven_year")
    if isinstance(be, str):
        if "n/a" in be:
            return _mk("attrition", "info",
                       ["Breakeven undefined — no buyer equity invested."], {})
        # Engine encodes never-recovered as the string "> 7": no headroom by
        # definition. The spec template's Y{n} slot has no number here, so we
        # render the beyond-horizon variant.
        return _mk("attrition", "negative",
                   ["No attrition headroom; breakeven beyond Y7."],
                   {"headroom": 0, "base_breakeven": be})
    base = _f(be)
    if base is None:
        return None
    pk, ik = ctx.get("pro_forma_kwargs"), ctx.get("irr_kwargs")
    if not pk or not ik:
        return None  # cannot honestly re-run the engine — do not guess
    base_attrition = _f(pk.get("attrition_rate"))
    if base_attrition is None:
        return None
    threshold = max(base, 5)
    headroom = 0
    for d in range(1, 11):
        res = _stress_irr5(pk, ik, attrition_rate=base_attrition + d / 100.0)
        be_d = res.get("breakeven_year")
        if isinstance(be_d, (int, float)) and float(be_d) <= threshold:
            headroom = d  # largest passing integer (spec-literal full scan)
    n = int(base)
    data = {"headroom": headroom, "base_breakeven": n}
    if headroom >= 3:
        return _mk("attrition", "positive",
                   ["Returns absorb ", _num(str(headroom)),
                    " extra points of attrition."], data)
    if headroom >= 1:
        return _mk("attrition", "caution",
                   ["Only ", _num(str(headroom)),
                    " points of attrition headroom."], data)
    return _mk("attrition", "negative",
               [f"No attrition headroom; breakeven already Y{n}."], data)


# ------------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------------

def compute_all_insights(ctx: dict) -> list:
    """Compute every producible insight for this deal state.

    Zero-state guard (spec 2.4): if inputs aum, revenue and ebitda are all 0
    the deal does not exist yet — return [] so nothing renders anywhere.

    Bid gate: with firm data but no bid, bid-dependent insights are still
    computed in their spec info states but flagged data["gated"]=True;
    select_insight() will not surface them (spec 2.4: the calculator renders
    bid-dependent insights only when purchase_price > 0)."""
    inputs = ctx.get("inputs") or {}
    aum = _f(inputs.get("aum"), 0.0) or 0.0
    revenue = _f(inputs.get("revenue"), 0.0) or 0.0
    ebitda = _f(inputs.get("ebitda"), 0.0) or 0.0
    if aum == 0 and revenue == 0 and ebitda == 0:
        return []

    bid = _f(ctx.get("purchase_price"), 0.0) or 0.0
    out = []
    for builder in (_insight_valuation, _insight_returns, _insight_dscr,
                    _insight_synergy, _insight_comps, _insight_attrition):
        ins = builder(ctx)
        if ins is not None:
            if bid <= 0 and ins.kind in _BID_DEPENDENT:
                ins.data["gated"] = True
            out.append(ins)
    return out


def _find(insights, kind: str, allow_gated: bool = False):
    for i in insights:
        if i.kind == kind and (allow_gated or not i.data.get("gated")):
            return i
    return None


def select_insight(insights: list, section: str) -> Optional[Insight]:
    """Pick the one insight a calculator section shows (spec 2.4 table).

    Sections: "valuation" (Tab 1), "financials" (Tab 2), "returns" (Tab 3),
    "integration" (Tab 4); a few readable aliases are accepted. Unknown
    section names raise ValueError so integration typos fail loudly.
    Returns None when nothing should render (zero-state, or every candidate
    is bid-gated)."""
    key = _SECTION_ALIASES.get(str(section).strip().lower())
    if key is None:
        raise ValueError(
            f"Unknown insight section {section!r}; expected one of "
            f"{sorted(set(_SECTION_ALIASES))}"
        )
    if not insights:
        return None

    if key == "valuation":
        v = _find(insights, "valuation")
        c = _find(insights, "comps")
        if v is not None and v.severity in ("caution", "negative"):
            return v
        # "Show comps if a comps band exists" — the info state means no band.
        if c is not None and c.severity != "info":
            return c
        return v
    if key == "financials":
        return _find(insights, "dscr")
    if key == "returns":
        r = _find(insights, "returns")
        a = _find(insights, "attrition")
        if r is not None and r.severity in ("caution", "negative"):
            return r
        if a is not None and a.severity in ("caution", "negative"):
            return a
        return r
    # integration
    return _find(insights, "synergy")


def verdict_from_insights(insights: list) -> tuple:
    """Single source of truth for the memo verdict (spec page 1).

    negatives >= 2 -> RECONSIDER; negatives == 1 or cautions >= 2 -> CAUTION;
    else GO. Info states count as nothing.

    Nothing scored -> INCOMPLETE, never GO. The old zero-state guard only fired
    when AUM, revenue AND EBITDA were all blank, so the ordinary post-SEC-apply
    state (AUM + revenue filled, EBITDA still 0) produced every insight as info
    or None and stamped a green "GO - 0 flags, 0 caution, 0 clear" on a deal
    nobody had underwritten yet. A verdict with no evidence behind it is the
    most dangerous thing this module can render."""
    counts = {"negative": 0, "caution": 0, "positive": 0}
    for i in insights:
        if i.severity in counts:
            counts[i.severity] += 1
    if sum(counts.values()) == 0:
        verdict = "INCOMPLETE"
    elif counts["negative"] >= 2:
        verdict = "RECONSIDER"
    elif counts["negative"] == 1 or counts["caution"] >= 2:
        verdict = "CAUTION"
    else:
        verdict = "GO"
    return verdict, counts


# ------------------------------------------------------------------------
# Render helpers (pure HTML strings; inject INSIGHT_CSS once, not per call)
# ------------------------------------------------------------------------

def _pct_str(x: float) -> str:
    v = round(x, 1)
    return f"{v:g}"


def _microviz_html(data: dict) -> str:
    """140px CSS number-line (spec 2.3). Positions are computed server-side
    into inline left/width percents, rounded to 1 decimal, clamped [0,100].
    Returns "" when the payload cannot draw an honest axis."""
    low, mid = _f(data.get("low")), _f(data.get("mid"))
    high, bid = _f(data.get("high")), _f(data.get("bid"))
    if low is None or mid is None or high is None or bid is None or bid <= 0:
        return ""
    axis_min = min(low, bid) * 0.92
    axis_max = max(high, bid) * 1.08
    span = axis_max - axis_min
    if span <= 0:
        return ""

    def pos(x: float) -> float:
        return round(min(100.0, max(0.0, (x - axis_min) / span * 100.0)), 1)

    band_left = pos(low)
    band_width = round(max(0.0, pos(high) - band_left), 1)
    return (
        '<div class="insight-viz" aria-hidden="true">'
        '<div class="insight-viz-track"></div>'
        f'<div class="insight-viz-band" style="left:{_pct_str(band_left)}%; '
        f'width:{_pct_str(band_width)}%;"></div>'
        f'<div class="insight-viz-tick" style="left:{_pct_str(pos(mid))}%;"></div>'
        f'<div class="insight-viz-marker" style="left:{_pct_str(pos(bid))}%;"></div>'
        "</div>"
    )


def render_insight(insight: Insight, with_microviz: bool = False) -> str:
    """HTML for one insight strip, for st.markdown(..., unsafe_allow_html=True).

    The micro-viz renders only for the valuation and comps kinds (spec 2.2)
    and only when a bid exists; requesting it elsewhere is a no-op."""
    sev = insight.severity if insight.severity in _SEVERITIES else "info"
    text_html = insight.data.get("html") or _esc(insight.text)
    sub_html = insight.data.get("sub_html")
    parts = [
        f'<div class="insight insight--{sev}">',
        '<span class="insight-dot"></span>',
        f'<span class="insight-text">{text_html}</span>',
    ]
    if sub_html:
        parts.append(f'<span class="insight-sub">{sub_html}</span>')
    if with_microviz and insight.kind in ("valuation", "comps"):
        parts.append(_microviz_html(insight.data))
    parts.append("</div>")
    return "".join(parts)


_VERDICT_CLASS = {"GO": "go", "CAUTION": "caution", "RECONSIDER": "reconsider",
                  "INCOMPLETE": "incomplete"}


def render_verdict_bar(verdict: str, counts: dict) -> str:
    """HTML for the memo page-1 verdict bar (.memo-verdict, spec page 1)."""
    v = str(verdict).upper()
    if v not in _VERDICT_CLASS:
        raise ValueError(
            f"Unknown verdict {verdict!r}; expected GO, CAUTION, RECONSIDER or INCOMPLETE")
    neg = int(counts.get("negative", 0))
    cau = int(counts.get("caution", 0))
    pos = int(counts.get("positive", 0))
    if v == "INCOMPLETE":
        # No checks scored — say what's missing instead of implying a judgement.
        counts_txt = "enter EBITDA and a bid to underwrite"
    else:
        # Name it as flag arithmetic, not advice: this line is what proves the
        # word on the left is a model screen rather than a recommendation.
        counts_txt = (f'{neg} flags &#183; {cau} caution &#183; {pos} clear '
                      f'&#183; {neg + cau + pos} of 6 checks scored')
    return (
        f'<div class="memo-verdict memo-verdict--{_VERDICT_CLASS[v]}">'
        '<span class="memo-verdict-dot"></span>'
        f'<span class="memo-verdict-word">{v}</span>'
        f'<span class="memo-verdict-counts">{counts_txt}</span>'
        "</div>"
    )


# ------------------------------------------------------------------------
# CSS (verbatim from the design-spec appendix, insight block only; the memo
# block ships with the memo module). Paste once into app.py _THEME_CSS —
# never emit per-render <style> tags (spec implementation note 2).
# ------------------------------------------------------------------------
INSIGHT_CSS = """
/* ============ INSIGHT COMPONENT ============ */
.insight{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  display:flex;align-items:center;gap:10px;
  background:#12151c;border:1px solid #1c2230;border-left:3px solid #6b7385;
  border-radius:6px;padding:10px 14px;margin:10px 0 2px 0;min-height:40px;
}
.insight--positive{border-left-color:#4ade80;}
.insight--caution{border-left-color:#fbbf24;}
.insight--negative{border-left-color:#f87171;}
.insight--info{border-left-color:#7c8cff;}
.insight-dot{width:6px;height:6px;border-radius:50%;flex:0 0 6px;background:#6b7385;}
.insight--positive .insight-dot{background:#4ade80;}
.insight--caution .insight-dot{background:#fbbf24;}
.insight--negative .insight-dot{background:#f87171;}
.insight--info .insight-dot{background:#7c8cff;}
.insight-text{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  color:#e5e9f0;font-size:0.86rem;font-weight:500;line-height:1.4;letter-spacing:-0.005em;
}
.insight-num{
  font-family:'IBM Plex Mono',ui-monospace,'SF Mono',Consolas,Menlo,monospace;
  font-weight:500;color:#ffffff;
  font-feature-settings:"tnum","lnum","zero";font-variant-numeric:tabular-nums lining-nums;
}
.insight-sub{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  color:#8b94a8;font-size:0.72rem;font-weight:400;white-space:nowrap;margin-left:2px;
}
.insight-viz{margin-left:auto;flex:0 0 140px;position:relative;height:16px;}
.insight-viz-track{position:absolute;top:6px;left:0;right:0;height:4px;background:#1c2230;border-radius:2px;}
.insight-viz-band{position:absolute;top:4px;height:8px;background:#2f3646;border-radius:2px;}
.insight-viz-tick{position:absolute;top:2px;width:1px;height:12px;background:#8b94a8;}
.insight-viz-marker{position:absolute;top:2px;width:3px;height:12px;border-radius:1.5px;transform:translateX(-1.5px);background:#6b7385;}
.insight--positive .insight-viz-marker{background:#4ade80;}
.insight--caution .insight-viz-marker{background:#fbbf24;}
.insight--negative .insight-viz-marker{background:#f87171;}
.insight--info .insight-viz-marker{background:#7c8cff;}
@media (max-width:640px){
  .insight{flex-wrap:wrap;}
  .insight-viz{flex-basis:100%;margin:6px 0 0 16px;}
  .insight-sub{white-space:normal;}
}
"""

__all__ = [
    "Insight", "compute_all_insights", "select_insight",
    "verdict_from_insights", "render_insight", "render_verdict_bar",
    "INSIGHT_CSS", "HURDLE_LOW", "HURDLE_HIGH", "DSCR_COVENANT",
]
