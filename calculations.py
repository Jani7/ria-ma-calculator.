"""
RIA M&A Calculator — Core calculation engine.
All financial modeling logic: EBOC, pro forma P&L, IRR, amortization, sensitivity analysis.
"""

import numpy as np
import pandas as pd


MARKET_REPLACEMENT_COST = 200_000  # Default market replacement cost for owner
# Steady-state attrition decays to this fraction of the year-1 rate after the
# transition period. A $500M+ RIA with disciplined client outreach typically
# runs ~2-3% steady-state attrition vs. 5-15% during the buyer transition.
STEADY_STATE_ATTRITION_FRACTION = 0.4


def auto_replacement_cost(aum: float) -> int:
    """Default replacement cost for a departing owner, scaled by firm size.

    The hardcoded $200K constant was systematically too low: a $500M-AUM
    firm replaces the owner with a $400-600K loaded-cost senior advisor,
    not a $200K hire. Using $200K across the board inflated EBOC and
    depressed the EBOC multiple, making every default deal look cheaper
    than it actually was.
    """
    if aum < 200_000_000:
        return 200_000
    if aum < 1_000_000_000:
        return 400_000
    return 600_000


def compute_eboc(ebitda: float, owner_comp: float, replacement_cost: float = MARKET_REPLACEMENT_COST) -> float:
    """EBOC = EBITDA + (Owner's Comp - Market Replacement Cost)."""
    return ebitda + (owner_comp - replacement_cost)


def suggest_deal_structure(
    aum: float,
    pct_recurring: float,
    growth_rate: float,
    key_person_score: float = 1.00,
    owner_age: int = 60,
    owner_staying: bool = True,
) -> dict:
    """Recommend a deal-structure mix (upfront / note / earnout / rollover)
    based on the firm's profile. Calibrated to 2025-26 RIA M&A market
    bands per the banker-MD memo, Part C.

    Returns the recommended percentage splits, note rate, earnout period,
    earnout cap, standstill years, and a single-sentence rationale.
    """
    # Pick the closest deal profile from the memo's matrix.
    # Note: order matters — check key-person concentration first because it
    # dominates structure regardless of size.
    if key_person_score <= 0.92:
        profile = "key_person_concentrated"
        upfront, note, earnout, rollover = 0.40, 0.15, 0.35, 0.10
        rationale = ("High key-person concentration shifts consideration into "
                     "earnout to align the owner with retention.")
    elif aum < 200_000_000:
        profile = "small_tuck_in"
        upfront, note, earnout, rollover = 0.85, 0.10, 0.05, 0.00
        rationale = ("Sub-$200M tuck-in: sellers won't accept long earnouts on "
                     "a small base; structure leans mostly upfront with a "
                     "short note.")
    elif aum >= 1_000_000_000 and owner_staying:
        profile = "platform"
        upfront, note, earnout, rollover = 0.58, 0.10, 0.15, 0.17
        rationale = ("$1B+ platform with owner staying: meaningful equity "
                     "rollover for alignment, modest earnout on growth.")
    elif growth_rate >= 0.10:
        profile = "high_growth"
        upfront, note, earnout, rollover = 0.58, 0.12, 0.20, 0.10
        rationale = ("High organic growth (>10%): earnout captures the upside "
                     "the seller is delivering; rollover keeps them invested.")
    else:
        profile = "mid_market"
        upfront, note, earnout, rollover = 0.65, 0.15, 0.15, 0.05
        rationale = ("Mid-market $200M-$1B target: balanced structure with "
                     "moderate earnout for performance assurance.")

    # Owner-age tilt: 65+ pushes more upfront (retiring soon), less rollover.
    if owner_age >= 65:
        # Shift 5% from rollover/earnout to upfront if available.
        shift = min(0.05, rollover + earnout * 0.5)
        if rollover >= shift:
            rollover -= shift
        else:
            shift -= rollover
            rollover = 0
            earnout -= shift
        upfront += shift if (upfront + shift) <= 0.95 else 0.95 - upfront

    # Highly-recurring revenue → smaller earnout (less to bet on).
    if pct_recurring >= 95 and earnout > 0.05:
        earnout_cut = min(0.05, earnout - 0.05)
        earnout -= earnout_cut
        upfront += earnout_cut

    # Normalize to 100% in case the heuristics overshot.
    total = upfront + note + earnout + rollover
    upfront, note, earnout, rollover = (x / total for x in (upfront, note, earnout, rollover))

    # Earnout cap: 125% standard, narrower if recurring is high (less upside).
    earnout_cap_pct = 125 if pct_recurring < 95 else 115
    # Earnout period: 3 years is market standard.
    earnout_period = 3

    # Note rate: SOFR + 250-400 bps on 2025-26 deals. SOFR currently ~4.3%
    # so prime rate is roughly 6.5-8.0%. Default to 6.5% with cap at 8.5%.
    note_rate = 0.065
    note_term = 5

    # Standstill: bank-financed deals require 1-2 years; self-funded don't.
    # The caller will set this based on pct_self_funded; we recommend 1.
    standstill_years = 1

    return {
        "profile": profile,
        "upfront": upfront,
        "note": note,
        "earnout": earnout,
        "rollover": rollover,
        "note_rate": note_rate,
        "note_term": note_term,
        "earnout_period": earnout_period,
        "earnout_cap_pct": earnout_cap_pct,
        "standstill_years": standstill_years,
        "rationale": rationale,
    }


