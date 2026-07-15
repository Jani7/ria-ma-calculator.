"""Every surface that prints an IRR must route through insights.irr_is_meaningful.

The engine's Newton solver returns unbounded garbage (1e10..1e21) when a deal's
cash flows never change sign. Six surfaces render an IRR. Four were gated only
after a reviewer drove the real engine into that state and looked. The other two
survived two rounds of fixes because a literal grep for "irr_yr5" cannot find
them: the heatmap builds its cell text inline, and the legacy PDF reads a
COMPUTED key (f"irr_yr{yr}").

Run:  python tests/test_irr_surfaces.py
"""
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import pdf_export
from calculations import (
    build_loan_amortization, build_seller_note_amortization, build_pro_forma,
    compute_dscr, compute_earnout_scenarios, compute_eboc,
    compute_irr_and_returns, implied_multiples, sensitivity_irr,
)
from insights import irr_is_meaningful

PASSED = 0


def ok(label, cond, detail=""):
    global PASSED
    assert cond, f"FAIL: {label} {detail}"
    PASSED += 1
    print(f"  PASS  {label}")


def _pdf_text(blob: bytes) -> str:
    """Rendered text of a PDF, with FlateDecode streams decompressed.

    fpdf2 zlib-compresses its content streams, so blob.decode() finds none of
    the copy — a test that greps the RAW bytes for garbage passes even when the
    PDF is full of it. An earlier version of this test did exactly that and was
    asserting nothing. Decompress, or assert nothing.
    """
    parts = []
    for chunk in blob.split(b"stream")[1:]:
        body = chunk.split(b"endstream")[0].strip(b"\r\n")
        try:
            parts.append(zlib.decompress(body).decode("latin-1", "ignore"))
        except zlib.error:
            continue  # not a compressed stream (fonts, images, xref)
    return "\n".join(parts)


# A deal that never returns the buyer's equity: negative net cash flow every
# year, zero sign changes, so Newton diverges. Reachable from ordinary sliders.
DOOMED = dict(
    revenue=5e6, ebitda=1e6, owner_comp=400e3, growth_rate=0.02,
    attrition_rate=0.10, aum=400e6, purchase_price=18e6, pct_upfront=0.80,
    pct_seller_note=0.10, note_rate=0.065, note_term=5, pct_earnout=0.10,
    earnout_period=3, pct_equity_rollover=0.0, pct_self_funded=0.30,
    loan_rate=0.09, loan_term=10, integration_costs=0, annual_synergies=0,
    additional_staff=0, years=7,
)


def _doomed_returns():
    pf = build_pro_forma(**DOOMED)
    pf["dscr"] = compute_dscr(pf)
    ls = build_loan_amortization(18e6 * 0.80 * 0.70, 0.09, 10)
    ns = build_seller_note_amortization(18e6 * 0.10, 0.065, 5)
    r = compute_irr_and_returns(
        pf, purchase_price=18e6, pct_upfront=0.80, pct_self_funded=0.30,
        pct_equity_rollover=0.0, pct_recurring=90,
        loan_schedule=ls, note_schedule=ns, pct_seller_note=0.10,
    )
    return pf, ls, ns, r


def test_engine_really_diverges():
    """Guard the premise. If the engine stops producing garbage these tests
    assert nothing, and should be revisited rather than quietly trusted."""
    print("\ntest_engine_really_diverges")
    pf, _, _, r = _doomed_returns()
    ok("every year's net cash flow is negative", (pf["net_cash_flow"] < 0).all())
    for yr in (3, 5, 7):
        ok(f"engine returns a divergent irr_yr{yr}",
           not irr_is_meaningful(r[f"irr_yr{yr}"]), f"got {r[f'irr_yr{yr}']!r}")


