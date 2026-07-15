"""
Headless tests for persistence.py — no Streamlit runtime required.

capture/restore take an injectable session dict / queue function, and the
codec + SQLiteStore are pure Python, so everything here runs with stdlib
only. Two ways to run (from ria_ma_calculator/):

    python -m pytest tests/test_persistence.py -q
    python tests/test_persistence.py            # plain-assert fallback
"""

import base64
import json
import math
import re
import shutil
import sys
import tempfile
import zlib
from pathlib import Path

# Make `import persistence` work from the tests/ subdir in both pytest and
# plain-python modes.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from persistence import (  # noqa: E402
    APP_BASE_URL,
    DEAL_STATE_KEYS,
    SQLiteStore,
    capture_deal_state,
    decode_deal_from_param,
    encode_deal_to_param,
    get_share_url,
    restore_deal_state,
    _MAX_ABS_VALUE as MAX_ABS_VALUE,
)


def _full_state() -> dict:
    """A realistic captured deal covering every manifest key (52)."""
    state = {
        # currency (captured as ints)
        "aum": 500_000_000, "revenue": 4_000_000, "ebitda": 1_200_000,
        "owner_comp": 450_000, "purchase_price": 8_000_000,
        "consulting_fee": 150_000, "noncompete": 100_000,
        "integration": 150_000, "synergies": 100_000,
        "buyer_aum": 1_000_000_000, "buyer_rev": 8_000_000,
        # count
        "num_clients": 350,
        # float
        "rev_growth_pct": 7.5, "attrition_rate": 4.5, "rev_multiple": 2.2,
        "note_rate": 5.25, "loan_rate": 6.5,
        "int_cost_takeout_pct": 0.14, "int_revenue_uplift_pct": 0.07,
        "int_capture_multiple": 1.25,
        # int
        "pct_recurring": 85, "pct_upfront_cash": 60, "pct_seller_note": 20,
        "pct_earnout": 15, "pct_equity_rollover": 5, "earnout_floor": 25,
        "earnout_cap": 125, "pct_self_funded": 50, "tax_rate": 32,
        "buyer_margin": 35, "note_term": 5, "note_standstill": 1,
        "earnout_period": 3, "loan_term": 7, "loan_io": 1,
        "loan_amort_yrs": 15, "consult_yrs": 2, "nc_yrs": 3,
        "additional_staff": 1, "integration_months": 12,
        "int_expense_base": 2_800_000,
        # bool
        "note_io": True, "earnout_cliff": False, "loan_balloon": True,
        "manual_integration": False, "show_combined": True,
        "int_buyer_compliance": True, "int_buyer_tech": True,
        "int_buyer_backoffice": True, "int_buyer_planning": False,
        # str
        "price_method": "Enter Price", "earnout_metric": "Revenue Retention",
    }
    assert set(state) == set(DEAL_STATE_KEYS), "test state must cover the manifest"
    return state


def _pipeline(obj) -> str:
    """Reproduce the encode pipeline for hand-built (possibly bad) envelopes."""
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


# ------------------------------------------------------------------------------
# Manifest
# ------------------------------------------------------------------------------
def test_manifest_shape():
    assert len(DEAL_STATE_KEYS) == 52
    allowed = {"currency", "count", "float", "int", "bool", "str"}
    assert set(DEAL_STATE_KEYS.values()) <= allowed
    # Spot-check the type calls that are easy to get wrong.
    assert DEAL_STATE_KEYS["loan_io"] == "int"          # years, not a checkbox
    assert DEAL_STATE_KEYS["consulting_fee"] == "currency"
    assert DEAL_STATE_KEYS["buyer_rev"] == "currency"
    assert DEAL_STATE_KEYS["int_cost_takeout_pct"] == "float"


# ------------------------------------------------------------------------------
# Codec round-trip
# ------------------------------------------------------------------------------
def test_roundtrip_full_state():
    state = _full_state()
    param = encode_deal_to_param(state)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", param), "param must be URL-safe, unpadded"
    assert len(param) < 1500, f"param too long for comfy URLs: {len(param)}"
    decoded = decode_deal_from_param(param)
    assert decoded == state
    url = get_share_url(state)
    assert url == f"{APP_BASE_URL}/?d={param}"
    print(f"[info] full 52-key state -> param {len(param)} chars, URL {len(url)} chars")


