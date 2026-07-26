"""Regression: the app must never advertise data the model doesn't hold.

The bug this guards was invisible to every existing suite because both
halves were individually correct. persistence.py's codec round-trips
faithfully (test_persistence.py, 10/10) and app.py renders the share link
after every sidebar input. What was wrong was the JOIN: while the SEC
reconcile dialog was pending — including after a stray click outside the
modal, which fires no callback — the sidebar showed a green "SEC loaded"
chip and per-field "SEC: $326,285,243,393" captions while the widgets, and
therefore the link built from them, still held the previous (usually empty)
deal. The user copied a link the UI told them was a firm.

So these assertions run through a real render (AppTest) and decode the
link the sidebar actually printed. Two invariants:

  1. The link always matches the widgets — never the un-applied SEC values.
  2. The UI's provenance claims match the link.

Also guards the Clear/Cancel re-open path: releasing `last_loaded_crd`
without also retiring the search query re-fires the auto-lookup on the very
next run, reopening the dialog and spending another lookup from the
rate-limit budget. That regression is a loop, so it gets an explicit test.

Run: python tests/test_share_provenance.py   (from ria_ma_calculator/)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest

import persistence
from sec_lookup import FirmData

APP = str(Path(__file__).resolve().parents[1] / "app.py")
FAILURES = []


def ok(label, cond, detail=""):
    if cond:
        print(f"PASS  {label}")
    else:
        FAILURES.append((label, detail))
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


def no_exception(label, at):
    ok(label, not at.exception,
       "; ".join(str(e.value)[:300] for e in at.exception))


def sidebar_share_url(at):
    """The share link exactly as the sidebar printed it."""
    for c in at.sidebar.code:
        if c.value.startswith(persistence.APP_BASE_URL):
            return c.value
    return None


def decoded_share_state(at):
    url = sidebar_share_url(at)
    if url is None:
        return None
    return persistence.decode_deal_from_param(url.split("?d=", 1)[1])


def sidebar_text(at):
    return " ".join(m.value for m in at.sidebar.markdown)


def as_int(comma_string):
    return int(str(comma_string).replace(",", "").replace("$", "").strip())


def ss(at, key, default=None):
    """AppTest's SafeSessionState has no .get() — absent keys raise."""
    try:
        return at.session_state[key]
    except (KeyError, AttributeError):
        return default


# The exact literal the sidebar prints for MEGA's AUM. Held in one place so a
# negative assertion ("this must not appear") cannot be silently disarmed by a
# cosmetic reword of the caption — the positive assertion below uses the same
# constant and would fail first.
_SEC_CAPTION = "SEC: $326,285,243,393"


def firm_name_on_page(at):
    """Does the target firm's name appear anywhere in the main panel?

    Covers the memo's firm stamp — the surface that ships to an investment
    committee, and the one of the four that had no coverage at all (reverting
    its gate left every suite green). Scraping the rendered page rather than
    adding a test hook to app.py: a gate is only worth testing through the
    thing a reader would actually see.
    """
    blobs = [m.value for m in at.main.markdown]
    blobs += [c.value for c in at.main.caption]
    return any(MEGA.firm_name in b for b in blobs)


# A real mega-RIA: above the $10B revenue-estimate cap, so the reconcile
# dialog offers AUM/clients/growth but deliberately no revenue.
MEGA = FirmData(
    crd=110518,
    firm_name="EDELMAN FINANCIAL ENGINES",
    aum=326_285_243_393.0,
    num_clients=322_130,
    num_accounts=None,
    as_of_date="2026-03-31",
    aum_prior_year=292_863_000_000.0,
    registration_type="sec",
)


def fresh(**session):
    at = AppTest.from_file(APP, default_timeout=180)
    at.session_state["show_calculator"] = True
    for k, v in session.items():
        at.session_state[k] = v
    return at