def compute_valuation_band(
    revenue: float,
    ebitda: float,
    owner_comp: float,
    aum: float,
    pct_recurring: float,
    growth_rate: float,
    replacement_cost: float = None,
    # Qualitative scores — default to neutral 1.00 when the UI doesn't
    # collect them yet. Each is bounded by the banker-memo ranges below.
    book_quality_score: float = 1.00,   # 0.90 (high concentration / aged) → 1.05 (clean)
    geography_score: float = 1.00,      # 0.95 (rural) → 1.05 (wealth-belt)
    key_person_score: float = 1.00,     # 0.80 (50%+ owner-dependent) → 1.05 (institutionalized)
) -> dict:
    """Multi-factor EBITDA-multiple valuation band.

    Replaces the user-enters-price-then-computes-multiples workflow with
    a defensible band the buyer can stress-test. Base multiple is the
    2025-26 market median for $250M-$1B AUM RIA transactions (Echelon
    Q1 2026 deal book ≈ 8.1× adjusted EBITDA — we anchor slightly lower
    at 7.0× and let the multipliers earn their way up).

    Returns:
        dict with low / mid / high dollar values, the implied EBITDA
        multiple, the adjusted EBITDA used, and each factor's contribution
        so the UI can show "here's why we got this number."
    """
    if replacement_cost is None:
        replacement_cost = auto_replacement_cost(aum)
    adj_ebitda = max(0.0, ebitda + (owner_comp - replacement_cost))

    def _recurring_factor(p):
        # 50% → 0.85, 80% → 1.00, 95%+ → 1.15. Smooth piecewise.
        p = max(50.0, min(100.0, p))
        if p <= 80.0:
            return 0.85 + (p - 50.0) / 30.0 * (1.00 - 0.85)
        return 1.00 + (p - 80.0) / 15.0 * (1.15 - 1.00)

    def _size_factor(a):
        # <$200M → 0.85, $500M → 1.00, $1B → 1.10, $2B+ → 1.20.
        if a < 200_000_000: return 0.85
        if a < 500_000_000: return 0.85 + (a - 200_000_000) / 300_000_000 * (1.00 - 0.85)
        if a < 1_000_000_000: return 1.00 + (a - 500_000_000) / 500_000_000 * (1.10 - 1.00)
        if a < 2_000_000_000: return 1.10 + (a - 1_000_000_000) / 1_000_000_000 * (1.20 - 1.10)
        return 1.20

    def _growth_factor(g):
        # <3% → 0.90, 5-7% → 1.00, 10%+ → 1.15. Negative growth → 0.85.
        if g < 0: return 0.85
        if g < 0.03: return 0.90
        if g < 0.05: return 0.90 + (g - 0.03) / 0.02 * (1.00 - 0.90)
        if g < 0.07: return 1.00
        if g < 0.10: return 1.00 + (g - 0.07) / 0.03 * (1.15 - 1.00)
        return 1.15

    def _margin_factor(rev, e):
        if rev <= 0: return 1.00
        margin = e / rev
        if margin < 0.25: return 0.95
        if margin < 0.30: return 0.95 + (margin - 0.25) / 0.05 * (1.00 - 0.95)
        if margin < 0.35: return 1.00 + (margin - 0.30) / 0.05 * (1.05 - 1.00)
        return 1.05

    factors = {
        "recurring": _recurring_factor(pct_recurring),
        "size": _size_factor(aum),
        "growth": _growth_factor(growth_rate),
        "margin": _margin_factor(revenue, ebitda),
        "book_quality": book_quality_score,
        "geography": geography_score,
        "key_person": key_person_score,
    }

    base_multiple = 7.0
    multiplier = 1.0
    for v in factors.values():
        multiplier *= v
    final_multiple = base_multiple * multiplier

    mid = adj_ebitda * final_multiple
    # Tighten the ±15% band when the profile is clean (high recurring,
    # institutionalized, healthy margin) and widen for risky deals.
    band_width = 0.15
    if pct_recurring < 70 or factors["key_person"] < 0.95 or aum < 200_000_000:
        band_width = 0.20

    return {
        "adj_ebitda": adj_ebitda,
        "base_multiple": base_multiple,
        "final_multiple": final_multiple,
        "multiplier": multiplier,
        "factors": factors,
        "low": mid * (1 - band_width),
        "mid": mid,
        "high": mid * (1 + band_width),
        "band_width": band_width,
        "replacement_cost": replacement_cost,
    }


