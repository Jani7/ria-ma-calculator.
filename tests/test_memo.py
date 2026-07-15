"""
Smoke tests for Deal Memo Mode (memo_mode.py + pdf_export.generate_memo_pdf).

Plain asserts, no pytest. Run:  python tests/test_memo.py

Scenario 1 (design-spec 5.10): AUM $500M, revenue $4.0M, EBITDA $1.6M,
owner comp $500K, 90% recurring, 5% growth, 5% attrition, bid $8.0M.
Real engine output feeds a real insights.py pass; the tests assert the
memo artifacts hold their contract (bytes, 5 pages, honest zero-state),
not exact insight values.
"""

import ast
import io
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# ------------------------------------------------------------------
# Prefer the real insights module (it ships alongside); fall back to a
# minimal contract-shaped stub only if it is somehow absent.
# ------------------------------------------------------------------
try:
    import insights  # noqa: F401
    USING_REAL_INSIGHTS = True
except ImportError:
    USING_REAL_INSIGHTS = False
    from dataclasses import dataclass, field

    stub = types.ModuleType("insights")

    @dataclass
    class Insight:
        kind: str
        severity: str
        text: str
        data: dict = field(default_factory=dict)

    def compute_all_insights(ctx):
        return [
            Insight("valuation", "positive", "Bid sits $3.7M below the band.",
                    {"low": 11.7e6, "mid": 13.7e6, "high": 15.8e6, "bid": 8e6}),
            Insight("returns", "positive", "IRR 55.7% clears typical sponsor hurdles.",
                    {"sub": "typical sponsor hurdles ~18-22%"}),
            Insight("dscr", "caution", "Y1 DSCR 1.30x — thin covenant cushion.",
                    {"min_dscr": 1.30, "year": 1}),
            Insight("synergy", "positive", "Clears hurdle with zero synergy capture.", {}),
            Insight("comps", "info", "Comps too thin here — no reliable band.",
                    {"sub": "N=2 in this slice"}),
            Insight("attrition", "positive", "Returns absorb 4 extra points of attrition.", {}),
        ]

    def verdict_from_insights(ins):
        counts = {"negative": 0, "caution": 0, "positive": 0}
        for i in ins:
            if i.severity in counts:
                counts[i.severity] += 1
        if counts["negative"] >= 2:
            return "RECONSIDER", counts
        if counts["negative"] == 1 or counts["caution"] >= 2:
            return "CAUTION", counts
        return "GO", counts

    def select_insight(ins, section):
        return ins[0] if ins else None

    def render_insight(i, with_microviz=False):
        return (f'<div class="insight insight--{i.severity}">'
                f'<span class="insight-dot"></span>'
                f'<span class="insight-text">{i.text}</span></div>')

    def render_verdict_bar(verdict, counts):
        return (f'<div class="memo-verdict memo-verdict--{str(verdict).lower()}">'
                f'<span class="memo-verdict-word">{verdict}</span></div>')

    stub.Insight = Insight
    stub.compute_all_insights = compute_all_insights
    stub.verdict_from_insights = verdict_from_insights
    stub.select_insight = select_insight
    stub.render_insight = render_insight
    stub.render_verdict_bar = render_verdict_bar
    stub.INSIGHT_CSS = ""
    sys.modules["insights"] = stub

import pandas as pd  # noqa: E402

from calculations import (  # noqa: E402
    auto_replacement_cost, compute_valuation_band, suggest_deal_structure,
    compute_synergy_schedule, build_pro_forma, compute_dscr,
    compute_irr_and_returns, build_loan_amortization,
    build_seller_note_amortization, lookup_comps_band,
)

import memo_mode  # noqa: E402
from memo_mode import build_memo_data, is_zero_state, MEMO_CSS  # noqa: E402
from pdf_export import generate_memo_pdf  # noqa: E402


def page_count(pdf_bytes):
    """Count PDF page objects: '/Type /Page' occurrences minus the one
    '/Type /Pages' tree node."""
    return pdf_bytes.count(b"/Type /Page") - pdf_bytes.count(b"/Type /Pages")


# ------------------------------------------------------------------
# 0. Both memo files must AST-parse.
# ------------------------------------------------------------------
for fname in ("memo_mode.py", "pdf_export.py"):
    with io.open(os.path.join(ROOT, fname), encoding="utf-8") as fh:
        ast.parse(fh.read())
print("PASS  AST parse: memo_mode.py, pdf_export.py")
print(f"      insights module: {'REAL' if USING_REAL_INSIGHTS else 'STUB'}")


