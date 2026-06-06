# RIA M&A Calculator

## Project Overview
Buyer-side acquisition economics calculator for Registered Investment Advisors (RIAs). Models deal structure, pro forma financials, returns analysis, sensitivity scenarios, and integration economics. Built with Streamlit, deployed on Streamlit Community Cloud.

## Live App
- **URL:** https://ria-ma-calc.streamlit.app/
- **Hosting:** Streamlit Community Cloud (free tier, requires public repo)
- **Public repo:** https://github.com/Jani7/ria-ma-calculator. (trailing dot is intentional)
- **Private data repo:** https://github.com/Jani7/ria-ma-data (Form ADV parquet)

## Tech Stack
- **UI:** Streamlit (dark theme, tabbed layout with welcome page)
- **Charts:** Plotly
- **Calculations:** Pandas, NumPy
- **PDF Export:** fpdf2
- **Python:** 3.11+ (devcontainer configured)

## File Structure
```
ria_ma_calculator/
  app.py              # Streamlit UI — sidebar inputs, 7 tabs + PDF export
  calculations.py     # Financial engine (pure functions, no Streamlit dep)
  pdf_export.py       # PDF report generation
  sec_lookup.py       # SEC ADV Form lookup (parquet-backed)
  requirements.txt    # Pinned dependencies (exact versions)
  SECURITY.md         # Vulnerability reporting policy
  .gitignore          # Hardened — secrets, certs, IDE, OS, venvs, parquet
  .streamlit/config.toml          # Dark theme config
  .devcontainer/devcontainer.json # Codespaces setup
  data/
    ria_ma_comps.csv      # 35-row comp transactions DB (tracked)
    adv_current.parquet   # SEC ADV bulk data (gitignored, hot-loaded from private repo)
  scripts/
    build_adv_data.py     # Refresh Parquet from SEC bulk ZIP
    seed_comps.py         # Refresh comps CSV
```

## Key Architecture
- All inputs are in the Streamlit sidebar, organized by section (Target Firm, Deal Terms, Seller Note, Earnout, Financing, Integration, Tax, Buyer Profile).
- Everything updates live — no submit button.
- Session state gates a welcome page before showing the calculator.
- `calculations.py` is pure functions with no Streamlit dependency (testable independently).
- Helper UI factories in `app.py`: `currency_input()` and `count_input()` (both apply thousands separators via `format="%d"` and live state-sync).
- Tabs (7): Deal Summary, Pro Forma Financials, Sensitivity Analysis, Earnout & Seller Economics, Debt Analysis, Integration Strategy, Instructions.

## Core Math (calculations.py)
- **EBOC** = EBITDA + (Owner Comp − Market Replacement). Replacement scales by AUM via `auto_replacement_cost()` (lines 17-30): $200K <$200M, $400K $200M-$1B, $600K $1B+.
- **Attrition**: applied at the input rate for years 1-2, then decays to `STEADY_STATE_ATTRITION_FRACTION` (0.4×) for Y3+. Prior versions zeroed Y3+ — flagged by red-team review.
- **IRR exit multiple**: scaled by % recurring revenue. 50% → 4×, 100% → 6× (`_exit_multiple_from_recurring`, lines 234-242).
- **Terminal-value debt netting**: `compute_irr_and_returns()` now subtracts remaining loan + seller-note principal at exit year (lines 525-617). Prior version overstated levered IRR.
- **DSCR**: returns NaN (not 0) for zero-debt years so the UI renders "n/a" (lines 868-877).

## Banker-MD Framework (Parts B/C/D/E)

### Part B — Valuation Band (`compute_valuation_band`, lines 134-231)
Multi-factor EBITDA-multiple band. Base 7.0× anchor. Factors multiply: recurring %, AUM size tier, growth rate, EBITDA margin, plus qualitative book_quality / geography / key_person scores. Returns `low/mid/high` (±15% band, widened to ±20% for sub-$200M or thin-book deals). Surfaces in Deal Summary tab.

### Part C — Suggested Deal Structure (`suggest_deal_structure`, lines 38-131)
Profile-driven splits across upfront / seller note / earnout / rollover. Five profiles: key_person_concentrated, small_tuck_in, platform ($1B+ owner-staying), high_growth, mid_market. Age-65+ tilts more upfront; 95%+ recurring shrinks earnout. Returns note rate (default 6.5%), term (5y), earnout period (3y), earnout cap (115-125%), standstill (1y).

### Part D — Comps Overlay (`lookup_comps_band`, lines 784-865)
35-row CSV at `data/ria_ma_comps.csv`. P25/median/P75 of EV/EBITDA and EV/Revenue for the relevant AUM-tier × recurring-tier slice. Falls back to adjacent AUM tiers if primary slice <5 deals (per `_ADJACENT_AUM_TIERS`). Returns None if even the widened slice is too thin.

