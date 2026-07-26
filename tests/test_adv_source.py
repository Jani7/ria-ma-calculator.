"""The ADV fetch path — the code that only ever runs in production.

Locally there is a parquet on disk, so resolve() short-circuits at step 1 and
the download, validate and install logic never executes. That means every bug
in it ships blind. These tests drive it with an injected opener so the
production-only branches are exercised on every run.

What they pin down, in order of what would hurt most:

  * A refreshed snapshot at an UNCHANGED path must produce a changed version
    token, or the dataframe cache in app.py keeps serving last month's data
    for the life of the container.
  * A bad download must never replace a good cached file.
  * A failure must degrade to stale data, not to no data.
  * 304 must be a success (cache hit), not an error.

Run: python tests/test_adv_source.py   (from ria_ma_calculator/)
"""
import io
import os
import sys
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import adv_source

FAILURES = []


def ok(label, cond, detail=""):
    if cond:
        print(f"PASS  {label}")
    else:
        FAILURES.append((label, detail))
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))


GOOD_SECRETS = {"ADV_DATA_TOKEN": "ghp_fake_token_for_tests"}


def make_parquet(path: Path, rows=3, extra_col=True):
    df = pd.DataFrame({
        "crd": list(range(1, rows + 1)),
        "firm_name": [f"FIRM {i}" for i in range(1, rows + 1)],
        "aum": [1e9 * i for i in range(1, rows + 1)],
    })
    if extra_col:
        df["as_of_date"] = ["2026-04-30"] * rows
    df.to_parquet(path, index=False)
    return path.read_bytes()


class FakeResponse(io.BytesIO):
    def __init__(self, body, etag=""):
        super().__init__(body)
        self.headers = {"ETag": etag}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def opener_returning(body, etag="", capture=None):
    def _open(req, timeout=None):
        if capture is not None:
            capture.append(req)
        return FakeResponse(body, etag)
    return _open


def opener_raising(exc):
    def _open(req, timeout=None):
        raise exc
    return _open


def paths(tmp):
    d = Path(tmp)
    return {
        "local_path": d / "missing_local.parquet",   # absent on purpose
        "cache_path": d / "cache" / "adv_current.parquet",
        "etag_path": d / "cache" / "adv_current.etag",
    }


# =============================================================================
print("\n-- fetch and install ------------------------------------------------")
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    body = make_parquet(Path(tmp) / "src.parquet")
    captured = []
    r = adv_source.resolve(**p, secrets=GOOD_SECRETS,
                           opener=opener_returning(body, '"etag-v1"', captured))
    ok("fetch succeeds", r["status"] == "fetched", str(r))
    ok("cache file installed", p["cache_path"].exists())
    ok("etag recorded", p["etag_path"].read_text().strip() == '"etag-v1"')
    ok("version is the etag", r["version"] == '"etag-v1"', str(r.get("version")))
    ok("no .part debris left behind",
       not list(p["cache_path"].parent.glob("*.part")),
       str(list(p["cache_path"].parent.glob("*.part"))))
    # Lowercase BOTH sides. Written as "If-none-match" against lowercased keys
    # this could never match and so could never fail — a reviewer proved it by
    # making resolve() send the header unconditionally and watching it pass.
    _hdrs = {k.lower(): v for k, v in captured[0].headers.items()}
    ok("no If-None-Match on the first fetch", "if-none-match" not in _hdrs,
       str(_hdrs))
    ok("token sent as a bearer credential",
       _hdrs.get("authorization") == "Bearer " + GOOD_SECRETS["ADV_DATA_TOKEN"],
       str(_hdrs.get("authorization")))