def test_roundtrip_partial_state():
    state = {"aum": 250_000_000, "price_method": "Select Multiple"}
    assert decode_deal_from_param(encode_deal_to_param(state)) == state


def test_malformed_params_return_none():
    valid = encode_deal_to_param(_full_state())
    bad = [
        None, 123, "", "   ",
        "!!!not/base64!!!",
        "abcd",                                   # decodes, but isn't zlib
        valid[: len(valid) // 2],                 # truncated stream
        base64.urlsafe_b64encode(b"not zlib").decode(),
        base64.urlsafe_b64encode(zlib.compress(b"not json", 9)).decode(),
        _pipeline([1, 2, 3]),                     # envelope not a dict
        _pipeline({"v": 1, "s": [1, 2]}),         # state not a dict
        _pipeline({"v": 2, "s": {}}),             # unknown schema version
        _pipeline({"s": {}}),                     # missing version
        "A" * 25_000,                             # over the length cap
    ]
    for param in bad:
        assert decode_deal_from_param(param) is None, f"expected None for {param!r:.60}"


# ------------------------------------------------------------------------------
# Capture (injectable session)
# ------------------------------------------------------------------------------
def test_capture_normalizes_and_filters():
    session = {
        "aum": "500,000,000",            # currency comma-string -> int
        "num_clients": "1,250",          # count comma-string -> int
        "rev_growth_pct": 7.5,
        "pct_recurring": 85,
        "note_io": True,
        "price_method": "Enter Price",
        "int_cost_takeout_pct": 0.14,
        "sec_search": "edelman",         # not in manifest -> ignored
        "_w_int_expense_base": 999,      # widget shadow -> ignored
        "ebitda": "garbage",             # unparseable -> skipped, not fatal
    }
    got = capture_deal_state(session)
    assert got["aum"] == 500_000_000 and isinstance(got["aum"], int)
    assert got["num_clients"] == 1_250
    assert got["rev_growth_pct"] == 7.5
    assert got["pct_recurring"] == 85
    assert got["note_io"] is True
    assert got["price_method"] == "Enter Price"
    assert got["int_cost_takeout_pct"] == 0.14
    for absent in ("sec_search", "_w_int_expense_base", "ebitda"):
        assert absent not in got


def test_capture_restore_symmetry():
    """capture -> restore queues exactly the widget-typed values."""
    session = {"aum": "12,345,678", "num_clients": "42", "show_combined": True}
    queued = {}
    n = restore_deal_state(capture_deal_state(session), queued.__setitem__)
    assert n == 3
    assert queued["aum"] == "12,345,678"     # back to the comma-string contract
    assert queued["num_clients"] == "42"
    assert queued["show_combined"] is True


# ------------------------------------------------------------------------------
# Restore coercion / clamping / mirrors
# ------------------------------------------------------------------------------
def test_restore_coercion_and_clamping():
    state = {
        "aum": 1_234_567,
        "revenue": "2,500,000",           # strings accepted too
        "num_clients": 350,
        "note_io": True,
        "earnout_cliff": 0,               # falsy -> False
        "rev_growth_pct": "7.5",
        "tax_rate": 400,                  # clamps to 50
        "attrition_rate": -3.0,           # clamps to 0.0
        "earnout_metric": "Bogus Metric", # not in options -> dropped
        "price_method": "Select Multiple",
        "loan_term": 10,
        "loan_amort_yrs": 5,              # below loan_term+1 -> bumped to 11
        "int_cost_takeout_pct": 0.14,
        "int_buyer_planning": False,
        "int_expense_base": 2_800_000,
        "ebitda": None,                   # junk -> skipped
        "unknown_key": 9,                 # not in manifest -> ignored
    }
    queued = {}
    n = restore_deal_state(state, queued.__setitem__)

    assert queued["aum"] == "1,234,567"
    assert queued["revenue"] == "2,500,000"
    assert queued["num_clients"] == "350"
    assert queued["note_io"] is True
    assert queued["earnout_cliff"] is False
    assert queued["rev_growth_pct"] == 7.5 and isinstance(queued["rev_growth_pct"], float)
    assert queued["tax_rate"] == 50 and type(queued["tax_rate"]) is int
    assert queued["attrition_rate"] == 0.0
    assert "earnout_metric" not in queued
    assert queued["price_method"] == "Select Multiple"
    assert queued["loan_amort_yrs"] == 11
    # Integration canonical + _w_ mirror double-queue, with x100 scaling.
    assert queued["int_cost_takeout_pct"] == 0.14
    assert queued["_w_int_cost_takeout_pct"] == 14.0
    assert queued["_w_int_buyer_planning"] is False
    assert queued["_w_int_expense_base"] == 2_800_000
    assert "ebitda" not in queued
    assert "unknown_key" not in queued
    # 14 canonical keys queued (mirrors don't count; 2 skipped; 1 unknown).
    assert n == 14


def test_restore_rejects_non_dict():
    queued = {}
    assert restore_deal_state(["not", "a", "dict"], queued.__setitem__) == 0
    assert restore_deal_state(None, queued.__setitem__) == 0
    assert queued == {}


# ------------------------------------------------------------------------------
# SQLiteStore CRUD
# ------------------------------------------------------------------------------
def test_non_finite_payload_is_refused():
    """A hand-built ?d= carrying Infinity/NaN must never reach restore.

    Python's json accepts the Infinity/-Infinity/NaN literals by default, and
    int(float("inf")) raises OverflowError deep inside restore_deal_state — so
    a crafted link would have crashed the app for whoever opened it, on a link
    they were told to trust. decode must reject the envelope outright, and
    restore must survive one even if it is handed one directly.
    """
    import base64 as _b64, json as _json, zlib as _zlib

    for literal in ("Infinity", "-Infinity", "NaN"):
        raw = '{"v": 1, "s": {"aum": %s}}' % literal
        # Sanity: this really is parseable JSON by default. If that ever stops
        # being true this test is guarding nothing and should be revisited.
        assert _json.loads(raw)["s"]["aum"] is not None
        param = _b64.urlsafe_b64encode(
            _zlib.compress(raw.encode("utf-8"), 9)
        ).decode("ascii").rstrip("=")
        assert decode_deal_from_param(param) is None, \
            f"{literal} payload was not refused"

    # The JSON *string* "nan" is not a JSON literal — parse_constant never sees
    # it, float() parses it happily, and _clamp_num cannot reject it because
    # every comparison against NaN is False. It reached the pro-forma engine and
    # crashed it. Guard the spelling explicitly, since it is the one that
    # actually shipped broken.
    raw = '{"v": 1, "s": {"attrition_rate": "nan"}}'
    param = _b64.urlsafe_b64encode(
        _zlib.compress(raw.encode("utf-8"), 9)
    ).decode("ascii").rstrip("=")
    queued = {}
    restore_deal_state(decode_deal_from_param(param) or {}, queued.__setitem__)
    assert "attrition_rate" not in queued, f'"nan" string reached the widget: {queued}'

    # PROPERTY, not spellings: no non-finite value may reach a widget for ANY
    # manifest key, however it is spelled, and restore must never raise. Asserting
    # only the spellings the fix implements is how "nan" got through the first
    # fix for the literal NaN.
    payloads = [
        # non-finite, every spelling
        "nan", "NaN", "NAN", "  nan  ", "-nan", "inf", "-inf", "+inf",
        "Infinity", "1e999", "1e309", 10 ** 4000, -(10 ** 4000),
        float("nan"), float("inf"), float("-inf"),
        # finite but astronomical — isfinite() says yes, Streamlit's number
        # widgets say StreamlitJSNumberBoundsError. Round 1 fixed the literal
        # NaN, round 2 found the string "nan", round 3 found THIS walking
        # through the same door. Same door, third spelling.
        1e308, -1e308, 1e300, 1e16, 2 ** 53 + 1, "1e308",
        "999999999999999999999",
    ]
    for key in DEAL_STATE_KEYS:
        for payload in payloads:
            q = {}
            try:
                restore_deal_state({key: payload}, q.__setitem__)
            except Exception as exc:  # noqa: BLE001 — a raise IS the failure
                raise AssertionError(
                    f"restore raised {type(exc).__name__} for {key}={payload!r:.20}"
                ) from exc
            for qk, qv in q.items():
                num = qv
                if isinstance(qv, str):
                    try:
                        num = float(qv.replace(",", ""))
                    except ValueError:
                        continue  # a legitimate non-numeric (enum) value
                if isinstance(num, (int, float)) and not isinstance(num, bool):
                    assert math.isfinite(num),                         f"{key}={payload!r:.20} queued non-finite {qk}={qv!r}"
                    assert abs(num) <= MAX_ABS_VALUE,                         f"{key}={payload!r:.20} queued out-of-bounds {qk}={qv!r}"

    # Legitimate restores must be completely unaffected by all of the above.
    queued = {}
    applied = restore_deal_state(
        {"aum": float("inf"), "revenue": float("nan"), "ebitda": 1_600_000,
         "attrition_rate": 5.0, "loan_term": 7},
        queued.__setitem__,
    )
    assert "aum" not in queued and "revenue" not in queued, queued
    assert queued.get("ebitda") == "1,600,000", queued
    assert queued.get("attrition_rate") == 5.0, queued
    assert queued.get("loan_term") == 7, queued
    assert applied == 3, applied

    # The ceiling must not reject legitimate scale: the largest RIAs on the ADV
    # file manage single-digit trillions. A guard that blocks real deals to stop
    # fake ones is not a guard, it is a bug.
    queued = {}
    restore_deal_state({"aum": 3_000_000_000_000, "revenue": 22_500_000_000},
                       queued.__setitem__)
    assert queued.get("aum") == "3,000,000,000,000", queued
    assert queued.get("revenue") == "22,500,000,000", queued


def test_sqlite_store_crud():
    tmpdir = tempfile.mkdtemp(prefix="deals_test_")
    try:
        store = SQLiteStore(db_path=Path(tmpdir) / "deals.db")
        state_1 = {"aum": 1, "price_method": "Enter Price"}

        id_1 = store.save("A@X.com", "Deal One", state_1)     # mixed case on purpose
        id_2 = store.save("a@x.com", "Deal Two", {"aum": 2})
        id_other = store.save("b@y.com", "Other Firm", {"aum": 3})

        # Email scoping + case-insensitivity.
        mine = store.list("a@x.com")
        assert {r["name"] for r in mine} == {"Deal One", "Deal Two"}
        assert all(set(r) == {"id", "name", "created_at", "updated_at"} for r in mine)
        assert {r["name"] for r in store.list("b@y.com")} == {"Other Firm"}
        assert store.list("nobody@nowhere.com") == []

        # Load round-trips the state dict; wrong email sees nothing.
        assert store.load(id_1, "a@x.com") == state_1
        assert store.load(id_1, "b@y.com") is None
        assert store.load("no-such-id", "a@x.com") is None

        # Rename, scoped by email.
        store.rename(id_2, "a@x.com", "Renamed Deal")
        assert {r["name"] for r in store.list("a@x.com")} == {"Deal One", "Renamed Deal"}
        store.rename(id_other, "a@x.com", "Hijack Attempt")   # no-op cross-email
        assert {r["name"] for r in store.list("b@y.com")} == {"Other Firm"}

        # Delete, scoped by email.
        store.delete(id_1, "b@y.com")                          # no-op cross-email
        assert store.load(id_1, "a@x.com") == state_1
        store.delete(id_1, "a@x.com")
        assert store.load(id_1, "a@x.com") is None
        assert {r["name"] for r in store.list("a@x.com")} == {"Renamed Deal"}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------------------
# Plain-assert fallback runner (when pytest isn't installed)
# ------------------------------------------------------------------------------
def _main() -> int:
    import traceback

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"PASS {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