# =============================================================================
# 1. Reconcile pending — the state the bug lived in
# =============================================================================
print("\n-- pending reconcile ------------------------------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=True)
at.run()
no_exception("app renders with a reconcile pending", at)

_text = sidebar_text(at)
ok("does not claim 'SEC applied' while pending", "SEC applied" not in _text)
ok("says the data is not applied yet", "not applied yet" in _text)
ok("offers a way back into the dialog",
   any(b.label == "Review & apply SEC values" for b in at.sidebar.button))

_decoded = decoded_share_state(at)
ok("sidebar still renders a share link", _decoded is not None)
if _decoded is not None:
    ok("share link does NOT carry the un-applied SEC AUM",
       _decoded.get("aum") != int(MEGA.aum),
       f"link aum={_decoded.get('aum')}")
    ok("share link matches the AUM widget",
       _decoded.get("aum") == as_int(at.session_state["aum"]),
       f"link={_decoded.get('aum')} widget={at.session_state['aum']}")
    ok("share link does NOT carry the un-applied client count",
       _decoded.get("num_clients") != int(MEGA.num_clients),
       f"link num_clients={_decoded.get('num_clients')}")

ok("no per-field SEC captions while pending",
   not any(_SEC_CAPTION in c.value for c in at.sidebar.caption))
ok("mega-firm banner withheld while pending",
   not any("above this calculator" in m.value for m in at.main.markdown))


# =============================================================================
# 2. Applied — provenance claims are now earned
# =============================================================================
print("\n-- applied --------------------------------------------------------")
at = fresh(
    sec_data=MEGA,
    pending_reconcile=False,
    # the values below ARE the SEC ones, so the derived gate reads 'applied'
    aum=f"{int(MEGA.aum):,}",
    num_clients=f"{int(MEGA.num_clients):,}",
    revenue="2,400,000,000",
    ebitda="600,000,000",
    purchase_price="4,800,000,000",
)
at.run()
no_exception("app renders with SEC data applied", at)

_text = sidebar_text(at)
ok("claims 'SEC applied' once the values are in", "SEC applied" in _text)
ok("no longer says not-applied", "not applied yet" not in _text)

_decoded = decoded_share_state(at)
ok("share link carries the applied AUM",
   _decoded is not None and _decoded.get("aum") == int(MEGA.aum),
   f"link aum={_decoded.get('aum') if _decoded else None}")
ok("share link carries the applied client count",
   _decoded is not None and _decoded.get("num_clients") == int(MEGA.num_clients))
ok("mega-firm banner shown once applied",
   any("above this calculator" in m.value for m in at.main.markdown))


# =============================================================================
# 3. Round trip — the link a recipient actually opens
# =============================================================================
print("\n-- round trip -----------------------------------------------------")
_url = sidebar_share_url(at)
ok("captured a share URL to round-trip", _url is not None)
if _url:
    at2 = AppTest.from_file(APP, default_timeout=180)
    at2.query_params["d"] = _url.split("?d=", 1)[1]
    at2.run()
    no_exception("recipient's session renders", at2)
    ok("recipient lands on the calculator, not the welcome page",
       ss(at2, "show_calculator") is True)
    ok("recipient's AUM matches the sender's",
       as_int(at2.session_state["aum"]) == int(MEGA.aum),
       f"got {at2.session_state['aum']}")
    ok("recipient's purchase price matches the sender's",
       as_int(at2.session_state["purchase_price"]) == 4_800_000_000,
       f"got {at2.session_state['purchase_price']}")
    ok("firm identity does NOT travel with the link",
       ss(at2, "sec_data") is None)


# =============================================================================
# 4. Clear / Cancel must not re-trigger the lookup (infinite-dialog guard)
# =============================================================================
print("\n-- clear does not loop ---------------------------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=True, sec_search="Edelman")
at.run()
no_exception("app renders with a live search query", at)

_clear = [b for b in at.sidebar.button if b.label == "Clear SEC data"]
ok("Clear button is reachable while pending", len(_clear) == 1)
if _clear:
    _clear[0].click().run()
    no_exception("clear runs cleanly", at)
    ok("clear drops the SEC record", at.session_state["sec_data"] is None)
    ok("clear drops the pending flag",
       at.session_state["pending_reconcile"] is False)
    ok("clear retires the search query", at.session_state["sec_search"] == "")
    ok("clear releases the re-open guard",
       ss(at, "last_loaded_crd") is None)

    # The loop: with the guard released, a surviving query would re-fire the
    # auto-lookup here and reopen the dialog the user just dismissed.
    at.run()
    no_exception("app is stable on the run after a clear", at)
    ok("cleared firm does not reload itself",
       at.session_state["sec_data"] is None,
       "auto-lookup re-fired after clear — dialog loop")
    ok("no dialog pending after a clear",
       at.session_state["pending_reconcile"] is False)


# =============================================================================
# 5. Declining every row closes the dialog having applied NOTHING
# =============================================================================
# Found by review. The first version of this fix gated everything on
# `not pending_reconcile`, and Apply cleared that flag unconditionally — so
# answering "Keep mine" to every row closed the dialog and lit the full green
# provenance set over a deal made entirely of the user's own numbers. Same bug
# as the original, one door further along. The gate is now "did a SEC value
# reach the widgets", which is the question that was always being asked.
print("\n-- declined every row ----------------------------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=True,
           # the user's OWN deal — so the memo has real content to mis-stamp
           aum="500,000,000", revenue="4,000,000", ebitda="1,600,000",
           owner_comp="500,000", num_clients="200", purchase_price="8,000,000")
at.run()
no_exception("dialog opens over an existing deal", at)

_radios = [r for r in at.radio if r.key and r.key.startswith("reconcile_radio_")]
ok("reconcile rows rendered", len(_radios) >= 1, f"found {len(_radios)}")
for _r in _radios:
    _r.set_value(_r.options[0])          # "Keep mine: ..." on every row
_apply = [b for b in at.button if b.label == "Apply selected"]
ok("Apply selected is reachable", len(_apply) == 1)
if _apply and _radios:
    _apply[0].click().run()
    no_exception("apply-with-nothing-selected runs cleanly", at)

    _text = sidebar_text(at)
    ok("declining everything does NOT claim 'SEC applied'",
       "SEC applied" not in _text)
    ok("declining everything keeps the not-applied chip",
       "not applied yet" in _text)
    ok("declining everything withholds the mega-firm banner",
       not any("above this calculator" in m.value for m in at.main.markdown))
    ok("declining everything leaves the firm off the memo",
       not firm_name_on_page(at))

    _decoded = decoded_share_state(at)
    ok("share link still carries the user's own AUM",
       _decoded is not None and _decoded.get("aum") == 500_000_000,
       f"link aum={_decoded.get('aum') if _decoded else None}")

    # And the way back: the per-field ↺ offers return once the dialog closes.
    _revert = [b for b in at.sidebar.button if b.key == "revert_aum"]
    ok("per-field SEC offer returns after the dialog closes", len(_revert) == 1)
    ok("the offer's caption is shown",
       any(_SEC_CAPTION in c.value for c in at.sidebar.caption))
    ok("a way back into the dialog is offered too",
       any(b.label == "Review & apply SEC values" for b in at.sidebar.button))

# Taking the revert offer runs from a fresh session seeded with the state the
# decline above produced. Continuing on the same AppTest object is not
# possible: its element tree still holds the closed dialog's radio nodes and
# reading their (now absent) state raises on the next run — a harness
# limitation, not app behaviour.
print("\n-- taking a per-field offer earns the claim -------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=False,
           aum="500,000,000", revenue="4,000,000", ebitda="1,600,000",
           owner_comp="500,000", num_clients="200", purchase_price="8,000,000")
at.run()
no_exception("post-decline state renders", at)
ok("starts without a provenance claim", "SEC applied" not in sidebar_text(at))
_revert = [b for b in at.sidebar.button if b.key == "revert_aum"]
ok("the per-field revert offer is present", len(_revert) == 1)
if _revert:
    _revert[0].click().run()
    no_exception("taking a per-field SEC value runs cleanly", at)
    ok("taking one SEC value earns the applied chip",
       "SEC applied" in sidebar_text(at), sidebar_text(at)[:160])
    ok("and it actually reached the widget",
       as_int(at.session_state["aum"]) == int(MEGA.aum),
       str(at.session_state["aum"]))
    _decoded = decoded_share_state(at)
    ok("and the share link carries it",
       _decoded is not None and _decoded.get("aum") == int(MEGA.aum),
       f"link aum={_decoded.get('aum') if _decoded else None}")


# =============================================================================
# 6. Applied state: the positive counterparts
# =============================================================================
# Paired with the negative assertions above. Without these, reverting a gate
# AND rewording the caption would leave the negatives passing on absence.
print("\n-- applied: positives ----------------------------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=False,
           aum=f"{int(MEGA.aum):,}", num_clients=f"{int(MEGA.num_clients):,}",
           revenue="2,400,000,000", ebitda="600,000,000",
           purchase_price="4,800,000,000")
at.run()
no_exception("applied state renders", at)
ok("per-field SEC caption DOES render once applied",
   any(_SEC_CAPTION in c.value for c in at.sidebar.caption),
   str([c.value for c in at.sidebar.caption][:6]))
ok("firm name DOES reach the memo once applied", firm_name_on_page(at))


# =============================================================================
# 6b. A second lookup, after a first one applied
# =============================================================================
# Found by review. The dialog read num_clients with a bare int(), but the
# Apply path writes it comma-formatted ("322,130") — so the first successful
# lookup poisoned every later one: the dialog raised, "Apply selected" never
# rendered, and the user was stranded. Worse, the per-field revert buttons are
# hidden while the dialog is pending, so the one-field-at-a-time way out was
# gone too. Applied state on the way in is what makes this test bite.
print("\n-- second lookup in a session ---------------------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=True,
           aum=f"{int(MEGA.aum):,}",
           num_clients=f"{int(MEGA.num_clients):,}",   # comma string, as applied
           revenue="2,400,000,000", ebitda="600,000,000")
at.run()
no_exception("dialog survives comma-formatted counts from a prior apply", at)
ok("Apply selected still renders on the second lookup",
   any(b.label == "Apply selected" for b in at.button),
   str([b.label for b in at.button][:8]))
ok("Cancel still renders on the second lookup",
   any(b.label == "Cancel" for b in at.button))


# =============================================================================
# 6c. Provenance must not outlive the values it describes
# =============================================================================
# Found in review, and the third variant of the same mistake: approximating
# "are the SEC values in the widgets?" with something cheaper. A flag set at
# apply-time survived the user editing the applied value straight back out,
# leaving a memo stamped with a firm whose numbers were gone. The gate is now
# derived from the widgets on every render, so there is no flag to go stale.
print("\n-- provenance does not outlive its values ---------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=False,
           aum=f"{int(MEGA.aum):,}",              # applied from SEC
           num_clients="200",                     # declined, user's own
           revenue="2,400,000,000", ebitda="600,000,000")
at.run()
no_exception("applied-one-field state renders", at)
ok("one applied field earns the claim", "SEC applied" in sidebar_text(at))
ok("and the firm reaches the memo", firm_name_on_page(at))

# Now the user overwrites the one field that came from SEC.
at.session_state["aum"] = "750,000,000"
at.run()
no_exception("renders after the applied value is edited away", at)
ok("editing the last SEC value away drops the claim",
   "SEC applied" not in sidebar_text(at), sidebar_text(at)[:170])
ok("and the firm leaves the memo", not firm_name_on_page(at))
ok("and the mega-firm banner goes with it",
   not any("above this calculator" in m.value for m in at.main.markdown))
_decoded = decoded_share_state(at)
ok("share link carries the user's number, not the SEC one",
   _decoded is not None and _decoded.get("aum") == 750_000_000,
   f"link aum={_decoded.get('aum') if _decoded else None}")


# =============================================================================
# 6d. A shared link opened into an already-applied session
# =============================================================================
# Same root cause, different door: apply_deal_from_url replaces every widget
# with someone else's deal but leaves sec_data alone, so a latch would have
# stamped THIS firm onto THAT deal.
print("\n-- shared link over an applied session ------------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=False, aum=f"{int(MEGA.aum):,}")
at.run()
ok("session starts with the firm applied", "SEC applied" in sidebar_text(at))
_url = sidebar_share_url(at)

at2 = fresh(sec_data=MEGA, pending_reconcile=False, aum=f"{int(MEGA.aum):,}")
at2.query_params["d"] = persistence.encode_deal_to_param(
    {"aum": 42_000_000, "revenue": 300_000, "ebitda": 90_000})
at2.run()
no_exception("restoring a shared deal over an applied session renders", at2)
ok("the shared deal's numbers win",
   as_int(ss(at2, "aum", "0")) == 42_000_000, str(ss(at2, "aum")))
ok("the previous firm does not stamp the shared deal",
   "SEC applied" not in sidebar_text(at2), sidebar_text(at2)[:170])
ok("and it does not reach the memo", not firm_name_on_page(at2))


# =============================================================================
# 6e. A firm whose SEC values coincide with the untouched defaults
# =============================================================================
# Negative AUM growth clamps to 0.0, which is exactly the growth slider's
# default — so for every firm with a flat or shrinking book, "applied" and
# "never touched" look identical on that field. Counting it as evidence
# resurrected the original bug for a large slice of real firms.
print("\n-- SEC values that collide with defaults ----------------------------")
SHRINKING = FirmData(
    crd=999001, firm_name="SHRINKING ADVISORS", aum=800_000_000.0,
    num_clients=1200, num_accounts=None, as_of_date="2026-04-30",
    aum_prior_year=920_000_000.0,      # declining -> growth clamps to 0.0
    registration_type="sec",
)
ok("setup: this firm's growth really does clamp to zero",
   SHRINKING.growth_rate is not None and SHRINKING.growth_rate < 0)

at = fresh(sec_data=SHRINKING, pending_reconcile=True)
at.run()
no_exception("shrinking-firm lookup renders", at)
# A SECOND run is what makes this bite. The chip renders above the growth
# slider, so on the first pass rev_growth_pct is not in session state yet and
# the comparison is skipped for a reason that has nothing to do with the fix.
# Every real interaction is a rerun, where the key exists and holds 0.0.
at.run()
no_exception("shrinking-firm rerun renders", at)
ok("setup: the rerun really does have the default in session state",
   ss(at, "rev_growth_pct") == 0.0, str(ss(at, "rev_growth_pct")))
ok("a zero-growth firm does not claim provenance on lookup",
   "SEC applied" not in sidebar_text(at), sidebar_text(at)[:170])
ok("it stays in the not-applied state", "not applied yet" in sidebar_text(at))
ok("and its name stays off the memo", not firm_name_on_page(at))

# The same firm, genuinely applied: AUM is non-zero, so it IS evidence.
at = fresh(sec_data=SHRINKING, pending_reconcile=False, aum="800,000,000")
at.run()
no_exception("shrinking-firm applied state renders", at)
ok("applying a real field still earns the claim",
   "SEC applied" in sidebar_text(at), sidebar_text(at)[:170])


# =============================================================================
# 7. Cancel resets the same guards as Clear
# =============================================================================
# Cancel was a copy of the Clear path with no test of its own: stripping its
# two reset lines left every suite green, while doing the same to Clear failed
# six assertions. One edit to one and not the other reopens the dialog loop.
print("\n-- cancel does not loop --------------------------------------------")
at = fresh(sec_data=MEGA, pending_reconcile=True, sec_search="Edelman")
at.run()
no_exception("dialog opens with a live query", at)
_cancel = [b for b in at.button if b.label == "Cancel"]
ok("Cancel is reachable in the dialog", len(_cancel) == 1)
if _cancel:
    _cancel[0].click().run()
    no_exception("cancel runs cleanly", at)
    ok("cancel drops the SEC record", ss(at, "sec_data") is None)
    ok("cancel drops the pending flag", ss(at, "pending_reconcile") is False)
    ok("cancel retires the search query", ss(at, "sec_search") == "")
    ok("cancel releases the re-open guard", ss(at, "last_loaded_crd") is None)
    # No second .run() here, unlike the Clear block above: re-running an
    # AppTest whose element tree still holds the dismissed dialog's
    # reconcile_radio_* nodes raises KeyError inside the harness. The
    # loop-prevention property is proven end-to-end by the Clear path, which
    # runs the identical two resets; what matters for Cancel is that it
    # performs both, which the four assertions above establish directly.


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    for label, detail in FAILURES:
        print(f"  - {label}" + (f": {detail}" if detail else ""))
    sys.exit(1)
print("ALL SHARE-PROVENANCE TESTS PASSED")