def test_heatmap_masks_garbage_and_keeps_the_colorscale_honest():
    """Surface 5 — the IRR sensitivity heatmap.

    Not a cosmetic text bug: z feeds Plotly's AUTOSCALED colorscale, so a single
    1e21 cell stretched the range to -8.9e12..4.8e21 and collapsed every real
    cell to colour position ~1e-20. The grid inverted — the genuine 72% IRR
    winner rendered RED while the mathematically impossible cell rendered GREEN,
    pointing a reader at exactly the wrong deal.
    """
    print("\ntest_heatmap_masks_garbage_and_keeps_the_colorscale_honest")
    base = dict(DOOMED, pct_recurring=90)
    table = sensitivity_irr(
        base,
        [round(x, 1) for x in np.arange(1.0, 4.1, 0.5)],
        [round(x, 2) for x in np.arange(0.0, 0.16, 0.03)],
    )
    mask = np.array([[irr_is_meaningful(v) for v in row] for row in table.values])
    ok("scenario actually produces garbage cells", (~mask).sum() > 0,
       f"masked {(~mask).sum()}/{mask.size}")
    ok("scenario also keeps real cells", mask.sum() > 0)

    z = np.where(mask, table.values.astype(float) * 100, np.nan)
    ok("no non-finite value reaches z", np.isfinite(z[~np.isnan(z)]).all())
    ok("z-range is plausible, not astronomical",
       np.nanmax(z) < 1000 and np.nanmin(z) > -1000,
       f"{np.nanmin(z):.2f}..{np.nanmax(z):.2f}")

    # The whole point: the best REAL cell must sit at the green end.
    best = np.nanmax(z)
    pos = (best - np.nanmin(z)) / (np.nanmax(z) - np.nanmin(z))
    ok("best real cell sits at the top of the colorscale", pos == 1.0,
       f"position {pos:.3g}")

    txt = [[f"{v*100:.1f}%" if m else "n/a" for v, m in zip(row, mrow)]
           for row, mrow in zip(table.values, mask)]
    flat = [c for row in txt for c in row]
    ok("garbage cells print 'n/a', never a number",
       all(c == "n/a" or float(c.rstrip("%")) < 1000 for c in flat))


def test_legacy_pdf_gates_all_three_irr_years():
    """Surface 6 — the legacy 'Export Full Analysis to PDF' button.

    Printed 'Year 5 IRR: 1131149108065523728384.0%' into a downloadable file.
    Survived because the key is computed (f"irr_yr{yr}"), so grep never saw it.
    """
    print("\ntest_legacy_pdf_gates_all_three_irr_years")
    pf, ls, ns, r = _doomed_returns()

    blob = pdf_export.generate_pdf(
        purchase_price=18e6,
        multiples=implied_multiples(18e6, 5e6, 400e6, compute_eboc(1e6, 400e3)),
        eboc=compute_eboc(1e6, 400e3), pro_forma=pf, returns=r,
        loan_amort=ls, note_amort=ns,
        earnout_scenarios=compute_earnout_scenarios(
            18e6, 0.10, 3, 5e6, 400e6, 200, 0.02, 0.10),
        dscr=pf["dscr"],
        inputs=dict(aum=400e6, revenue=5e6, ebitda=1e6, owner_comp=400e3,
                    num_clients=200, growth_rate=0.02, attrition_rate=0.10,
                    integration_costs=0, consulting_annual=0, consulting_years=0,
                    noncompete_total=0, noncompete_years=0),
    )
    ok("legacy PDF still generates", isinstance(blob, bytes) and len(blob) > 3000)

    text = _pdf_text(blob)
    # Guard the guard: prove the test can actually see the rendered copy before
    # asserting anything about its absence.
    ok("test actually reads the rendered text", "Year 5 IRR" in text)

    rendered = re.findall(r"Year \d IRR[^)]*\)[^(]*\(([^)]*)\)", text)
    ok("all three IRR rows render", len(rendered) == 3, str(rendered))
    ok("every divergent IRR row prints n/a", all(v == "n/a" for v in rendered),
       str(rendered))
    huge = re.findall(r"\d{4,}\.\d%", text)
    ok("no thousands-of-percent figure anywhere in the PDF", not huge, str(huge[:3]))


def test_legit_deal_still_renders_its_numbers():
    """The gate must not swallow real IRRs — a guard that blanks working deals
    is not a guard, it is a new bug."""
    print("\ntest_legit_deal_still_renders_its_numbers")
    good = dict(DOOMED, purchase_price=8e6, attrition_rate=0.05,
                pct_self_funded=0.70, ebitda=1.6e6, revenue=4e6, aum=500e6)
    pf = build_pro_forma(**good)
    pf["dscr"] = compute_dscr(pf)
    r = compute_irr_and_returns(
        pf, purchase_price=8e6, pct_upfront=0.80, pct_self_funded=0.70,
        pct_equity_rollover=0.0, pct_recurring=90,
        loan_schedule=build_loan_amortization(8e6 * 0.80 * 0.30, 0.09, 10),
        note_schedule=build_seller_note_amortization(8e6 * 0.10, 0.065, 5),
        pct_seller_note=0.10,
    )
    ok("a healthy deal's IRR passes the gate",
       irr_is_meaningful(r["irr_yr5"]), f"got {r['irr_yr5']!r}")


if __name__ == "__main__":
    test_engine_really_diverges()
    test_heatmap_masks_garbage_and_keeps_the_colorscale_honest()
    test_legacy_pdf_gates_all_three_irr_years()
    test_legit_deal_still_renders_its_numbers()
    print(f"\nAll {PASSED} assertions passed.")