# =============================================================================
print("\n-- a new snapshot at the same path changes the version --------------")
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    b1 = make_parquet(Path(tmp) / "v1.parquet", rows=3)
    r1 = adv_source.resolve(**p, secrets=GOOD_SECRETS,
                            opener=opener_returning(b1, '"etag-v1"'))
    b2 = make_parquet(Path(tmp) / "v2.parquet", rows=9)
    r2 = adv_source.resolve(**p, secrets=GOOD_SECRETS,
                            opener=opener_returning(b2, '"etag-v2"'))
    ok("same path both times", r1["path"] == r2["path"])
    ok("version changed with the bytes", r1["version"] != r2["version"],
       f"{r1['version']} vs {r2['version']}")
    ok("installed file is the NEW snapshot",
       len(pd.read_parquet(p["cache_path"])) == 9)


# =============================================================================
print("\n-- conditional re-check ---------------------------------------------")
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    body = make_parquet(Path(tmp) / "src.parquet")
    adv_source.resolve(**p, secrets=GOOD_SECRETS,
                       opener=opener_returning(body, '"etag-v1"'))
    captured = []
    adv_source.resolve(**p, secrets=GOOD_SECRETS,
                       opener=opener_returning(body, '"etag-v1"', captured))
    _hdrs = {k.lower(): v for k, v in captured[0].headers.items()}
    ok("second call sends If-None-Match",
       _hdrs.get("If-none-match".lower()) == '"etag-v1"', str(_hdrs))

    # GitHub answers 304 when unchanged — urllib raises that as HTTPError.
    r = adv_source.resolve(
        **p, secrets=GOOD_SECRETS,
        opener=opener_raising(urllib.error.HTTPError(
            "u", 304, "Not Modified", {}, None)))
    ok("304 is a cache hit, not a failure", r["status"] == "cache", str(r))
    ok("304 keeps serving the cached file", r["path"] == p["cache_path"])
    ok("304 keeps the version stable", r["version"] == '"etag-v1"')


