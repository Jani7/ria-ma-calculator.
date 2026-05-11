# RIA M&A Calculator

## Project Overview
Buyer-side acquisition economics calculator for Registered Investment Advisors (RIAs). Models deal structure, pro forma financials, returns analysis, and sensitivity scenarios. Built with Streamlit.

## Live App
- **URL:** https://ria-ma-calc.streamlit.app/
- **Hosting:** Streamlit Community Cloud (free tier, requires public repo)
- **GitHub:** https://github.com/Jani7/ria-ma-calculator. (public)

## Tech Stack
- **UI:** Streamlit (dark theme, tabbed layout with welcome page)
- **Charts:** Plotly
- **Calculations:** Pandas, NumPy
- **PDF Export:** fpdf2
- **Python:** 3.11+ (devcontainer configured)

## File Structure
```
ria_ma_calculator/
  app.py              # Main Streamlit app — UI, sidebar inputs, 6 tabs + PDF export
  calculations.py     # Financial engine — EBOC, pro forma P&L, IRR, amortization, sensitivity
  pdf_export.py       # PDF report generation
  requirements.txt    # Pinned dependencies (exact versions)
  SECURITY.md         # Vulnerability reporting policy
  .gitignore          # Hardened — covers secrets, certs, IDE, OS files, venvs
  .streamlit/config.toml  # Dark theme config
  .devcontainer/devcontainer.json  # Codespaces setup
```

## Key Architecture
- All inputs are in the Streamlit sidebar, organized by section (Target Firm, Deal Terms, Seller Note, Earnout, Financing, Integration, Tax, Buyer Profile)
- Everything updates live — no submit button
- Session state gates a welcome page before showing the calculator
- `calculations.py` is pure functions with no Streamlit dependency (testable independently)
- EBOC = EBITDA + (Owner Comp - $200K market replacement)
- IRR uses 6x EBITDA terminal value at years 3, 5, 7
- Client attrition applied to years 1-2 only

## Security (completed May 2026)
- Full security audit: no secrets in code or git history
- `.gitignore` expanded to 44 rules (secrets, certs, IDE, OS, venvs, Streamlit secrets)
- Dependencies pinned to exact versions matching local environment
- `SECURITY.md` added with vulnerability reporting policy
- No persistent data storage — all inputs are session-scoped
- `unsafe_allow_html=True` used for hardcoded CSS/HTML only (no user-generated HTML)

## SEC ADV Auto-Fill
**Status:** Implemented May 2026.

**What it does:** "Lookup RIA" search box in the sidebar. User types a firm name, picks a match, and AUM / client count / estimated revenue / growth rate auto-populate from SEC Form ADV. A reconciliation dialog asks per-field whether to use the SEC value or keep the user's input. Each input retains a "↺ Use SEC value" badge so the user can swap at any time.

**Data source:** SEC's bulk Form ADV dataset, shipped with the app as `data/adv_current.parquet`.

Important — what is **NOT** the source (and why future sessions should not chase these):
- `efts.sec.gov/LATEST/search-index` is EDGAR full-text search for **corporate** filings (10-K/Q, 8-K). RIAs don't file Form ADV through EDGAR.
- `data.sec.gov` publishes only corporate XBRL submissions — no Form ADV.
- Form ADV is filed through **IARD** (FINRA-administered) and exposed via IAPD (`adviserinfo.sec.gov`). IAPD has **no public/documented JSON API**; the undocumented internal endpoints its React UI uses are fragile and against SEC's automation policy.
- DO NOT use adv.news — locked down (Disallow: /api/ in robots.txt, Cloudflare-protected).

**Canonical free source:** SEC bulk ADV ZIP at https://www.sec.gov/foia-services/frequently-requested-documents/form-adv-data (refreshed monthly, ~15K SEC-registered RIAs). Refresh process: `python scripts/build_adv_data.py` regenerates the Parquet from the latest ZIP. Run quarterly.

**Field mapping:**
| Calculator Input | SEC ADV Source | Notes |
|---|---|---|
| AUM | Form ADV Item 5.F | Direct value |
| Number of Clients | Form ADV Item 5.D | Direct value |
| Revenue | Not in ADV | Estimate: AUM × default fee rate (0.75%) |
| Growth Rate | Current vs prior-year AUM snapshot | Both snapshots in the Parquet |
| EBITDA | Not in ADV | Manual input required |
| Owner's Comp | Not in ADV | Manual input required |

## Future Monetization Ideas
1. **SEC ADV Auto-Fill** — free for 3 lookups, paid after (freemium gate)
2. **Saved Deals & Scenario Comparison** — requires user accounts
3. **Branded PDF Reports** — white-labeled exports for IC memos
4. **Comparable Transaction Database** — benchmark deals against market
5. **Multi-Target Portfolio Modeling** — model acquiring multiple RIAs
6. **Monte Carlo Simulation** — probabilistic return analysis

## Dev Notes
- Repo has a trailing dot in the name: `ria-ma-calculator.` (not a typo)
- Branch is `main` (not master)
- Main file is `app.py` (not streamlit_app.py)
- Git user: jani7 / jani7@users.noreply.github.com