# ------------------------------------------------------------------
# Scenario 1 ctx/extras from the real engine.
# ------------------------------------------------------------------
AUM, REVENUE, EBITDA, OWNER_COMP = 500e6, 4.0e6, 1.6e6, 500e3
PCT_RECURRING, GROWTH, ATTRITION = 90.0, 0.05, 0.05
# The bid sits INSIDE the computed band ($11.7M-$15.8M). This fixture is not
# just a test input — it renders tests/_memo_scenario1.pdf, the sample artifact
# prospects judge the product on. The old $8.0M bid produced a 4.7x purchase of
# a 90%-recurring, 40%-margin book at a 72.4% IRR and stamped GO on it; two
# reviewers independently said any banker reads that as naive within ten
# seconds. A market-plausible deal is a better demo than a fantasy one.
BID = 13.0e6
# Self-funded raised 30% -> 70%: at 30% the sample levered into a 0.97x Y1 DSCR
# (covenant breach) and a 38-50% IRR. This structure lands IRR ~27% with a 1.74x
# DSCR cushion — a deal a buyer could actually take to a committee.
UPFRONT, NOTE, EARNOUT, ROLLOVER, SELF_FUNDED = 0.75, 0.10, 0.10, 0.05, 0.70
LOAN_RATE, LOAN_TERM, NOTE_RATE, NOTE_TERM, EARNOUT_PERIOD = 0.085, 10, 0.065, 5, 3

replacement = auto_replacement_cost(AUM)
valuation = compute_valuation_band(
    revenue=REVENUE, ebitda=EBITDA, owner_comp=OWNER_COMP, aum=AUM,
    pct_recurring=PCT_RECURRING, growth_rate=GROWTH, replacement_cost=replacement,
)
suggested = suggest_deal_structure(aum=AUM, pct_recurring=PCT_RECURRING, growth_rate=GROWTH)
synergy = compute_synergy_schedule(
    target_revenue=REVENUE, target_expense_base=REVENUE - EBITDA,
    buyer_profile={"has_compliance_team": True, "has_cco_redundancy": True,
                   "has_tech_stack": True, "has_planning_shop": False,
                   "has_back_office": True},
    years=7,
)

pro_forma_kwargs = dict(
    revenue=REVENUE, ebitda=EBITDA, owner_comp=OWNER_COMP,
    growth_rate=GROWTH, attrition_rate=ATTRITION, aum=AUM,
    purchase_price=BID, pct_upfront=UPFRONT, pct_seller_note=NOTE,
    note_rate=NOTE_RATE, note_term=NOTE_TERM, pct_earnout=EARNOUT,
    earnout_period=EARNOUT_PERIOD, pct_equity_rollover=ROLLOVER,
    pct_self_funded=SELF_FUNDED, loan_rate=LOAN_RATE, loan_term=LOAN_TERM,
    integration_costs=50e3, annual_synergies=0.0, additional_staff=0,
    years=7, replacement_cost=replacement, synergy_schedule=synergy,
)
pro_forma = build_pro_forma(**pro_forma_kwargs)
pro_forma["dscr"] = compute_dscr(pro_forma)

debt_amount = BID * UPFRONT * (1 - SELF_FUNDED)
loan_sched = build_loan_amortization(debt_amount, LOAN_RATE, LOAN_TERM)
note_sched = build_seller_note_amortization(BID * NOTE, NOTE_RATE, NOTE_TERM)
irr_kwargs = dict(
    purchase_price=BID, pct_upfront=UPFRONT, pct_self_funded=SELF_FUNDED,
    pct_equity_rollover=ROLLOVER, pct_recurring=PCT_RECURRING,
    loan_schedule=loan_sched, note_schedule=note_sched, pct_seller_note=NOTE,
)
returns = compute_irr_and_returns(pro_forma, **irr_kwargs)

comps_csv = os.path.join(ROOT, "data", "ria_ma_comps.csv")
comps = None
comps_table_df = None
if os.path.exists(comps_csv):
    comps_df = pd.read_csv(comps_csv)
    comps = lookup_comps_band(AUM, PCT_RECURRING, comps_df)
    try:
        comps_table_df = lookup_comps_band(
            AUM, PCT_RECURRING, comps_df, include_estimated=True)["slice_df"]
    except TypeError:  # older signature without include_estimated
        comps_table_df = comps.get("slice_df")

inputs = dict(
    aum=AUM, revenue=REVENUE, ebitda=EBITDA, owner_comp=OWNER_COMP,
    pct_recurring=PCT_RECURRING, growth_rate=GROWTH, attrition_rate=ATTRITION,
    num_clients=250, integration_costs=50e3, annual_synergies=0.0,
    pct_upfront=UPFRONT, pct_seller_note=NOTE, pct_earnout=EARNOUT,
    pct_equity_rollover=ROLLOVER, pct_self_funded=SELF_FUNDED,
)

ctx = dict(
    purchase_price=BID, valuation=valuation, returns=returns,
    pro_forma=pro_forma, synergy_schedule=synergy, comps=comps,
    inputs=inputs, pro_forma_kwargs=pro_forma_kwargs, irr_kwargs=irr_kwargs,
)
extras = dict(
    firm_name="Sample Wealth Partners",
    suggested_deal=suggested,
    structure_actual=dict(upfront=UPFRONT, note=NOTE, earnout=EARNOUT,
                          rollover=ROLLOVER),
    comps_table_df=comps_table_df,
)