def _exit_multiple_from_recurring(pct_recurring: float) -> float:
    """Map the % Recurring Revenue input (50-100) to a terminal-value EBITDA
    multiple. RIA M&A precedent: higher fee-based recurring revenue earns a
    higher exit multiple. 50% recurring → ~4.0×; 100% → ~6.0×."""
    if pct_recurring is None:
        return 6.0
    # Clamp to the slider range, then linearly interpolate 4× → 6×.
    p = max(50.0, min(100.0, float(pct_recurring)))
    return 4.0 + 2.0 * (p - 50.0) / 50.0


def implied_multiples(purchase_price: float, revenue: float, aum: float, eboc: float) -> dict:
    """Calculate implied purchase multiples."""
    return {
        "revenue_multiple": purchase_price / revenue if revenue else 0,
        "aum_multiple": (purchase_price / aum * 100) if aum else 0,  # as % of AUM
        "eboc_multiple": purchase_price / eboc if eboc else 0,
    }


def build_pro_forma(
    revenue: float,
    ebitda: float,
    owner_comp: float,
    growth_rate: float,
    attrition_rate: float,
    aum: float,
    purchase_price: float,
    pct_upfront: float,
    pct_seller_note: float,
    note_rate: float,
    note_term: int,
    pct_earnout: float,
    earnout_period: int,
    pct_equity_rollover: float,
    pct_self_funded: float,
    loan_rate: float,
    loan_term: int,
    integration_costs: float,
    annual_synergies: float,
    additional_staff: int,
    staff_cost_per_head: float = 85_000,
    years: int = 7,
    # Loan customization
    loan_io_years: int = 0,
    loan_balloon: bool = False,
    loan_amort_years: int = 0,
    # Seller note customization
    note_standstill_years: int = 0,
    note_io_during_standstill: bool = False,
    # Transition compensation (buyer expense)
    consulting_annual: float = 0,
    consulting_years: int = 0,
    noncompete_total: float = 0,
    noncompete_years: int = 0,
    # Tax
    tax_rate: float = 0,
    # Earnout expectation for buyer cash-flow purposes. 1.0 = full earnout
    # paid (target met), 0.0 = nothing paid. This is the base-case figure
    # used for IRR; the Earnout tab models full upside/downside scenarios.
    earnout_achievement: float = 1.0,
    # Market-rate replacement comp for the departing owner. Defaults to the
    # tool's $200K constant but should be overridden for senior CIO seats
    # at $500M+ AUM firms (typically $400-700K).
    replacement_cost: float = MARKET_REPLACEMENT_COST,
) -> pd.DataFrame:
    """Build a year-by-year pro forma P&L for years 1-N."""
    rows = []
    # Expense base = Revenue - EBITDA, then replace owner comp with market rate
    base_expenses = revenue - ebitda - owner_comp + replacement_cost
    additional_staff_cost = additional_staff * staff_cost_per_head

    # --- Debt service schedule (year-by-year) ---
    debt_amount = purchase_price * pct_upfront * (1 - pct_self_funded)
    loan_schedule = build_loan_amortization(
        debt_amount, loan_rate, loan_term,
        io_years=loan_io_years, balloon=loan_balloon,
        amort_years=loan_amort_years,
    )

    # --- Seller note schedule (year-by-year) ---
    seller_note_amount = purchase_price * pct_seller_note
    note_schedule = build_seller_note_amortization(
        seller_note_amount, note_rate, note_term,
        standstill_years=note_standstill_years,
        io_during_standstill=note_io_during_standstill,
    )

    # --- Transition comp schedule ---
    annual_noncompete = noncompete_total / noncompete_years if noncompete_years > 0 else 0

    # --- Earnout payment schedule (buyer outflow) ---
    # Previously earnout dollars were tracked only as seller proceeds; they
    # never entered net_cash_flow. That made every IRR for an earnout-heavy
    # deal materially overstated. We now subtract the base-case earnout
    # payment from buyer cash flow over the earnout period.
    total_earnout = purchase_price * pct_earnout * earnout_achievement
    annual_earnout_payment = (
        total_earnout / earnout_period if earnout_period > 0 else 0
    )

    for yr in range(1, years + 1):
        # Revenue with growth and attrition. Attrition was previously clamped
        # to years 1-2 only, which assumed 100% retention from Y3 onward — a
        # red-team-flagged inflation of pro forma revenue. We now decay
        # attrition to a steady-state fraction of the year-1 rate, modeling
        # real ongoing client churn rather than zeroing it out.
        if yr <= 2:
            yr_attrition = attrition_rate
        else:
            yr_attrition = attrition_rate * STEADY_STATE_ATTRITION_FRACTION
        effective_rate = growth_rate - yr_attrition

        if yr == 1:
            yr_revenue = revenue * (1 + effective_rate)
        else:
            yr_revenue = rows[-1]["revenue"] * (1 + effective_rate)

        # AUM tracking
        if yr == 1:
            yr_aum = aum * (1 + effective_rate)
        else:
            yr_aum = rows[-1]["aum"] * (1 + effective_rate)

        # Expenses grow at a slower pace (assume 2% cost inflation)
        cost_inflation = 0.02
        yr_expenses = base_expenses * ((1 + cost_inflation) ** yr) + additional_staff_cost
        yr_expenses -= annual_synergies  # subtract synergies

        # Transition compensation costs
        yr_consulting = consulting_annual if yr <= consulting_years else 0
        yr_noncompete = annual_noncompete if yr <= noncompete_years else 0
        yr_transition = yr_consulting + yr_noncompete

        # One-time integration costs in year 1
        yr_integration = integration_costs if yr == 1 else 0

        yr_ebitda = yr_revenue - yr_expenses - yr_integration - yr_transition

        # Debt service from schedule
        loan_row = loan_schedule[loan_schedule["year"] == yr]
        yr_debt = loan_row["payment"].iloc[0] if len(loan_row) > 0 else 0
        yr_debt_interest = loan_row["interest"].iloc[0] if len(loan_row) > 0 else 0
        yr_debt_balloon = loan_row["balloon_payment"].iloc[0] if (len(loan_row) > 0 and "balloon_payment" in loan_row.columns) else 0

        # Seller note from schedule
        note_row = note_schedule[note_schedule["year"] == yr]
        yr_note = note_row["payment"].iloc[0] if len(note_row) > 0 else 0

        total_debt_service = yr_debt + yr_debt_balloon + yr_note

        # Earnout payment in this year (buyer outflow)
        yr_earnout_payment = annual_earnout_payment if yr <= earnout_period else 0

        yr_pretax_cf = yr_ebitda - total_debt_service - yr_earnout_payment

        # Tax benefit from interest deduction
        yr_tax_benefit = 0
        if tax_rate > 0:
            note_interest = note_row["interest"].iloc[0] if len(note_row) > 0 else 0
            deductible_interest = yr_debt_interest + note_interest
            yr_tax_benefit = deductible_interest * tax_rate

        yr_net_cf = yr_pretax_cf + yr_tax_benefit

        rows.append({
            "year": yr,
            "revenue": yr_revenue,
            "aum": yr_aum,
            "expenses": yr_expenses,
            "transition_comp": yr_transition,
            "integration_costs": yr_integration,
            "ebitda": yr_ebitda,
            "debt_service": yr_debt + yr_debt_balloon,
            "seller_note_payment": yr_note,
            "earnout_payment": yr_earnout_payment,
            "tax_benefit": yr_tax_benefit,
            "pretax_cash_flow": yr_pretax_cf,
            "net_cash_flow": yr_net_cf,
        })

    return pd.DataFrame(rows)