### Part E — Integration Synergies (`compute_synergy_schedule`, lines 254-335)
Per-year ramp of cost takeout / revenue uplift / capture cost. Cost ramp 20/60/90/100, revenue ramp 0/50/85/100 (lags one year), capture cost split 60/30/10 across Y1-3. `buyer_profile` dict (compliance/tech/back_office/planning/cco) sets capability_factor (floor 0.25). Synergy NPV at 10% discount. When passed into `build_pro_forma(synergy_schedule=...)`, it overrides the legacy flat `integration_costs` + `annual_synergies` inputs.

## SEC ADV Auto-Fill
**Status:** Implemented May 2026.

**What it does:** "Lookup RIA" search box in the sidebar. User types a firm name, picks a match, and AUM / client count / estimated revenue / growth rate auto-populate from SEC Form ADV. A reconciliation dialog asks per-field whether to use the SEC value or keep the user's input. Each input retains a "↺ Use SEC value" badge so the user can swap at any time.

**Data source:** SEC's bulk Form ADV dataset, shipped as `data/adv_current.parquet`.

Not the source (do not chase):
- `efts.sec.gov` / `data.sec.gov`: EDGAR corporate filings only — RIAs don't file Form ADV through EDGAR.
- IAPD (`adviserinfo.sec.gov`) has no public/documented JSON API.
- DO NOT use adv.news — locked down.

**Canonical free source:** SEC bulk ADV ZIP at https://www.sec.gov/data-research/sec-markets-data/information-about-registered-investment-advisers-exempt-reporting-advisers. Refreshed monthly. Rebuild flow:
```
python scripts/build_adv_data.py     # regenerate Parquet
cp data/adv_current.parquet /path/to/ria-ma-data/
git -C /path/to/ria-ma-data commit -am "Refresh ADV snapshot YYYY-MM"
git -C /path/to/ria-ma-data push
```
Streamlit Cloud picks up the new file on next cold start.

**Runtime resolution order** (`sec_lookup.py`):
1. `data/adv_current.parquet` — local dev (gitignored).
2. `/tmp/ria_adv_cache/adv_current.parquet` — populated by step 3, cached for container lifetime.
3. Private repo `Jani7/ria-ma-data` via GitHub Contents API, authenticated with Streamlit secret `ADV_DATA_TOKEN` (fine-grained PAT, Contents:read, scoped to that repo only).
4. None resolve → lookup UI shows "SEC data file not available"; rest of the app keeps working.

**Field mapping:**
| Calculator Input | SEC ADV Source | Notes |
|---|---|---|
| AUM | Form ADV Item 5.F | Direct value |
| Number of Clients | Form ADV Item 5.D | Direct value |
| Revenue | Not in ADV | Estimate: AUM × default fee rate (0.75%) |
| Growth Rate | Current vs prior-year AUM snapshot | Both snapshots in Parquet |
| EBITDA | Not in ADV | Manual input required |
| Owner's Comp | Not in ADV | Manual input required |

## Security Posture
- Full security audit: no secrets in code or git history.
- `.gitignore` hardened (44+ rules).
- Dependencies pinned to exact versions.
- `SECURITY.md` documents vulnerability reporting policy.
- No persistent data storage — all inputs session-scoped.
- `unsafe_allow_html=True` only used for hardcoded CSS/HTML (no user-generated HTML).

**History rewrite (completed this session):** `data/adv_current.parquet` was purged from git history using `git filter-repo`, then force-pushed. Local safety net:
- Branch: `backup/pre-history-rewrite`
- Tag: `pre-history-rewrite-backup`

**Orphaned-blob caveat:** GitHub retains unreachable blobs for ~90 days unless GC is forced. A GitHub Support ticket is open to expire the orphaned blob sooner. PAT rotation pending in Streamlit Cloud secrets (the leaked file was data, not credentials, but the token that fetches it should still be rotated post-rewrite).

## Open Follow-ups
1. **PDF export still reads legacy `integration_costs` field** (banker memo Part E Q): when the synergy schedule overrides legacy flat inputs, the PDF report renders the old field. Update `pdf_export.py` to surface the per-year synergy schedule.
2. **PAT rotation pending** in Streamlit Cloud secrets (`ADV_DATA_TOKEN`).
3. **GitHub Support ticket** open to expire the orphaned parquet blob.

## Dev Notes
- Repo has a trailing dot in the name: `ria-ma-calculator.` (not a typo).
- Branch is `main` (not master) on remote; local working copy on `master`.
- Main file is `app.py` (not streamlit_app.py).
- Git user: jani7 / jani7@users.noreply.github.com.
- User authorizes direct pushes to main per-session.

## Future Monetization Ideas
1. SEC ADV Auto-Fill — free for 3 lookups, paid after (freemium gate)
2. Saved Deals & Scenario Comparison (requires accounts)
3. Branded PDF Reports — white-labeled IC memos
4. Comparable Transaction Database — expand the 35-row seed
5. Multi-Target Portfolio Modeling
6. Monte Carlo Simulation