# ------------------------------------------------------------------
# 1. Digest sanity.
# ------------------------------------------------------------------
assert not is_zero_state(ctx)
d = build_memo_data(ctx, extras)
assert d["zero"] is False
assert d["verdict"] in ("go", "caution", "reconsider"), d["verdict"]
assert d["paragraph"].strip().endswith("."), d["paragraph"]
assert 3 <= len(d["bullets"]) <= 5, d["bullets"]
assert len(d["pf_rows"]) == 5
assert ".memo-page" in MEMO_CSS and ".memo-verdict" in MEMO_CSS
print(f"PASS  digest: verdict={d['verdict_word']} counts={d['counts']} "
      f"bullets={len(d['bullets'])}")
print(f"      paragraph: {d['paragraph']}")

# ------------------------------------------------------------------
# 2. Screen pages render, dollars fully entity-escaped (KaTeX guard).
# ------------------------------------------------------------------
pages = [fn(d) for fn in (memo_mode._page1_html, memo_mode._page2_html,
                          memo_mode._page3_html, memo_mode._page4_html,
                          memo_mode._page5_html)]
for i, html_page in enumerate(pages, start=1):
    assert html_page.startswith('<div class="memo-page">'), f"page {i} wrapper"
    assert f"Page {i} of 5" in html_page, f"page {i} footer"
    assert "$" not in html_page, f"page {i} carries a raw dollar sign"
print("PASS  screen: 5 .memo-page cards, footers present, no raw '$'")

# ------------------------------------------------------------------
# 3. PDF: bytes, size, exactly 5 pages, ASCII-only content streams input.
# ------------------------------------------------------------------
pdf_bytes = generate_memo_pdf(ctx, extras)
assert isinstance(pdf_bytes, bytes)
assert len(pdf_bytes) > 5 * 1024, f"PDF too small: {len(pdf_bytes)} bytes"
n_pages = page_count(pdf_bytes)
assert n_pages == 5, f"expected 5 pages, found {n_pages}"
print(f"PASS  PDF scenario 1: {len(pdf_bytes):,} bytes, {n_pages} pages")

out_path = os.path.join(HERE, "_memo_scenario1.pdf")
with open(out_path, "wb") as fh:
    fh.write(pdf_bytes)
print(f"      sample written to {out_path}")

# ------------------------------------------------------------------
# 4. Degraded inputs: comps None + manual integration mode -> still 5 pages.
# ------------------------------------------------------------------
ctx_manual = dict(ctx)
ctx_manual["comps"] = None
ctx_manual["synergy_schedule"] = None  # manual-override integration
extras_manual = dict(extras)
extras_manual["comps_table_df"] = None
pdf2 = generate_memo_pdf(ctx_manual, extras_manual)
assert page_count(pdf2) == 5
print(f"PASS  PDF manual mode + no comps: {len(pdf2):,} bytes, 5 pages")

# ------------------------------------------------------------------
# 5. Synthetic confirmed comps band -> band_ok path renders, 5 pages.
# ------------------------------------------------------------------
conf_df = pd.DataFrame([
    {"date": f"2025-0{i}-15", "buyer": f"Buyer {i}", "seller": f"Seller {i}",
     "seller_aum": 400e6 + i * 1e7, "ev_ebitda_multiple": 8.0 + i * 0.2,
     "is_estimated": False, "notes": ""}
    for i in range(1, 7)
])
ctx_band = dict(ctx)
ctx_band["comps"] = {
    "aum_tier": "200M-500M", "recurring_tier": "90%+", "n_primary": 6,
    "n_used": 6, "n_confirmed": 6, "n_estimated_excluded": 0,
    "fallback": False,
    "ev_ebitda": {"p25": 8.2, "median": 8.5, "p75": 9.0},
    "ev_revenue": None, "slice_df": conf_df,
}
extras_band = dict(extras)
extras_band["comps_table_df"] = None
d_band = build_memo_data(ctx_band, extras_band)
assert d_band["comps"]["band_ok"] is True
assert d_band["comps"]["n_confirmed"] == 6
pdf3 = generate_memo_pdf(ctx_band, extras_band)
assert page_count(pdf3) == 5
print(f"PASS  PDF confirmed-band path: {len(pdf3):,} bytes, 5 pages")

# ------------------------------------------------------------------
# 6. Zero-state: digest short-circuits; PDF raises ValueError (the tab
#    hides the download button, so a silent $0 memo must be impossible).
# ------------------------------------------------------------------
ctx_zero = dict(ctx)
ctx_zero["inputs"] = dict(aum=0, revenue=0, ebitda=0)
assert is_zero_state(ctx_zero)
assert build_memo_data(ctx_zero, extras) == {"zero": True}
try:
    generate_memo_pdf(ctx_zero, extras)
except ValueError:
    print("PASS  zero-state: build_memo_data -> {'zero': True}; "
          "generate_memo_pdf raises ValueError")
else:
    raise AssertionError("zero-state generate_memo_pdf did not raise")

print("\nALL MEMO TESTS PASSED")