def compute_irr_and_returns(
    pro_forma: pd.DataFrame,
    purchase_price: float,
    pct_upfront: float,
    pct_self_funded: float,
    pct_equity_rollover: float,
    pct_recurring: float = 100.0,
    loan_schedule: pd.DataFrame = None,
    note_schedule: pd.DataFrame = None,
    pct_seller_note: float = 0.0,
) -> dict:
    """Compute IRR and cash-on-cash returns at years 3, 5, 7.

    Returns None for IRR/CoC fields when the buyer's invested cash is zero
    or negative (100% leverage, 0% upfront, etc.) so the UI can show 'n/a'
    rather than a meaningless thousands-of-percent figure that came out of
    dividing by a placeholder $1.

    `pct_recurring` (50-100) controls the exit-multiple assumption: 50% →
    4× EBITDA, 100% → 6× EBITDA. Reflects RIA M&A precedent that fee-based
    recurring revenue earns a higher exit multiple than transactional.

    `loan_schedule` and `note_schedule` are passed in so the terminal value
    at exit can deduct remaining debt + remaining seller-note principal —
    without this, a 60%-leverage Y5 exit shows IRR as if all the debt
    magically disappeared at closing. Banker flagged this as a material
    overstatement of levered IRR, not just incomplete.
    """
    total_cash_invested = purchase_price * pct_upfront * pct_self_funded
    exit_multiple = _exit_multiple_from_recurring(pct_recurring)

    results: dict = {
        "total_cash_invested": total_cash_invested,
        "exit_multiple": exit_multiple,
    }

    if total_cash_invested <= 0:
        # No buyer equity at risk — IRR / CoC / breakeven are undefined.
        # We return None so the UI can render 'n/a' explicitly.
        for horizon in [3, 5, 7]:
            results[f"irr_yr{horizon}"] = None
            results[f"coc_yr{horizon}"] = None
        results["breakeven_year"] = "n/a — no equity invested"
        return results

    # Cash flows for IRR: initial outlay + annual net cash flows
    cf = [-total_cash_invested] + pro_forma["net_cash_flow"].tolist()

    def _remaining_principal(schedule, year):
        """End-of-year remaining balance from an amortization schedule."""
        if schedule is None or len(schedule) == 0:
            return 0.0
        row = schedule[schedule["year"] == year]
        if len(row) == 0:
            # Past schedule horizon — fully paid down.
            return 0.0
        return float(row["end_balance"].iloc[0])

    for horizon in [3, 5, 7]:
        subset = cf[: horizon + 1]
        # Add terminal value at exit (last year EBITDA × recurring-adjusted
        # multiple), net of remaining debt and seller-note balance at exit.
        if horizon <= len(pro_forma):
            ebitda_at_exit = pro_forma.iloc[min(horizon - 1, len(pro_forma) - 1)]["ebitda"]
            gross_terminal = ebitda_at_exit * exit_multiple
            debt_at_exit = (
                _remaining_principal(loan_schedule, horizon)
                + _remaining_principal(note_schedule, horizon)
            )
            terminal = gross_terminal - debt_at_exit
            subset_with_tv = subset.copy()
            subset_with_tv[-1] += terminal
        else:
            subset_with_tv = subset

        irr = _compute_irr(subset_with_tv)
        cumulative_cf = sum(subset[1:])
        coc = cumulative_cf / total_cash_invested

        results[f"irr_yr{horizon}"] = irr
        results[f"coc_yr{horizon}"] = coc

    # Breakeven analysis
    cumulative = 0
    breakeven_year = None
    for i, row in pro_forma.iterrows():
        cumulative += row["net_cash_flow"]
        if cumulative >= total_cash_invested:
            breakeven_year = row["year"]
            break
    results["breakeven_year"] = breakeven_year if breakeven_year else "> 7"

    return results


