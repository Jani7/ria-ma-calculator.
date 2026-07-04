# Comparable-Transactions Database — Sources & Provenance

Last audited: 2026-07-03.

Every row in `ria_ma_comps.csv` carries an `is_estimated` flag:

- **`FALSE` (confirmed)** — the EV/EBITDA or EV/Revenue multiple is either stated
  outright in the cited source, or arithmetically derivable from figures stated in
  the cited sources. The arithmetic is shown below, figure by figure.
- **`TRUE` (estimated)** — the multiple is a modeled guess (buyer's "typical pricing
  band", trade-press vibes, or an LLM-synthesized figure). These rows are excluded
  from the computed P25/median/P75 benchmark by default and carry **no** `source_url`.

Only 4 of 39 rows are confirmed. That is not a gap to be papered over — it is the
honest state of this market: RIA sellers are private, buyers are private, and deal
terms almost never reach the public record in a form you can put a multiple on.

---

## Confirmed rows (`is_estimated = FALSE`)

### 1. Clayton Dubilier & Rice + Stone Point Capital / Focus Financial Partners

- **Row:** date 2023-02-27 · EV/Revenue 3.3x · EV/EBITDA 13.0x · 5B+ · 90%+ · aggregator
- **Primary source (in CSV):** completion press release, SEC-hosted Exhibit 99.1 —
  <https://www.sec.gov/Archives/edgar/data/1651052/000110465923097110/tm2325227d1_ex99-1.htm>
  (mirror: <https://www.cdr.com/news/clayton-dubilier-rice-completes-acquisition-of-focus-financial-partners>)
- **Secondary source:** Focus Financial Partners Q4/FY2022 earnings release
  (Feb 16, 2023), via Focus IR / SEC 8-K.

| Figure | Status | Value |
|---|---|---|
| Enterprise value | **Disclosed** — "enterprise value in excess of $7 billion" (all-cash, $53.00/share; announced 2023-02-27, closed 2023-08-31) | $7,000M used as floor |
| FY2022 Adjusted EBITDA | **Disclosed** — Focus FY2022 earnings release | $537.5M |
| FY2022 total revenues | **Disclosed** — Focus FY2022 earnings release | $2,143.2M |
| EV/EBITDA | **Derived** | 7,000 / 537.5 = **13.0x** |
| EV/Revenue | **Derived** | 7,000 / 2,143.2 = 3.27 ≈ **3.3x** |

Notes: using the $7.0B floor of "in excess of $7 billion" makes both multiples
conservative. `seller_aum` 350e9 is Focus's commonly cited ">$350B client assets" —
approximate, and irrelevant to the multiple arithmetic (tier is 5B+ regardless).

### 2. ADIA / Bain Capital / Flexpoint Ford / Ares / State of Wisconsin — 20% pre-IPO stake in Corient (CI US)

- **Row:** date 2023-05-11 · EV/Revenue blank · EV/EBITDA 25.6x · 5B+ · 90%+ · aggregator
- **Primary source (in CSV):** CI Financial announcement, quote-verified —
  <https://ir.cifinancial.com/news/news-details/2023/CI-Financial-Announces-pre-IPO-Minority-Investment-in-its-U.S.-Wealth-Management-Business-from-Leading-Global-Institutional-Investors/default.aspx>
  (mirror: <https://www.businesswire.com/news/home/20230510006148/en/>)

| Figure | Status | Value |
|---|---|---|
| Investment | **Disclosed** — C$1.34B (US$1.0B) for 20% of CI US | stated |
| Implied enterprise value | **Disclosed** — $7.1B | stated |
| EV/EBITDA | **Disclosed verbatim** — "25.6x CI US Q1'23 annualized Adjusted EBITDA" | **25.6x** |
| EV/Revenue | Not disclosed | left blank |

Notes: this is a minority-stake implied valuation, not a control sale — minority
growth rounds price rich, and the row's notes say so. `seller_aum` 147e9 per CI's
2023 reporting of ~US$147B in US wealth client assets (approximate). Multiple as
stated by CI is currency-independent.

### 3. Cetera Holdings / Avantax (take-private)

- **Row:** date 2023-09-11 · EV/Revenue 1.6x · EV/EBITDA 9.6x · 5B+ · <70% · hybrid-bd
- **Primary source (in CSV):** definitive-agreement release —
  <https://www.globenewswire.com/news-release/2023/09/11/2740821/0/en/Avantax-Enters-into-Definitive-Agreement-to-be-Acquired-by-Cetera-Holdings.html>
- **Secondary source:** Avantax Q2 2023 earnings release (Aug 9, 2023), quote-verified —
  <https://www.globenewswire.com/news-release/2023/08/09/2722129/0/en/Avantax-Reports-Second-Quarter-2023-Results.html>

| Figure | Status | Value |
|---|---|---|
| Enterprise value | **Disclosed** — "approximately $1.2 billion, inclusive of Avantax's net debt" ($26.00/share all-cash) | $1,200M |
| FY2023 Adjusted EBITDA outlook | **Disclosed** — $124.5M–$126.5M (guidance in effect at announcement) | midpoint $125.5M |
| FY2023 revenue outlook | **Disclosed** — $753.0M–$756.0M | midpoint $754.5M |
| EV/EBITDA | **Derived** | 1,200 / 125.5 = 9.56 ≈ **9.6x** |
| EV/Revenue | **Derived** | 1,200 / 754.5 = 1.59 ≈ **1.6x** |

Notes: EBITDA basis is company guidance (forward), not trailing actuals — Avantax
went private before reporting FY2023, so guidance is the best citable figure.
Tax-focused broker-dealer + RIA hybrid (~51% advisory assets at Q2'23); channel
`hybrid-bd` and recurring tier `<70%` keep it out of fee-only 90%+ slices.
`seller_aum` uses advisory AUM ($42.0B) rather than total client assets ($83.8B).

### 4. LPL Financial / Commonwealth Financial Network

- **Row:** date 2025-03-31 · EV/Revenue blank · EV/EBITDA 22.5x · 5B+ · 70-90% · hybrid-bd
- **Primary source (in CSV):** LPL announcement —
  <https://investor.lpl.com/news-releases/news-release-details/lpl-financial-acquire-commonwealth-financial-network>
  (SEC 8-K exhibits: <https://www.sec.gov/Archives/edgar/data/0001397911/000119312525067661/d697025dex991.htm> and dex992.htm;
  mirror without the EBITDA figures: <https://www.commonwealth.com/insights/press-and-news/press-releases/lpl-financial-to-acquire-commonwealth-financial-network>)

| Figure | Status | Value |
|---|---|---|
| Purchase price | **Disclosed** — "approximately $2.7 billion in cash" for 100% of the equity | $2,700M |
| Acquired baseline EBITDA | **Disclosed** — "acquired base-line EBITDA is expected to be approximately $120 million" upon close | $120M |
| Post-synergy run-rate EBITDA | **Disclosed** — "run-rate EBITDA of approximately $415 million" after onboarding/integration | context only |
| EV/EBITDA | **Derived** | 2,700 / 120 = **22.5x** pre-synergy baseline (≈6.5x on post-synergy run-rate) |
| EV/Revenue | Not disclosed | left blank |

Notes: 22.5x is a strategic consolidator paying for synergies — the row's notes
carry both the pre-synergy and post-synergy framing so nobody reads it naively.
Baseline EBITDA is LPL's "expected upon close" figure, i.e. company-stated, not
audited trailing. Recurring tier `70-90%` is our classification (Commonwealth is
the most advisory-weighted large IBD; the exact mix was not disclosed).

---

## Considered and rejected (failed the verifiability rule)

| Candidate | Why rejected |
|---|---|
| AlTi Global / East End Advisors (2024-04) | $76M initial price stated in the 8-K, but no target revenue/EBITDA disclosed — multiple not derivable. |
| Mubadala Capital / CI Financial (2024-11) | EV C$12.1B stated and CI EBITDA public, but CI is a diversified Canadian asset + wealth manager — category error for an RIA comp set. |
| Goldman Sachs PFM → Creative Planning (2023) | Price never officially disclosed; "~$1.6B per WSJ" is unverifiable. Kept as the pre-existing row, flagged `is_estimated=TRUE`. |
| Creative Planning / TPG minority (2024, "$15.3B") | Valuation only via "people familiar with the matter". |
| Fisher Investments / Advent + ADIA (2024, "$12.75B") | Valuation reported, but no revenue/EBITDA disclosed — no multiple derivable. |
| Kestra / Stone Point, Mercer recap, Lido / HPS, Hightower / ADIA | All "sources say" valuations, no disclosed financials. |
| LPL / Atria (2024) | $805M + $230M earnout stated, but no clean pre-synergy target EBITDA stated. |
| Rathbones / Investec W&I (UK), AssetMark / GTCR (TAMP), Envestnet / Bain (fintech) | Geography or category mismatch. |

## The 35 estimated rows

The original 35-row table shipped with multiples synthesized from buyers' "typical
pricing bands" — modeled guesses, not disclosures (34 rows say `ESTIMATED:` in
their notes; the Goldman/Creative Planning row derives from revenue estimates).
They are retained for context because the deals themselves happened and the tier
structure is useful scaffolding, but as of this audit they are flagged
`is_estimated=TRUE`, their unverified `source_url` values have been removed, and
they no longer feed the default benchmark. Their notes are preserved verbatim as
the audit trail. Channel labels for Mariner/AndCo and Hightower/NEPC were corrected
to `institutional` (both are institutional-consulting books, not retail wealth).

## Classification caveats

`aum_tier`, `recurring_tier`, and `channel` on confirmed rows are our
classifications for slicing purposes; where the underlying mix was not disclosed
(Avantax `<70%`, Commonwealth `70-90%`), the basis is described in the row notes
above. Multiples themselves are never classified — they are either stated,
derived per the tables above, or the row is flagged estimated.

## Adding rows

Add rows in-app (Deal Summary tab → "Browse transaction database" — session-only)
or edit this CSV directly. Rule of the file: `is_estimated=FALSE` requires a
`source_url` whose stated figures produce the multiple by arithmetic you could
show a banker. When in doubt, flag it `TRUE`.
