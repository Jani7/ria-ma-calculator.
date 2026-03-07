# RIA M&A Calculator

Buyer-side acquisition economics calculator for Registered Investment Advisors (RIAs). Models deal structure, pro forma financials, returns analysis, and sensitivity scenarios.

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
streamlit run app.py
```

## Features

- **Deal Summary** — Purchase price with implied multiples (revenue, AUM, EBOC), deal structure waterfall chart, key return metrics (IRR, cash-on-cash, breakeven, DSCR)
- **Pro Forma Financials** — 5-year P&L with revenue, expenses, EBITDA, debt service, net cash flow. Optional combined entity view.
- **Sensitivity Analysis** — Two-way heatmaps: (1) Revenue multiple vs. attrition rate → IRR, (2) Growth rate vs. multiple → breakeven year
- **Earnout & Seller Economics** — Earnout payouts under 3 scenarios (base, upside, downside), total seller proceeds timeline, seller note amortization
- **Debt Analysis** — Loan amortization schedule, DSCR by year, principal vs. interest breakdown
- **PDF Export** — Full analysis export

## Inputs

All inputs are configurable via the sidebar:

| Category | Inputs |
|----------|--------|
| Target Firm | AUM, revenue, EBITDA, owner comp, clients, growth rate, recurring %, attrition |
| Deal Terms | Purchase price or multiple, upfront/note/earnout/rollover split, note rate & term |
| Financing | Self-funded %, loan rate, loan term |
| Integration | One-time costs, annual synergies, additional staff, timeline |
| Buyer Profile | Existing AUM, revenue, margin (optional combined view) |

## Key Calculations

- **EBOC** = EBITDA + (Owner's Comp − $200K market replacement)
- **IRR** at years 3, 5, 7 (with 6x EBITDA terminal value)
- **Client attrition** applied to years 1-2, stabilizing year 3+
- **DSCR** = EBITDA / total debt service

## Tech Stack

- Streamlit (UI)
- Plotly (charts)
- Pandas/NumPy (calculations)
- FPDF2 (PDF export)
