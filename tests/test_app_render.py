"""Headless render smoke test for the integrated app.

Executes app.py in-process via streamlit.testing.v1.AppTest — catches the
class of runtime failures that AST checks and module imports can't see
(bad kwargs at render time, Streamlit API misuse, session-state key
collisions). Three passes: welcome page, calculator with zero inputs,
calculator with scenario-1 firm data.

Run: python tests/test_app_render.py   (from ria_ma_calculator/)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")
FAILURES = []


def check(label, at):
    if at.exception:
        FAILURES.append((label, [str(e.value) for e in at.exception]))
        print(f"FAIL  {label}: {[str(e.value)[:200] for e in at.exception]}")
    else:
        print(f"PASS  {label}")


# Pass 1 — welcome page (default session state)
at = AppTest.from_file(APP, default_timeout=120)
at.run()
check("welcome page renders", at)

# Pass 2 — calculator, zero firm data (the empty state)
at.session_state["show_calculator"] = True
at.run()
check("calculator renders at zero inputs", at)
n_tabs = len(at.tabs)
print(f"      tabs found: {n_tabs} (expect 5)")
if n_tabs != 5:
    FAILURES.append(("tab count", n_tabs))

# Pass 3 — calculator with scenario-1 firm data via session state
# (comma strings for currency/count keys — the widget contract)
for k, v in {
    "aum": "500,000,000", "revenue": "4,000,000", "ebitda": "1,600,000",
    "owner_comp": "500,000", "num_clients": "200",
    "purchase_price": "8,000,000",
}.items():
    at.session_state[k] = v
at.session_state["rev_growth_pct"] = 5.0
at.session_state["pct_recurring"] = 90
at.session_state["attrition_rate"] = 5.0
at.run()
check("calculator renders with scenario-1 data", at)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    sys.exit(1)
print("ALL RENDER SMOKE TESTS PASSED")