# =============================================================================
print("\n-- a bad download must not poison a good cache ----------------------")
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    good = make_parquet(Path(tmp) / "good.parquet", rows=5)
    adv_source.resolve(**p, secrets=GOOD_SECRETS,
                       opener=opener_returning(good, '"etag-good"'))

    for label, junk in [
        ("html error page", b"<html>404 not found</html>"),
        ("truncated parquet", good[:len(good) // 2]),
        ("empty body", b""),
    ]:
        r = adv_source.resolve(**p, secrets=GOOD_SECRETS,
                               opener=opener_returning(junk, '"etag-bad"'))
        # Exactly "cache": with a good cache present that is the only correct
        # outcome. The looser `in ("cache", "bad_response")` accepted both
        # reachable results and so could not distinguish serving stale data
        # from serving none.
        ok(f"{label} rejected, cache still served", r["status"] == "cache", str(r))
        ok(f"{label}: good cache survives",
           len(pd.read_parquet(p["cache_path"])) == 5)
        ok(f"{label}: etag not advanced to the bad one",
           p["etag_path"].read_text().strip() == '"etag-good"')
        ok(f"{label}: no .part debris",
           not list(p["cache_path"].parent.glob("*.part")))

    # Schema drift is the quiet one: a perfectly valid parquet with the wrong
    # columns passes every byte-level check and then breaks every lookup.
    drifted = Path(tmp) / "drift.parquet"
    pd.DataFrame({"crd": [1], "name_of_firm": ["X"], "assets": [1.0]}).to_parquet(
        drifted, index=False)
    r = adv_source.resolve(**p, secrets=GOOD_SECRETS,
                           opener=opener_returning(drifted.read_bytes(), '"etag-drift"'))
    ok("schema drift rejected, cache still served", r["status"] == "cache", str(r))
    ok("schema drift: good cache survives",
       "firm_name" in pd.read_parquet(p["cache_path"]).columns)


# =============================================================================
print("\n-- a corrupt cache must not be pinned by its own ETag ---------------")
# Found in review. The cache file and its ETag sidecar are separate files, so
# they can disagree: if the parquet is truncated (disk full, killed mid-write,
# /tmp reaping a partial file) while the ETag survives, asking conditionally
# gets a 304 — "what you have is current" — and the old code served that
# corrupt file forever, since every later re-check asked the same question and
# got the same answer. Nothing could repair it short of a container restart.
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    good = make_parquet(Path(tmp) / "good.parquet", rows=6)
    adv_source.resolve(**p, secrets=GOOD_SECRETS,
                       opener=opener_returning(good, '"etag-good"'))

    # Corrupt the cache but leave the ETag intact.
    p["cache_path"].write_bytes(good[:len(good) // 3])
    ok("setup: cache is corrupt, etag survives",
       not adv_source.parquet_is_usable(p["cache_path"])
       and p["etag_path"].read_text().strip() == '"etag-good"')

    captured = []
    r = adv_source.resolve(**p, secrets=GOOD_SECRETS,
                           opener=opener_returning(good, '"etag-good"', captured))
    _hdrs = {k.lower(): v for k, v in captured[0].headers.items()}
    ok("does NOT ask conditionally when the cache is corrupt",
       "if-none-match" not in _hdrs, str(_hdrs))
    ok("re-download repairs the cache", r["status"] == "fetched", str(r))
    ok("repaired cache is usable again",
       adv_source.parquet_is_usable(p["cache_path"]))
    ok("repaired cache has the full row count",
       len(pd.read_parquet(p["cache_path"])) == 6)

# And the matching fallback rule: a corrupt cache is not something to serve
# when the remote is down either — no data beats data that fails at lookup.
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    good = make_parquet(Path(tmp) / "good.parquet", rows=6)
    adv_source.resolve(**p, secrets=GOOD_SECRETS,
                       opener=opener_returning(good, '"etag-good"'))
    p["cache_path"].write_bytes(good[:len(good) // 3])
    r = adv_source.resolve(**p, secrets=GOOD_SECRETS,
                           opener=opener_raising(urllib.error.URLError("down")))
    ok("corrupt cache is not served as a fallback",
       r["path"] is None and r["status"] == "network_error", str(r))


print("\n-- failures degrade to stale, not to nothing ------------------------")
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    good = make_parquet(Path(tmp) / "good.parquet", rows=4)
    adv_source.resolve(**p, secrets=GOOD_SECRETS,
                       opener=opener_returning(good, '"etag-good"'))

    for label, exc in [
        ("network error", urllib.error.URLError("down")),
        ("http 500", urllib.error.HTTPError("u", 500, "err", {}, None)),
        ("http 403", urllib.error.HTTPError("u", 403, "forbidden", {}, None)),
        ("unexpected exception", RuntimeError("boom")),
    ]:
        r = adv_source.resolve(**p, secrets=GOOD_SECRETS, opener=opener_raising(exc))
        ok(f"{label} falls back to cache", r["status"] == "cache", str(r))
        ok(f"{label} still returns a usable path", r["path"] == p["cache_path"])

with TemporaryDirectory() as tmp:
    p = paths(tmp)  # no cache to fall back to
    for label, exc, expected in [
        ("network error", urllib.error.URLError("down"), "network_error"),
        ("http 403", urllib.error.HTTPError("u", 403, "no", {}, None), "http_403"),
    ]:
        r = adv_source.resolve(**p, secrets=GOOD_SECRETS, opener=opener_raising(exc))
        ok(f"{label} with no cache reports the reason",
           r["status"] == expected and r["path"] is None, str(r))


# =============================================================================
print("\n-- credentials vanish while a good cache exists ----------------------")
# Found in review. These preflight failures used to return path=None directly,
# which was survivable when resolve() ran once per container. Under the
# re-check TTL it re-runs every 15 minutes against a container that already
# holds a good snapshot — so a single transient st.secrets read would take
# every SEC lookup down while a perfectly good parquet sat on disk. The token
# gates fetching, never reading what was already fetched and validated.
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    good = make_parquet(Path(tmp) / "good.parquet", rows=7)
    adv_source.resolve(**p, secrets=GOOD_SECRETS,
                       opener=opener_returning(good, '"etag-good"'))

    for label, sec in [
        ("token disappears", {}),
        ("secrets read returns nothing", {"ADV_DATA_TOKEN": ""}),
        ("repo misconfigured", {"ADV_DATA_TOKEN": "t", "ADV_DATA_REPO": "owner repo"}),
        ("ref misconfigured", {"ADV_DATA_TOKEN": "t", "ADV_DATA_REF": "main branch"}),
    ]:
        r = adv_source.resolve(**p, secrets=sec, opener=opener_raising(
            AssertionError("must not reach the network")))
        ok(f"{label}: keeps serving the cached snapshot",
           r["status"] == "cache" and r["path"] == p["cache_path"], str(r))
        # Guarded: on regression r["path"] is None, and read_parquet(None)
        # would abort the file and hide every assertion after this one.
        ok(f"{label}: the data is still usable",
           r["path"] is not None and len(pd.read_parquet(r["path"])) == 7,
           f"path={r['path']}")


print("\n-- config guards ----------------------------------------------------")
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    r = adv_source.resolve(**p, secrets={}, opener=opener_raising(AssertionError(
        "must not reach the network without a token")))
    ok("absent token short-circuits", r["status"] == "no_token", str(r))

    for bad in [{"ADV_DATA_TOKEN": "t", "ADV_DATA_REPO": "owner repo"},
                {"ADV_DATA_TOKEN": "t", "ADV_DATA_REPO": "../../evil"},
                {"ADV_DATA_TOKEN": "t", "ADV_DATA_REF": "main branch"}]:
        r = adv_source.resolve(**p, secrets=bad, opener=opener_raising(
            AssertionError("must not reach the network with bad config")))
        ok(f"rejects {list(bad.items())[1][0]}={list(bad.items())[1][1]!r}",
           r["status"] == "bad_config", str(r))

    # The earlier version of this asserted "Jani7" and "ghp_" were absent from
    # one status string. A reviewer enumerated the module's whole status
    # vocabulary by AST and showed no reachable value can structurally contain
    # either — it passed for every branch, broken or not. Drive every failure
    # mode instead and check the full returned dict, since that dict is what
    # gets logged.
    _leaky = {"ADV_DATA_TOKEN": "ghp_secret_do_not_log",
              "ADV_DATA_REPO": "SecretOwner/secret-data-repo"}
    _cases = [
        ("bad body", GOOD_SECRETS, opener_returning(b"x", "")),
        ("http 404", _leaky, opener_raising(
            urllib.error.HTTPError("u", 404, "no", {}, None))),
        ("network", _leaky, opener_raising(urllib.error.URLError("down"))),
        ("exotic", _leaky, opener_raising(RuntimeError("boom ghp_secret_do_not_log"))),
    ]
    for _label, _sec, _op in _cases:
        r = adv_source.resolve(**p, secrets=_sec, opener=_op)
        _blob = repr(r)
        ok(f"{_label}: result never carries the token",
           "ghp_secret_do_not_log" not in _blob and "ghp_" not in _blob, _blob)
        ok(f"{_label}: result never carries the private repo name",
           "SecretOwner" not in _blob and "secret-data-repo" not in _blob, _blob)


# =============================================================================
print("\n-- local dev file wins ----------------------------------------------")
with TemporaryDirectory() as tmp:
    d = Path(tmp)
    local = d / "local.parquet"
    make_parquet(local)
    r = adv_source.resolve(local_path=local,
                           cache_path=d / "cache" / "adv_current.parquet",
                           etag_path=d / "cache" / "adv_current.etag",
                           secrets=GOOD_SECRETS,
                           opener=opener_raising(AssertionError(
                               "must not hit the network with a local file")))
    ok("local file short-circuits the network", r["status"] == "local", str(r))

    # The version token exists so an in-place rebuild is picked up without a
    # restart. Asserting only its SHAPE never tested that claim, so rebuild the
    # file and prove the token actually moves.
    _v1 = r["version"]
    _t = local.stat().st_mtime
    make_parquet(local, rows=11)
    os.utime(local, (_t + 60, _t + 60))  # a rebuild a minute later
    _v2 = adv_source.resolve(local_path=local,
                             cache_path=d / "cache" / "adv_current.parquet",
                             etag_path=d / "cache" / "adv_current.etag",
                             secrets=GOOD_SECRETS,
                             opener=opener_raising(AssertionError("no network")))["version"]
    ok("rebuilding the local file changes its version token", _v1 != _v2,
       f"{_v1} vs {_v2}")


# =============================================================================
print("\n-- data_as_of -------------------------------------------------------")
_df = pd.DataFrame({"as_of_date": ["2026-01-31", "2026-04-30", "2025-12-31"]})
ok("picks the newest filing date", adv_source.data_as_of(_df) == "2026-04-30")
ok("empty frame is blank, not an error",
   adv_source.data_as_of(pd.DataFrame()) == "")
ok("missing column is blank, not an error",
   adv_source.data_as_of(pd.DataFrame({"x": [1]})) == "")
ok("None is blank, not an error", adv_source.data_as_of(None) == "")


print("\n-- hostile ETags and oversized bodies -------------------------------")
with TemporaryDirectory() as tmp:
    p = paths(tmp)
    good = make_parquet(Path(tmp) / "good.parquet", rows=4)

    # A header-splicing ETag must never be stored, and never replayed.
    for label, bad_etag in [
        ("newline", '"abc\r\nX-Injected: 1"'),
        ("unquoted", "abc123"),
        ("empty", ""),
        ("overlong", '"' + "a" * 500 + '"'),
    ]:
        _p2 = paths(tmp + label)
        r = adv_source.resolve(**_p2, secrets=GOOD_SECRETS,
                               opener=opener_returning(good, bad_etag))
        ok(f"{label} etag: fetch still succeeds", r["status"] == "fetched", str(r))
        ok(f"{label} etag: not stored verbatim",
           adv_source._read_etag(_p2["etag_path"]) == "",
           repr(adv_source._read_etag(_p2["etag_path"])))
        captured = []
        adv_source.resolve(**_p2, secrets=GOOD_SECRETS,
                           opener=opener_returning(good, bad_etag, captured))
        _h = {k.lower(): v for k, v in captured[0].headers.items()}
        ok(f"{label} etag: never replayed as a request header",
           "if-none-match" not in _h, str(_h))

    # An oversized body is refused rather than parsed or truncated.
    r = adv_source.resolve(**p, secrets=GOOD_SECRETS,
                           opener=opener_returning(b"P" * (64 * 1024 * 1024 + 10), '"e"'))
    ok("oversized body refused", r["status"] == "too_large", str(r))
    ok("oversized body not installed", not p["cache_path"].exists())


print("\n-- fmt_data_as_of ---------------------------------------------------")
ok("formats a real date", adv_source.fmt_data_as_of("2026-04-30") == "Apr 2026")
ok("January is not off by one", adv_source.fmt_data_as_of("2026-01-31") == "Jan 2026")
ok("December is not off by one", adv_source.fmt_data_as_of("2025-12-31") == "Dec 2025")
# Month "00" indexes _MONTH_ABBR[-1] without a range check, printing a corrupt
# date as a confident "Dec 2026" — the exact failure a freshness stamp exists
# to prevent.
ok("month 00 is blank, not 'Dec'", adv_source.fmt_data_as_of("2026-00-30") == "",
   adv_source.fmt_data_as_of("2026-00-30"))
ok("month 13 is blank", adv_source.fmt_data_as_of("2026-13-30") == "")
for _bad in ["", "not-a-date", "2026", "2026-04", None, 20260430, "26-04-30"]:
    ok(f"rejects {_bad!r}", adv_source.fmt_data_as_of(_bad) == "",
       repr(adv_source.fmt_data_as_of(_bad)))


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S)")
    for label, detail in FAILURES:
        print(f"  - {label}" + (f": {detail}" if detail else ""))
    sys.exit(1)
print("ALL ADV-SOURCE TESTS PASSED")