def build_loan_amortization(
    principal: float,
    rate: float,
    term: int,
    io_years: int = 0,
    balloon: bool = False,
    amort_years: int = 0,
) -> pd.DataFrame:
    """
    Full loan amortization schedule with optional:
    - io_years: interest-only period at start
    - balloon: if True, amortize over amort_years but balloon at term end
    """
    if principal <= 0 or term <= 0:
        return pd.DataFrame(columns=[
            "year", "beg_balance", "payment", "interest",
            "principal_paid", "end_balance", "balloon_payment",
        ])

    # Determine amortization schedule
    if balloon and amort_years > term:
        # Payments based on longer amort schedule, balloon remainder at term
        amort_payment = _pmt(rate, amort_years, principal)
    else:
        remaining_term = term - io_years
        amort_payment = _pmt(rate, max(remaining_term, 1), principal) if remaining_term > 0 else 0

    rows = []
    balance = principal

    for yr in range(1, term + 1):
        interest = balance * rate
        balloon_pmt = 0

        if yr <= io_years:
            # Interest-only period
            payment = interest
            princ_paid = 0
        else:
            payment = amort_payment
            princ_paid = payment - interest

        end_bal = balance - princ_paid

        # Balloon payment in final year
        if yr == term and balloon and end_bal > 0:
            balloon_pmt = end_bal
            end_bal = 0

        rows.append({
            "year": yr,
            "beg_balance": balance,
            "payment": payment,
            "interest": interest,
            "principal_paid": princ_paid,
            "end_balance": max(end_bal, 0),
            "balloon_payment": balloon_pmt,
        })
        balance = max(end_bal, 0)

    return pd.DataFrame(rows)


