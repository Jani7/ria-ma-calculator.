"""Tests for insights.py — compute / select / verdict only (no Streamlit).

Run:  python tests/test_insights.py

Scenario fixtures follow the design-spec smoke checks (section 5.10):
  S1  bid $8.0M, band $11.7/13.7/15.8M, IRR 55.7%, min DSCR 1.30 (Y1)
      -> GO, counts 0 flags / 1 caution / 5 clear
  S2  bid $16.5M, IRR 11.2%, min DSCR 1.10 (Y2), breakeven Y6
      -> RECONSIDER with 5 negatives
The spec's returns/DSCR figures were produced under illustrative structure
assumptions, so those two feed the ctx directly (minimal fake DataFrames);
the valuation band and the synergy/attrition stress re-runs go through the
real calculations engine with app-default deal structure.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from calculations import (
    auto_replacement_cost, compute_valuation_band, compute_synergy_schedule,
    build_loan_amortization, build_seller_note_amortization,
)
from insights import (
    Insight, compute_all_insights, select_insight, verdict_from_insights,
    render_verdict_bar, _insight_returns, _insight_valuation,
    irr_is_meaningful,
)

PASSED = 0


def ok(label, cond, detail=""):
    global PASSED
    assert cond, f"FAIL: {label} {detail}"
    PASSED += 1
    print(f"  PASS  {label}")


def by_kind(insights):
    return {i.kind: i for i in insights}


# ---------------------------------------------------------------- fixtures

AUM, REVENUE, EBITDA, OWNER_COMP = 500e6, 4.0e6, 1.6e6, 500e3
PCT_RECURRING, GROWTH = 90.0, 0.05

COMPS = {  # spec page-5 reference band
    "ev_ebitda": {"p25": 8.2, "median": 8.5, "p75": 9.0},
    "n_used": 11, "n_primary": 11, "fallback": False,
}


def build_ctx(bid, attrition, self_funded, returns, pro_forma):
    """S1/S2 ctx: real band + real stress kwargs, spec numbers for the rest."""
    repl = auto_replacement_cost(AUM)
    valuation = compute_valuation_band(
        REVENUE, EBITDA, OWNER_COMP, AUM, PCT_RECURRING, GROWTH,
        replacement_cost=repl,
    )
    buyer_profile = {  # app-default buyer capabilities
        "has_compliance_team": True, "has_cco_redundancy": True,
        "has_tech_stack": True, "has_planning_shop": False,
        "has_back_office": True,
    }
    sched = compute_synergy_schedule(
        REVENUE, REVENUE - EBITDA, buyer_profile, 0.14, 0.07, 1.25, years=7,
    )
    pf_kwargs = dict(  # app-default deal structure (60/20/15/5 etc.)
        revenue=REVENUE, ebitda=EBITDA, owner_comp=OWNER_COMP,
        growth_rate=GROWTH, attrition_rate=attrition, aum=AUM,
        purchase_price=bid, pct_upfront=0.60, pct_seller_note=0.20,
        note_rate=0.05, note_term=5, pct_earnout=0.15, earnout_period=3,
        pct_equity_rollover=0.05, pct_self_funded=self_funded,
        loan_rate=0.065, loan_term=7, integration_costs=0, annual_synergies=0,
        additional_staff=1, years=7, replacement_cost=repl,
        synergy_schedule=sched,
    )
    irr_kwargs = dict(
        purchase_price=bid, pct_upfront=0.60, pct_self_funded=self_funded,
        pct_equity_rollover=0.05, pct_recurring=PCT_RECURRING,
        loan_schedule=build_loan_amortization(bid * 0.60 * (1 - self_funded), 0.065, 7),
        note_schedule=build_seller_note_amortization(bid * 0.20, 0.05, 5),
        pct_seller_note=0.20,
    )
    return {
        "purchase_price": bid,
        "valuation": valuation,
        "returns": returns,
        "pro_forma": pro_forma,
        "synergy_schedule": sched,
        "comps": dict(COMPS),
        "inputs": {
            "aum": AUM, "revenue": REVENUE, "ebitda": EBITDA,
            "pct_recurring": PCT_RECURRING, "attrition_rate": attrition,
            "growth_rate": GROWTH,
        },
        "pro_forma_kwargs": pf_kwargs,
        "irr_kwargs": irr_kwargs,
    }


# S1 pro forma: DSCR derived from ebitda / (debt + note) => exactly the spec
# figures 1.30 / 1.76 / 2.07 / 2.25 / 2.35 with min at Y1.
S1_PF = pd.DataFrame({
    "year": [1, 2, 3, 4, 5],
    "ebitda": [d * 1.0e6 for d in (1.30, 1.76, 2.07, 2.25, 2.35)],
    "debt_service": [0.7e6] * 5,
    "seller_note_payment": [0.3e6] * 5,
})

# S2 pro forma: precomputed dscr column path, min 1.10 at Y2 (spec render).
S2_PF = pd.DataFrame({
    "year": [1, 2, 3, 4, 5],
    "dscr": [1.32, 1.10, 1.18, 1.25, 1.31],
})

S1_RETURNS = {"irr_yr5": 0.557, "coc_yr5": 1.96, "breakeven_year": 4,
              "total_cash_invested": 2.4e6}
S2_RETURNS = {"irr_yr5": 0.112, "coc_yr5": 0.4, "breakeven_year": 6,
              "total_cash_invested": 2.475e6}


# ---------------------------------------------------------------- tests

def test_zero_state():
    print("zero-state guard")
    ctx = {"purchase_price": 0,
           "inputs": {"aum": 0, "revenue": 0, "ebitda": 0}}
    out = compute_all_insights(ctx)
    ok("compute_all_insights returns []", out == [])
    ok("select_insight returns None", select_insight(out, "valuation") is None)
    v, c = verdict_from_insights(out)
    # Nothing scored must never read as GO — a green verdict with no evidence
    # behind it is worse than no verdict. (Reproduced in the wild: post-SEC
    # apply with EBITDA still blank stamped "GO - 0 flags, 0 caution, 0 clear".)
    ok("empty verdict is INCOMPLETE, not GO",
       v == "INCOMPLETE" and c == {"negative": 0, "caution": 0, "positive": 0})
    bar = render_verdict_bar(v, c)
    ok("INCOMPLETE bar renders and says what's missing",
       "memo-verdict--incomplete" in bar and "enter EBITDA" in bar)


def test_scenario_1():
    print("Scenario 1 (bid $8.0M) — spec smoke check")
    ctx = build_ctx(8_000_000, 0.05, 0.50, S1_RETURNS, S1_PF)
    ins = compute_all_insights(ctx)
    k = by_kind(ins)
    ok("all six insights produced", len(ins) == 6, f"got {sorted(k)}")

    # The spec originally expected "positive" here. Banker review overruled it:
    # an $8.0M bid is 32% under the $11.7M band low, and scoring that a clean
    # win is the naive read — either the seller walks or diligence hasn't found
    # the reason they wouldn't. Below DEEP_DISCOUNT_FRACTION * low it's caution.
    ok("deep-discount valuation is caution, not positive",
       k["valuation"].severity == "caution")
    ok("valuation text",
       k["valuation"].text == "Bid sits $3.7M below the band — confirm seller expectations.",
       repr(k["valuation"].text))
    ok("valuation viz payload",
       round(k["valuation"].data["low"] / 1e6, 1) == 11.7
       and round(k["valuation"].data["high"] / 1e6, 1) == 15.8
       and k["valuation"].data["bid"] == 8_000_000)

    ok("returns positive", k["returns"].severity == "positive")
    ok("returns text", k["returns"].text == "IRR 55.7% clears typical sponsor hurdles.",
       repr(k["returns"].text))
    ok("returns hedge sub always present",
       k["returns"].data["sub"] == "typical sponsor hurdles ~18–22%")

    ok("dscr caution", k["dscr"].severity == "caution")
    ok("dscr text", k["dscr"].text == "Y1 DSCR 1.30× — thin covenant cushion.",
       repr(k["dscr"].text))

    ok("synergy positive (clears at f=0)", k["synergy"].severity == "positive")
    ok("synergy text", k["synergy"].text == "Clears hurdle with zero synergy capture.",
       repr(k["synergy"].text))

    ok("comps positive", k["comps"].severity == "positive")
    ok("comps text", k["comps"].text == "Deal prices 3.8× under the median comp.",
       repr(k["comps"].text))
    ok("comps sub", k["comps"].data["sub"] == "N=11 comps")

    ok("attrition positive (headroom >= 3)",
       k["attrition"].severity == "positive"
       and k["attrition"].data["headroom"] >= 3,
       f"headroom={k['attrition'].data.get('headroom')}")
    ok("attrition text", k["attrition"].text
       == f"Returns absorb {k['attrition'].data['headroom']} extra points of attrition.",
       repr(k["attrition"].text))

    v, c = verdict_from_insights(ins)
    # Spec said GO. Banker review moved it: two cautions (a 32%-under-band bid
    # and a thin Y1 covenant cushion) is exactly the "this looks too good --
    # go check it" signal that was missing when this scenario stamped a clean
    # GO on a 4.7x purchase of a 90%-recurring, 40%-margin book.
    ok("verdict CAUTION (deep discount + thin DSCR)", v == "CAUTION", v)
    ok("counts 0 flags / 2 caution / 4 clear",
       c == {"negative": 0, "caution": 2, "positive": 4}, c)

    # Priority table: a caution/negative valuation wins Tab 1. Under the old
    # scoring this bid was positive and the tab fell through to comps; now the
    # deep-discount caution takes the slot, which is the spec's intent ("bid
    # outside the band wins; else show comps position").
    ok("select valuation-tab -> valuation (caution wins the slot)",
       select_insight(ins, "valuation").kind == "valuation")
    ok("select financials -> dscr", select_insight(ins, "financials").kind == "dscr")
    ok("select returns -> returns", select_insight(ins, "returns").kind == "returns")
    ok("select integration -> synergy",
       select_insight(ins, "integration").kind == "synergy")
    return ins


def test_scenario_2():
    print("Scenario 2 (bid $16.5M, 25% self-funded, 10% attrition)")
    ctx = build_ctx(16_500_000, 0.10, 0.25, S2_RETURNS, S2_PF)
    ins = compute_all_insights(ctx)
    k = by_kind(ins)
    ok("all six insights produced", len(ins) == 6, f"got {sorted(k)}")

    ok("valuation negative", k["valuation"].severity == "negative")
    ok("valuation text", k["valuation"].text == "Bid exceeds the band by $0.7M.",
       repr(k["valuation"].text))
    ok("valuation sub on negative",
       k["valuation"].data["sub"] == "band $11.7M–$15.8M")

    ok("returns negative", k["returns"].severity == "negative")
    ok("returns text", k["returns"].text == "IRR 11.2% misses typical sponsor hurdles.",
       repr(k["returns"].text))

    ok("dscr negative", k["dscr"].severity == "negative")
    ok("dscr text", k["dscr"].text == "Y2 DSCR 1.10× breaches 1.25× covenant.",
       repr(k["dscr"].text))

    # Newton garbage guard: the f=0 / f=0.2 re-runs produce 1e10..1e17 "IRRs"
    # (no sign change in cash flows); they must not read as clearing.
    ok("synergy negative (misses at full capture)",
       k["synergy"].severity == "negative", k["synergy"].text)
    ok("synergy text", k["synergy"].text == "Misses hurdle even at full synergy capture.",
       repr(k["synergy"].text))

    ok("comps caution (0.7x over, within +1.0x)", k["comps"].severity == "caution")
    ok("comps text", k["comps"].text == "Deal prices 0.7× above the comp range.",
       repr(k["comps"].text))

    ok("attrition negative", k["attrition"].severity == "negative")
    ok("attrition text",
       k["attrition"].text == "No attrition headroom; breakeven already Y6.",
       repr(k["attrition"].text))

    v, c = verdict_from_insights(ins)
    ok("verdict RECONSIDER", v == "RECONSIDER", v)
    ok("counts 5 flags / 1 caution / 0 clear",
       c == {"negative": 5, "caution": 1, "positive": 0}, c)

    ok("select valuation-tab -> valuation (caution/negative wins)",
       select_insight(ins, "valuation").kind == "valuation")
    ok("select returns -> returns (negative wins over attrition)",
       select_insight(ins, "returns").kind == "returns")


def test_bid_gate():
    print("bid gate — firm data present, no bid entered")
    ctx = build_ctx(0, 0.05, 0.50,
                    {"irr_yr5": None, "coc_yr5": None,
                     "breakeven_year": "n/a — no equity invested"},
                    S1_PF)
    ins = compute_all_insights(ctx)
    k = by_kind(ins)
    ok("valuation info state produced",
       k["valuation"].severity == "info"
       and k["valuation"].text == "Enter a bid to position it here.")
    ok("bid-dependent insights flagged gated",
       all(i.data.get("gated") for i in ins if i.kind in
           ("valuation", "returns", "dscr", "attrition")))
    ok("no comps insight without a bid", "comps" not in k)
    ok("no synergy insight without equity", "synergy" not in k)
    for section in ("valuation", "financials", "returns", "integration"):
        ok(f"select {section} -> None (gated)",
           select_insight(ins, section) is None)


def test_info_states():
    print("info states")
    base_inputs = {"aum": AUM, "revenue": REVENUE, "ebitda": EBITDA,
                   "pct_recurring": PCT_RECURRING, "attrition_rate": 0.05,
                   "growth_rate": GROWTH}
    # Manual-override synergy mode: key present and None.
    ctx = {"purchase_price": 8_000_000, "inputs": base_inputs,
           "synergy_schedule": None}
    k = by_kind(compute_all_insights(ctx))
    ok("manual mode -> synergy info",
       k["synergy"].severity == "info"
       and k["synergy"].text == "Synergy schedule off — using manual inputs.")
    ok("select integration -> synergy info",
       select_insight(list(k.values()), "integration").kind == "synergy")

    # No debt anywhere -> DSCR info.
    pf = pd.DataFrame({"year": [1, 2, 3],
                       "ebitda": [1e6, 1.1e6, 1.2e6],
                       "debt_service": [0.0, 0.0, 0.0],
                       "seller_note_payment": [0.0, 0.0, 0.0]})
    ctx = {"purchase_price": 8_000_000, "inputs": base_inputs, "pro_forma": pf}
    k = by_kind(compute_all_insights(ctx))
    ok("no debt -> dscr info",
       k["dscr"].severity == "info"
       and k["dscr"].text == "No debt service — DSCR not applicable.")

    # IRR None with a bid (0% self-funded) -> returns info, selectable.
    ctx = {"purchase_price": 8_000_000, "inputs": base_inputs,
           "returns": {"irr_yr5": None, "breakeven_year": "n/a — no equity invested"}}
    ins = compute_all_insights(ctx)
    k = by_kind(ins)
    ok("irr None -> returns info",
       k["returns"].severity == "info"
       and k["returns"].text == "Returns undefined — no buyer equity invested.")
    ok("attrition info on undefined breakeven",
       k["attrition"].severity == "info"
       and k["attrition"].text == "Breakeven undefined — no buyer equity invested.")
    ok("select returns-tab -> returns info shown",
       select_insight(ins, "returns").kind == "returns")

    # Thin confirmed comps (new lookup shape: n_used=0, n_confirmed present).
    ctx = {"purchase_price": 8_000_000, "inputs": base_inputs,
           "valuation": {"low": 11.7e6, "mid": 13.7e6, "high": 15.8e6,
                         "adj_ebitda": 1.7e6},
           "comps": {"ev_ebitda": None, "n_used": 0, "n_confirmed": 2,
                     "n_estimated_excluded": 6, "fallback": False}}
    k = by_kind(compute_all_insights(ctx))
    ok("thin comps -> info variant",
       k["comps"].severity == "info"
       and k["comps"].text == "Comps too thin here — no reliable band.")
    ok("thin comps sub uses confirmed count",
       k["comps"].data["sub"] == "N=2 in this slice")
    ok("confirmed<5 forces info even when a band exists",
       by_kind(compute_all_insights({
           **ctx, "comps": {**COMPS, "n_confirmed": 3},
       }))["comps"].severity == "info")
    ok("select valuation-tab skips band-less comps -> valuation",
       select_insight(list(k.values()), "valuation").kind == "valuation")


def test_verdict_rules():
    print("verdict edge rules")
    def mk(*sevs):
        return [Insight("valuation", s, "t", {}) for s in sevs]
    ok("2 negatives -> RECONSIDER",
       verdict_from_insights(mk("negative", "negative"))[0] == "RECONSIDER")
    ok("1 negative -> CAUTION",
       verdict_from_insights(mk("negative", "positive"))[0] == "CAUTION")
    ok("2 cautions -> CAUTION",
       verdict_from_insights(mk("caution", "caution"))[0] == "CAUTION")
    ok("1 caution -> GO",
       verdict_from_insights(mk("caution", "positive"))[0] == "GO")
    ok("info counts as nothing",
       verdict_from_insights(mk("info", "info", "positive"))[0] == "GO")


def test_select_contract():
    print("select_insight contract")
    ins = [Insight("returns", "positive", "r", {}),
           Insight("attrition", "caution", "a", {})]
    ok("attrition caution surfaces when returns is clear",
       select_insight(ins, "returns").kind == "attrition")
    ok("section aliases accepted",
       select_insight(ins, "stress").kind == "attrition")
    try:
        select_insight(ins, "nonsense")
        raised = False
    except ValueError:
        raised = True
    ok("unknown section raises ValueError", raised)


def test_irr_garbage_guard():
    """A deal whose cash flows never turn positive must never read as clearing.

    The engine's Newton solver returns ~1e10+ on sign-change-less series.
    Unguarded, that rendered "IRR 1721053807567.8% clears typical sponsor
    hurdles" as a POSITIVE insight and softened the memo verdict from
    RECONSIDER to CAUTION on a deal that loses money every year."""
    print("\ntest_irr_garbage_guard")
    for junk in (1.7e10, 1e17, -5.0, float("inf")):
        ins = _insight_returns({"returns": {"irr_yr5": junk}})
        ok(f"IRR {junk:.3g} -> negative, not positive",
           ins is not None and ins.severity == "negative",
           f"got {ins.severity if ins else None}")
        ok(f"IRR {junk:.3g} copy refuses to quote the number",
           "not meaningful" in ins.text and "clears" not in ins.text,
           ins.text)
    # A sane IRR must still be scored normally.
    sane = _insight_returns({"returns": {"irr_yr5": 0.557}})
    ok("sane IRR 55.7% still clears", sane.severity == "positive")


def test_irr_is_meaningful_gate():
    """The shared gate every IRR *display* goes through.

    Clamping only the scorer left four renderers printing the garbage: the
    Key Return Metrics card showed a green "5-Year IRR = 286746111107328000.0%"
    right next to the red strip calling it meaningless. Any surface that prints
    an IRR must route through this.
    """
    print("\ntest_irr_is_meaningful_gate")
    for bad in (None, float("nan"), float("inf"), float("-inf"),
                1.7e10, 1e17, 3.14e17, 10.0, 10.5, -0.99, -5.0, "abc", "", [1]):
        ok(f"rejects {bad!r:.14}", irr_is_meaningful(bad) is False)
    for good in (0.0, 0.05, 0.222, 0.557, 9.99, -0.5):
        ok(f"accepts {good!r}", irr_is_meaningful(good) is True)


def test_deep_discount_skepticism():
    """A bid far under the band is a question, not a win (banker review)."""
    print("\ntest_deep_discount_skepticism")
    val = {"low": 11.7e6, "mid": 13.7e6, "high": 15.8e6}
    deep = _insight_valuation({"valuation": val, "purchase_price": 8.0e6})
    ok("32%-under-band bid scores caution, not positive",
       deep.severity == "caution", f"got {deep.severity}")
    ok("deep-discount copy asks about the seller",
       "seller expectations" in deep.text, deep.text)
    # Just below the band but not implausibly so -> still a clean positive.
    mild = _insight_valuation({"valuation": val, "purchase_price": 11.0e6})
    ok("mildly-under-band bid stays positive", mild.severity == "positive")


if __name__ == "__main__":
    test_zero_state()
    test_scenario_1()
    test_scenario_2()
    test_bid_gate()
    test_info_states()
    test_verdict_rules()
    test_select_contract()
    test_irr_garbage_guard()
    test_irr_is_meaningful_gate()
    test_deep_discount_skepticism()
    print(f"\nAll {PASSED} assertions passed.")
