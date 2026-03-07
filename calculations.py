"""
RIA M&A Calculator — Core calculation engine.
All financial modeling logic: EBOC, pro forma P&L, IRR, amortization, sensitivity analysis.
"""

import numpy as np
import pandas as pd


MARKET_REPLACEMENT_COST = 200_000  # Default market replacement cost for owner


def compute_eboc(ebitda: float, owner_comp: float, replacement_cost: float = MARKET_REPLACEMENT_COST) -> float:
    """EBOC = EBITDA + (Owner's Comp - Market Replacement Cost)."""
    return ebitda + (owner_comp - replacement_cost)


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
) -> pd.DataFrame:
    """Build a year-by-year pro forma P&L for years 1-N."""
    rows = []
    # Expense base = Revenue - EBITDA, then replace owner comp with market rate
    base_expenses = revenue - ebitda - owner_comp + MARKET_REPLACEMENT_COST
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

    for yr in range(1, years + 1):
        # Revenue with growth and attrition
        if yr <= 2:
            effective_rate = growth_rate - attrition_rate
        else:
            effective_rate = growth_rate

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

        yr_pretax_cf = yr_ebitda - total_debt_service

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
) -> dict:
    """Compute IRR and cash-on-cash returns at years 3, 5, 7."""
    total_cash_invested = purchase_price * pct_upfront * pct_self_funded
    if total_cash_invested <= 0:
        total_cash_invested = 1  # avoid division by zero

    # Cash flows for IRR: initial outlay + annual net cash flows
    cf = [-total_cash_invested] + pro_forma["net_cash_flow"].tolist()

    # IRR for different horizons
    results = {}
    for horizon in [3, 5, 7]:
        subset = cf[: horizon + 1]
        # Add terminal value at exit (rough: last year EBITDA * 6x multiple)
        if horizon <= len(pro_forma):
            terminal = pro_forma.iloc[min(horizon - 1, len(pro_forma) - 1)]["ebitda"] * 6
            subset_with_tv = subset.copy()
            subset_with_tv[-1] += terminal
        else:
            subset_with_tv = subset

        irr = _compute_irr(subset_with_tv)
        cumulative_cf = sum(subset[1:])
        coc = cumulative_cf / total_cash_invested

        results[f"irr_yr{horizon}"] = irr
        results[f"coc_yr{horizon}"] = coc

    results["total_cash_invested"] = total_cash_invested

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
    """Debt Service Coverage Ratio = EBITDA / (Debt Service + Seller Note Payment)."""
    total_debt = pro_forma["debt_service"] + pro_forma["seller_note_payment"]
    dscr = pro_forma["ebitda"] / total_debt.replace(0, np.nan)
    return dscr.fillna(0)


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
            )
            results.append({
                "multiple": mult,
                "attrition_rate": att,
                "irr_yr5": returns["irr_yr5"],
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