def build_seller_note_amortization(
    principal: float,
    rate: float,
    term: int,
    standstill_years: int = 0,
    io_during_standstill: bool = False,
) -> pd.DataFrame:
    """
    Seller note amortization with optional:
    - standstill_years: deferred start (no principal payments)
    - io_during_standstill: if True, pay interest during standstill; if False, accrue it
    """
    if principal <= 0 or term <= 0:
        return pd.DataFrame(columns=[
            "year", "beg_balance", "payment", "interest",
            "principal_paid", "end_balance", "balloon_payment",
        ])

    total_years = standstill_years + term
    balance = principal
    rows = []

    # Amortizing payment for the active term
    amort_payment = _pmt(rate, term, principal) if term > 0 else 0

    for yr in range(1, total_years + 1):
        interest = balance * rate
        balloon_pmt = 0

        if yr <= standstill_years:
            if io_during_standstill:
                # Pay interest only during standstill
                payment = interest
                princ_paid = 0
            else:
                # Accrue interest (PIK — added to balance)
                payment = 0
                princ_paid = 0
                balance += interest
                interest = 0  # Not a cash payment
        else:
            # Active amortization period
            if yr == standstill_years + 1 and not io_during_standstill and standstill_years > 0:
                # Recalculate payment based on accrued balance
                amort_payment = _pmt(rate, term, balance)

            payment = amort_payment
            princ_paid = payment - (balance * rate)
            interest = balance * rate

        end_bal = balance - princ_paid

        rows.append({
            "year": yr,
            "beg_balance": balance,
            "payment": payment,
            "interest": interest,
            "principal_paid": princ_paid,
            "end_balance": max(end_bal, 0),
            "balloon_payment": balloon_pmt,
        })
        balance = max(end_bal, 0)

    return pd.DataFrame(rows)


def compute_dscr(pro_forma: pd.DataFrame) -> pd.Series:
    """Debt Service Coverage Ratio = EBITDA / (Debt Service + Seller Note Payment).

    Returns NaN — not 0.0 — for years with zero debt service. A credit
    reviewer reads 0.00× as a covenant breach; leaving the value NaN lets
    the UI render it as 'n/a' instead, which is the correct semantics for
    'this period has no debt to cover.'
    """
    total_debt = pro_forma["debt_service"] + pro_forma["seller_note_payment"]
    return pro_forma["ebitda"] / total_debt.replace(0, np.nan)


