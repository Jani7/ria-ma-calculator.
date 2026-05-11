"""
Seed / refresh notes for data/ria_ma_comps.csv (RIA M&A comparable transactions).

This file is intentionally a documentation script, not an automated scraper.
RIA M&A multiples are almost never officially disclosed: published numbers
are derived from buyer commentary, analyst commentary (Echelon, DeVoe), and
trade-press inference. An automated scraper would manufacture a false sense
of precision — every row needs a human in the loop.

How to add a new transaction
----------------------------
1. Find a primary source (press release, RIABiz, WealthManagement.com).
2. If the multiple is officially disclosed, drop it in and leave `notes`
   empty (or use it for color).
3. If you are deriving the multiple from disclosed price + estimated
   revenue/EBITDA, prefix `notes` with "ESTIMATED:" and explain the basis.
4. Bucket aum_tier and recurring_tier per the calculator's enums:
     aum_tier        ∈ {"<200M", "200M-500M", "500M-1B", "1B-5B", "5B+"}
     recurring_tier  ∈ {"<70%", "70-90%", "90%+"}
     channel         ∈ {"aggregator", "platform", "pe-platform",
                        "bank/wirehouse", "ria-to-ria"}

Recurring tier heuristic when not disclosed
-------------------------------------------
- Wealth managers w/ RIA + planning focus  → 90%+
- Multi-line books (RIA + brokerage)       → 70-90%
- Heavy brokerage / commission / insurance → <70%
- Institutional consultants (OCIO / DC)    → 90%+ (board contracts)

Source priority (banker memo Part D, in order)
----------------------------------------------
1. Echelon Partners quarterly RIA M&A Deal Reports
2. DeVoe Deal Book quarterly summaries
3. RIABiz.com deal coverage
4. WealthManagement.com M&A vertical
5. PR Newswire / Business Wire press releases
6. SEC Form ADV-W (post-deal withdrawal) for closing-date confirmation

What NOT to do
--------------
- Do not fabricate deals. Better 30 verified rows than 50 with three fakes.
- Do not use SourceForge / S&P CapitalIQ / Pitchbook unless you have a
  license — those terms are paywalled and we can't redistribute them.
- Do not include sub-$50M deals unless they're truly informative; the band
  for tuck-ins is dominated by 2-3 deals and you'll just add noise.

Quarterly refresh checklist
---------------------------
- Pull Echelon's most recent quarterly RIA M&A Deal Report for headline
  numbers (median EV/EBITDA, average deal size).
- Cross-reference any new transactions against existing rows; dedupe.
- Update this docstring's "last refreshed" line below.

Last refreshed: 2026-05-11 (initial seed, 35 transactions 2023-04 → 2026-01).
"""

# Intentionally executable but a no-op. Run for the sanity check:
#     python scripts/seed_comps.py
from pathlib import Path
import csv

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "ria_ma_comps.csv"


def main() -> int:
    if not CSV_PATH.exists():
        print(f"Missing: {CSV_PATH}")
        return 1
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    required = {
        "date", "buyer", "seller", "seller_aum",
        "ev_revenue_multiple", "ev_ebitda_multiple",
        "aum_tier", "recurring_tier", "channel", "source_url",
    }
    missing_cols = required - set(rows[0].keys() if rows else [])
    if missing_cols:
        print(f"Schema error — missing columns: {missing_cols}")
        return 2
    no_source = [r for r in rows if not r.get("source_url", "").strip()]
    if no_source:
        print(f"{len(no_source)} row(s) missing source_url:")
        for r in no_source:
            print(f"  - {r.get('buyer')} / {r.get('seller')}")
        return 3
    print(f"OK — {len(rows)} transactions, all have source_url.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