def compute_earnout_scenarios(
    purchase_price: float,
    pct_earnout: float,
    earnout_period: int,
    revenue: float,
    aum: float,
    num_clients: int,
    growth_rate: float,
    attrition_rate: float,
    earnout_metric: str = "Revenue Retention",
    earnout_floor_pct: float = 0,
    earnout_cap_pct: float = 125,
    earnout_cliff: bool = False,
) -> list:
    """
    Model earnout payouts under 3 scenarios.
    Supports different metrics, floor/cap, and cliff vesting.
    """
    total_earnout = purchase_price * pct_earnout
    annual_earnout = total_earnout / earnout_period if earnout_period > 0 else 0

    scenarios = []
    for scenario, growth_adj, attrition_adj in [
        ("Base Case", 0, 0),
        ("Upside", 0.02, -0.02),
        ("Downside", -0.02, 0.03),
    ]:
        adj_growth = growth_rate + growth_adj
        adj_attrition = max(attrition_rate + attrition_adj, 0)

        yearly_payouts = []
        yr_rev = revenue
        yr_aum = aum
        yr_clients = num_clients
        total_payout = 0
        cumulative_achievement = 0

        for yr in range(1, earnout_period + 1):
            if yr <= 2:
                eff_rate = adj_growth - adj_attrition
            else:
                eff_rate = adj_growth

            yr_rev *= (1 + eff_rate)
            yr_aum *= (1 + eff_rate)
            yr_clients = int(yr_clients * (1 - adj_attrition if yr <= 2 else 1))

            # Achievement % based on selected metric
            if earnout_metric == "AUM Retention":
                target = aum * ((1 + growth_rate) ** yr)
                achievement = yr_aum / target if target else 1.0
            elif earnout_metric == "Client Retention":
                target = num_clients * ((1 - attrition_rate) ** min(yr, 2))
                achievement = yr_clients / target if target else 1.0
            else:  # Revenue Retention
                target = revenue * ((1 + growth_rate) ** yr)
                achievement = yr_rev / target if target else 1.0

            # Apply floor and cap
            achievement = max(achievement, earnout_floor_pct / 100)
            achievement = min(achievement, earnout_cap_pct / 100)

            if earnout_cliff:
                # Cliff: nothing until final year, then full based on avg achievement
                cumulative_achievement += achievement
                if yr == earnout_period:
                    avg_achievement = cumulative_achievement / earnout_period
                    yr_payout = total_earnout * avg_achievement
                else:
                    yr_payout = 0
            else:
                yr_payout = annual_earnout * achievement

            total_payout += yr_payout
            yearly_payouts.append(yr_payout)

        scenarios.append({
            "scenario": scenario,
            "total_payout": total_payout,
            "pct_of_max": total_payout / total_earnout * 100 if total_earnout else 0,
            "yearly_payouts": yearly_payouts,
        })

    return scenarios


def compute_seller_total_proceeds(
    purchase_price: float,
    pct_upfront: float,
    pct_seller_note: float,
    note_rate: float,
    note_term: int,
    earnout_scenarios: list,
    note_standstill_years: int = 0,
    note_io_during_standstill: bool = False,
    consulting_annual: float = 0,
    consulting_years: int = 0,
    noncompete_total: float = 0,
    noncompete_years: int = 0,
    years: int = 7,
) -> pd.DataFrame:
    """Timeline of total seller proceeds: upfront + note payments + earnout + transition."""
    upfront_cash = purchase_price * pct_upfront
    note_principal = purchase_price * pct_seller_note
    note_schedule = build_seller_note_amortization(
        note_principal, note_rate, note_term,
        standstill_years=note_standstill_years,
        io_during_standstill=note_io_during_standstill,
    )
    annual_noncompete = noncompete_total / noncompete_years if noncompete_years > 0 else 0

    rows = []
    for scenario_data in earnout_scenarios:
        scenario_name = scenario_data["scenario"]
        cumulative = upfront_cash  # received at close

        for yr in range(1, years + 1):
            note_row = note_schedule[note_schedule["year"] == yr]
            yr_note = note_row["payment"].iloc[0] if len(note_row) > 0 else 0
            yr_earnout = 0
            if yr <= len(scenario_data["yearly_payouts"]):
                yr_earnout = scenario_data["yearly_payouts"][yr - 1]
            yr_consult = consulting_annual if yr <= consulting_years else 0
            yr_nc = annual_noncompete if yr <= noncompete_years else 0

            cumulative += yr_note + yr_earnout + yr_consult + yr_nc
            rows.append({
                "scenario": scenario_name,
                "year": yr,
                "note_payment": yr_note,
                "earnout_payment": yr_earnout,
                "consulting_pay": yr_consult,
                "noncompete_pay": yr_nc,
                "cumulative_proceeds": cumulative,
            })

    return pd.DataFrame(rows)


def sensitivity_irr(
    base_params: dict,
    multiples: list,
    attrition_rates: list,
) -> pd.DataFrame:
    """Two-way sensitivity: purchase multiple vs attrition rate -> IRR at year 5."""
    results = []
    for mult in multiples:
        for att in attrition_rates:
            params = base_params.copy()
            params["purchase_price"] = params["revenue"] * mult
            params["attrition_rate"] = att

            pf = build_pro_forma(**{k: v for k, v in params.items() if k in _PRO_FORMA_PARAMS})
            returns = compute_irr_and_returns(
                pf,
                params["purchase_price"],
                params["pct_upfront"],
                params["pct_self_funded"],
                params.get("pct_equity_rollover", 0),
                params.get("pct_recurring", 100),
            )
            # IRR is None for degenerate inputs (no equity). Map to NaN so
            # the heatmap masks the cell rather than charting a misleading 0.
            results.append({
                "multiple": mult,
                "attrition_rate": att,
                "irr_yr5": returns["irr_yr5"] if returns["irr_yr5"] is not None else np.nan,
            })

    df = pd.DataFrame(results)
    return df.pivot(index="multiple", columns="attrition_rate", values="irr_yr5")


def sensitivity_breakeven(
    base_params: dict,
    growth_rates: list,
    multiples: list,
) -> pd.DataFrame:
    """Two-way sensitivity: growth rate vs purchase multiple -> breakeven year."""
    results = []
    for gr in growth_rates:
        for mult in multiples:
            params = base_params.copy()
            params["growth_rate"] = gr
            params["purchase_price"] = params["revenue"] * mult

            pf = build_pro_forma(**{k: v for k, v in params.items() if k in _PRO_FORMA_PARAMS})
            returns = compute_irr_and_returns(
                pf,
                params["purchase_price"],
                params["pct_upfront"],
                params["pct_self_funded"],
                params.get("pct_equity_rollover", 0),
                params.get("pct_recurring", 100),
            )
            be = returns["breakeven_year"]
            results.append({
                "growth_rate": gr,
                "multiple": mult,
                "breakeven_year": be if isinstance(be, (int, float)) else 8,
            })

    df = pd.DataFrame(results)
    return df.pivot(index="growth_rate", columns="multiple", values="breakeven_year")


# --- Helper functions ---

_PRO_FORMA_PARAMS = {
    "revenue", "ebitda", "owner_comp", "growth_rate", "attrition_rate",
    "aum", "purchase_price", "pct_upfront", "pct_seller_note", "note_rate",
    "note_term", "pct_earnout", "earnout_period", "pct_equity_rollover",
    "pct_self_funded", "loan_rate", "loan_term", "integration_costs",
    "annual_synergies", "additional_staff", "years",
    "loan_io_years", "loan_balloon", "loan_amort_years",
    "note_standstill_years", "note_io_during_standstill",
    "consulting_annual", "consulting_years",
    "noncompete_total", "noncompete_years",
    "tax_rate",
    "earnout_achievement", "replacement_cost",
}


def _pmt(rate: float, nper: int, pv: float) -> float:
    """Calculate annual payment (PMT) for a fixed-rate loan."""
    if rate == 0:
        return pv / nper if nper > 0 else 0
    return pv * (rate * (1 + rate) ** nper) / ((1 + rate) ** nper - 1)


def _compute_irr(cash_flows: list, guess: float = 0.1, max_iter: int = 1000, tol: float = 1e-8) -> float:
    """Compute IRR using Newton's method."""
    if not cash_flows or len(cash_flows) < 2:
        return 0.0

    rate = guess
    for _ in range(max_iter):
        npv = sum(cf / (1 + rate) ** t for t, cf in enumerate(cash_flows))
        dnpv = sum(-t * cf / (1 + rate) ** (t + 1) for t, cf in enumerate(cash_flows))
        if abs(dnpv) < 1e-14:
            break
        new_rate = rate - npv / dnpv
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate

    try:
        return float(np.irr(cash_flows)) if hasattr(np, 'irr') else rate
    except Exception:
        return rate
