"""Tests for scripts/validate_adv_data.py — the publication gate on the SEC
Form ADV snapshot.

A gate that only proves "good data passes" is worthless: the whole point is
that it blocks the bad snapshot nobody is watching for at 3am on a cron. So
almost every test here builds a *specific corruption* — the ones SEC bulk data
actually does to you — and asserts the gate FAILS on it, by name.

Everything is synthetic and lives in a tempdir. Nothing here downloads, and
nothing here needs data/adv_current.parquet to exist.

Two ways to run (from ria_ma_calculator/):

    python -m pytest tests/test_adv_validation.py -q
    python tests/test_adv_validation.py            # plain-assert fallback
"""

import atexit
import contextlib
import io
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# Make `import validate_adv_data` work from tests/ in both pytest and plain
# python modes. The module lives in scripts/, not at the repo root.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import validate_adv_data as V  # noqa: E402
from validate_adv_data import FAIL, WARN, validate  # noqa: E402

# ------------------------------------------------------------------------------
# Module-level scratch space + synthetic snapshot builder
# ------------------------------------------------------------------------------
TMPDIR = Path(tempfile.mkdtemp(prefix="adv_validation_"))
atexit.register(shutil.rmtree, TMPDIR, ignore_errors=True)

# Comfortably over V.MIN_ROWS (8,000) without making the tests slow.
N_ROWS = 12_000
TODAY = date.today()


def _good_df(n: int = N_ROWS, seed: int = 20260725) -> pd.DataFrame:
    """A synthetic snapshot shaped like the real one: ~16K SEC registrants,
    lognormal AUM with a median in the hundreds of millions, filing dates
    clustered in the last few months with a small ancient tail, ~92%
    prior-year coverage. Deterministic for a given (n, seed)."""
    rng = np.random.default_rng(seed)

    aum = np.round(np.exp(rng.normal(np.log(4.5e8), 1.5, n)), 2)

    # Filing dates: bulk within ~5 months, plus a ~2% tail of stale filers
    # (the real file has rows going back to 2008).
    offsets = rng.integers(1, 150, n)
    tail = rng.choice(n, size=n // 50, replace=False)
    offsets[tail] = rng.integers(1500, 6000, len(tail))
    as_of = [(TODAY - timedelta(days=int(o))).isoformat() for o in offsets]

    clients = rng.integers(1, 5000, n).astype("float64")
    clients[rng.choice(n, size=max(n // 1000, 1), replace=False)] = np.nan

    prior = aum / (1.0 + rng.normal(0.08, 0.15, n))
    prior[rng.choice(n, size=int(n * 0.08), replace=False)] = np.nan

    return pd.DataFrame(
        {
            "crd": pd.array(np.arange(100_000, 100_000 + n), dtype="Int64"),
            "firm_name": [f"SYNTHETIC {i:06d} ADVISORS LLC" for i in range(n)],
            "aum": aum,
            "num_clients": clients,
            "num_accounts": np.round(clients * 1.4),
            "as_of_date": as_of,
            "aum_prior_year": np.round(prior, 2),
            "registration_type": "sec",
        }
    )[list(V.EXPECTED_COLUMNS)]


def _write(df: pd.DataFrame, name: str) -> Path:
    """Write a parquet into the module tempdir and return its path."""
    path = TMPDIR / f"{name}.parquet"
    df.to_parquet(path, compression="snappy", index=False)
    return path


def _minus_months(d: date, months: int) -> date:
    """Step back whole calendar months. Day is clamped to 28 because SEC
    snapshot filenames wander across the month end (ia043026 vs ia050125) and
    the gap is measured in calendar months, not days."""
    year, month = d.year, d.month - months
    while month < 1:
        month += 12
        year -= 1
    return date(year, month, min(d.day, 28))


def _manifest_for(df: pd.DataFrame, *, gap_months: int = 12,
                  exempt_status: str = "included", **overrides) -> dict:
    """A provenance sidecar that honestly describes `df`, shaped exactly like
    the one build_adv_data.py writes. Keyword overrides let a test corrupt one
    field at a time."""
    current = TODAY
    prior = _minus_months(current, gap_months)
    aum = pd.to_numeric(df["aum"], errors="coerce").dropna()
    payload = {
        "manifest_schema_version": V.MANIFEST_SCHEMA_VERSION,
        "built_at_utc": "2026-07-25T04:00:00+00:00",
        "parquet_path": "data/adv_current.parquet",
        "current_snapshot_date": current.isoformat(),
        "prior_snapshot_date": prior.isoformat(),
        "snapshot_gap_months": gap_months,
        "snapshot_gap_days": (current - prior).days,
        "exempt_status": exempt_status,
        "exempt_error": None,
        # One annual filing season apart -- the shape of the real Oct 2026 run.
        "filing_season": {
            "paired_firms": 14_599,
            "valid_gap_firms": 14_599,
            "unchanged_aum_share": 0.0074,
            "median_filing_gap_years": 1.018,
            "blanked_firms": 336,
            "blanked_share": 0.023,
        },
        "source_urls": {
            "current": "https://www.sec.gov/files/investment/data/ia063026.zip",
            "prior": "https://www.sec.gov/files/investment/data/ia063025.zip",
            "current_exempt": None,
            "listing": "https://www.sec.gov/data-research/sec-markets-data/x",
        },
        "row_count": int(len(df)),
        "total_aum": float(aum.sum()),
        "registration_type_counts": {"sec": int(len(df))},
    }
    payload.update(overrides)
    return payload


def _write_manifest(payload: dict, name: str) -> Path:
    path = TMPDIR / f"{name}.manifest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _failed(report) -> set:
    return {check for sev, check, _ in report.findings if sev == FAIL}


def _warned(report) -> set:
    return {check for sev, check, _ in report.findings if sev == WARN}


def _assert_blocks(report, check: str):
    """The gate must refuse to publish, and must name the right check."""
    assert not report.ok, f"expected FAIL, got a clean pass: {report.summary}"
    assert check in _failed(report), (
        f"expected check {check!r} to fail; failures were {sorted(_failed(report))}"
    )


# ------------------------------------------------------------------------------
# Happy path — the gate must not cry wolf
# ------------------------------------------------------------------------------
def test_happy_path_passes_clean():
    report = validate(_write(_good_df(), "good"))
    assert report.ok, f"clean data was blocked: {report.findings}"
    assert report.findings == [], f"clean data produced findings: {report.findings}"
    assert len(report.summary["checks_run"]) >= 15
    print(f"[info] happy path ran {len(report.summary['checks_run'])} checks clean")


def test_happy_path_with_realistic_prior():
    """A normal month: a few hundred new registrants, markets up ~5%."""
    good = _good_df()
    prior = good.iloc[: int(N_ROWS * 0.97)].copy()
    prior["aum"] = prior["aum"] * 0.95
    report = validate(_write(good, "hp_new"), _write(prior, "hp_prior"))
    assert report.ok, f"normal month-over-month drift was blocked: {report.findings}"
    assert report.findings == [], f"unexpected findings: {report.findings}"
    assert "crd_overlap_rate" in report.summary
    assert report.summary["crd_overlap_rate"] == 1.0


def test_summary_carries_what_ci_prints():
    report = validate(_write(_good_df(), "summary_src"))
    s = report.summary
    for key in (
        "row_count", "total_aum", "median_aum", "max_aum", "null_rates",
        "as_of_min", "as_of_max", "data_as_of", "prior_year_coverage_sec",
        "registration_type_counts", "file_bytes", "checks_run",
    ):
        assert key in s, f"summary missing {key!r}"
    assert s["row_count"] == N_ROWS
    assert s["data_as_of"] == s["as_of_max"]          # the "data as of" label
    assert set(s["null_rates"]) == set(V.EXPECTED_COLUMNS)
    # to_dict must survive json.dumps (numpy scalars everywhere otherwise).
    json.dumps(report.to_dict())


# ------------------------------------------------------------------------------
# The file itself: missing, stubbed, truncated
# ------------------------------------------------------------------------------
def test_missing_file_blocks():
    _assert_blocks(validate(TMPDIR / "does_not_exist.parquet"), "file_exists")


def test_tiny_stub_file_blocks():
    stub = TMPDIR / "stub.parquet"
    stub.write_bytes(b"PAR1" + b"\x00" * 500)
    report = validate(stub)
    _assert_blocks(report, "file_size")
    assert "readback" in _failed(report), "a garbage stub must also fail read-back"


def test_truncated_parquet_blocks_on_readback():
    """Half a download is still a big file — the footer is what's missing."""
    full = _write(_good_df(), "truncate_src").read_bytes()
    cut = TMPDIR / "truncated.parquet"
    cut.write_bytes(full[: int(len(full) * 0.6)])
    assert cut.stat().st_size > V.MIN_FILE_BYTES, "test needs a big-but-broken file"
    _assert_blocks(validate(cut), "readback")


def test_row_count_floor_blocks():
    _assert_blocks(validate(_write(_good_df(n=400), "tiny")), "row_count")


# ------------------------------------------------------------------------------
# Schema: missing, renamed, reordered, retyped
# ------------------------------------------------------------------------------
def test_missing_column_blocks():
    df = _good_df().drop(columns=["num_accounts"])
    report = validate(_write(df, "missing_col"))
    _assert_blocks(report, "schema_columns")
    assert "num_accounts" in report.failures[0][2]


def test_renamed_column_blocks():
    """The SEC renaming a field is the single most likely upstream break."""
    df = _good_df().rename(columns={"aum": "aum_total"})
    report = validate(_write(df, "renamed_col"))
    _assert_blocks(report, "schema_columns")
    msg = " ".join(m for _, _, m in report.failures)
    assert "aum" in msg and "aum_total" in msg


def test_reordered_columns_block():
    df = _good_df()
    df = df[["firm_name", "crd"] + [c for c in df.columns if c not in ("crd", "firm_name")]]
    report = validate(_write(df, "reordered"))
    _assert_blocks(report, "schema_columns")
    assert "canonical order" in report.failures[0][2]


def test_stringified_aum_blocks_on_dtype():
    """If _clean_number regresses, AUM arrives as "1,234,567" strings and
    sec_lookup's float(row["aum"]) raises for every single firm."""
    df = _good_df()
    df["aum"] = df["aum"].map(lambda v: f"{v:,.2f}")
    _assert_blocks(validate(_write(df, "str_aum")), "schema_dtypes")


def test_datetime_as_of_date_blocks_on_dtype():
    """The app prints as_of_date verbatim; a datetime64 column renders as
    "2026-04-30 00:00:00" in the UI."""
    df = _good_df()
    df["as_of_date"] = pd.to_datetime(df["as_of_date"])
    _assert_blocks(validate(_write(df, "dt_as_of")), "schema_dtypes")


# ------------------------------------------------------------------------------
# CRD — the app's only join key
# ------------------------------------------------------------------------------
def test_duplicate_crds_block():
    df = _good_df()
    df.loc[df.index[:50], "crd"] = df["crd"].iloc[0]
    _assert_blocks(validate(_write(df, "dup_crd")), "crd_unique")


def test_null_crds_block():
    df = _good_df()
    df.loc[df.index[:20], "crd"] = pd.NA
    report = validate(_write(df, "null_crd"))
    _assert_blocks(report, "crd_valid")
    assert "null_rates" in _failed(report), "a null CRD must also trip null_rates"


def test_nonpositive_crds_block():
    df = _good_df()
    df.loc[df.index[:5], "crd"] = 0
    df.loc[df.index[5:10], "crd"] = -42
    _assert_blocks(validate(_write(df, "neg_crd")), "crd_valid")


# ------------------------------------------------------------------------------
# firm_name — the only thing search_firms matches on
# ------------------------------------------------------------------------------
def test_blank_firm_names_block():
    df = _good_df()
    df.loc[df.index[:15], "firm_name"] = ""
    df.loc[df.index[15:30], "firm_name"] = "   "
    _assert_blocks(validate(_write(df, "blank_names")), "firm_name_blank")


def test_null_firm_names_block():
    df = _good_df()
    df.loc[df.index[:15], "firm_name"] = None
    _assert_blocks(validate(_write(df, "null_names")), "firm_name_blank")


# ------------------------------------------------------------------------------
# AUM — units, sign, and finiteness
# ------------------------------------------------------------------------------
def test_aum_scaled_up_1000x_blocks():
    """SEC switching Item 5 to a different unit keeps every row valid."""
    df = _good_df()
    df["aum"] = df["aum"] * 1000
    report = validate(_write(df, "aum_x1000"))
    _assert_blocks(report, "aum_bounds")
    assert "units" in " ".join(m for _, _, m in report.failures)


def test_aum_scaled_down_1000x_blocks():
    df = _good_df()
    df["aum"] = df["aum"] / 1000
    _assert_blocks(validate(_write(df, "aum_d1000")), "aum_bounds")


def test_aum_all_null_blocks():
    """Item 5 parsing dying entirely. build_adv_data's aum>0 filter would
    normally empty the file, but if that filter is what broke, this is what
    lands on disk."""
    df = _good_df()
    df["aum"] = np.nan
    report = validate(_write(df, "aum_null"))
    _assert_blocks(report, "aum_bounds")
    assert "null_rates" in _failed(report)


def test_aum_nonpositive_blocks():
    df = _good_df()
    df.loc[df.index[:30], "aum"] = 0.0
    df.loc[df.index[30:40], "aum"] = -5_000_000.0
    _assert_blocks(validate(_write(df, "aum_nonpos")), "aum_valid")


def test_aum_infinite_blocks():
    df = _good_df()
    df.loc[df.index[:5], "aum"] = np.inf
    _assert_blocks(validate(_write(df, "aum_inf")), "aum_valid")


# ------------------------------------------------------------------------------
# Client / account counts
# ------------------------------------------------------------------------------
def test_negative_counts_block():
    df = _good_df()
    df.loc[df.index[:25], "num_clients"] = -5.0
    df.loc[df.index[25:50], "num_accounts"] = -1.0
    _assert_blocks(validate(_write(df, "neg_counts")), "counts_valid")


def test_count_null_rate_blocks():
    df = _good_df()
    df.loc[df.index[: int(N_ROWS * 0.40)], "num_clients"] = np.nan
    _assert_blocks(validate(_write(df, "null_counts")), "null_rates")


# ------------------------------------------------------------------------------
# as_of_date — a stale listing must never publish as current
# ------------------------------------------------------------------------------
def test_future_dates_block():
    """A DD/MM-vs-MM/DD flip or a bad year pivot puts filings in the future."""
    df = _good_df()
    future = (TODAY + timedelta(days=30)).isoformat()
    df.loc[df.index[:100], "as_of_date"] = future
    _assert_blocks(validate(_write(df, "future_dates")), "as_of_future")


def test_stale_snapshot_blocks():
    df = _good_df()
    df["as_of_date"] = pd.to_datetime(df["as_of_date"]).map(
        lambda d: (d - pd.Timedelta(days=3 * 365)).date().isoformat()
    )
    report = validate(_write(df, "stale_dates"))
    _assert_blocks(report, "as_of_recency")
    assert "stale" in " ".join(m for _, _, m in report.failures)


def test_scattered_historical_dates_block():
    """Not stale at the top, but 60% of the file is years old — that is a
    concatenation of old rosters, not a current roster."""
    df = _good_df()
    old = [(TODAY - timedelta(days=1200 + i % 900)).isoformat()
           for i in range(int(N_ROWS * 0.6))]
    df.loc[df.index[: len(old)], "as_of_date"] = old
    _assert_blocks(validate(_write(df, "scattered_dates")), "as_of_recency")


def test_unparseable_dates_block():
    df = _good_df()
    df.loc[df.index[: int(N_ROWS * 0.10)], "as_of_date"] = "not-a-date"
    _assert_blocks(validate(_write(df, "bad_dates")), "as_of_parseable")


def test_wrong_snapshot_month_blocks_with_as_of_expected():
    """discover_snapshots picking a neighbouring month is invisible without
    telling the gate which month CI asked for."""
    path = _write(_good_df(), "as_of_expected_src")
    assert validate(path, as_of_expected=TODAY.isoformat()).ok
    report = validate(path, as_of_expected=(TODAY - timedelta(days=400)).isoformat())
    _assert_blocks(report, "as_of_expected")


def test_non_iso_dates_only_warn():
    """Ugly, not wrong: WARN must not block publication."""
    df = _good_df()
    df["as_of_date"] = pd.to_datetime(df["as_of_date"]).dt.strftime("%m/%d/%Y")
    report = validate(_write(df, "non_iso"))
    assert report.ok, f"a cosmetic format change blocked publication: {report.failures}"
    assert "as_of_format" in _warned(report)


# ------------------------------------------------------------------------------
# registration_type
# ------------------------------------------------------------------------------
def test_bad_registration_type_blocks():
    df = _good_df()
    df.loc[df.index[:10], "registration_type"] = "SEC"        # case matters
    df.loc[df.index[10:20], "registration_type"] = "federal"  # unknown source
    report = validate(_write(df, "bad_reg"))
    _assert_blocks(report, "registration_type")
    msg = " ".join(m for _, _, m in report.failures)
    assert "SEC" in msg and "federal" in msg


def test_null_registration_type_blocks():
    df = _good_df()
    df.loc[df.index[:10], "registration_type"] = None
    _assert_blocks(validate(_write(df, "null_reg")), "null_rates")


# ------------------------------------------------------------------------------
# aum_prior_year — the YoY-growth honesty check
# ------------------------------------------------------------------------------
def test_prior_year_coverage_collapse_blocks():
    """The prior-snapshot merge silently failing is the quietest of all the
    breaks: row count, AUM, names and dates are all untouched, and the app just
    stops showing growth rates."""
    df = _good_df()
    df.loc[df.index[: int(N_ROWS * 0.60)], "aum_prior_year"] = np.nan
    _assert_blocks(validate(_write(df, "no_prior_yr")), "prior_year_coverage")


def test_prior_year_coverage_sag_only_warns():
    df = _good_df()
    # ~75% coverage: worth noticing, not worth blocking a monthly refresh over.
    df["aum_prior_year"] = df["aum_prior_year"].where(
        np.arange(len(df)) % 4 != 0, np.nan
    )
    report = validate(_write(df, "prior_yr_sag"))
    assert report.ok, f"a 75% coverage sag blocked publication: {report.failures}"
    assert "prior_year_coverage" in _warned(report)


def test_prior_year_coverage_measured_on_sec_rows_only():
    """Exempt rows never carry aum_prior_year by construction, so a roster-mix
    change must not read as a merge failure."""
    df = _good_df()
    df.loc[df.index[: int(N_ROWS * 0.30)], "registration_type"] = "exempt"
    df.loc[df.index[: int(N_ROWS * 0.30)], "aum_prior_year"] = np.nan
    report = validate(_write(df, "exempt_mix"))
    assert report.ok, f"legitimate exempt rows blocked publication: {report.failures}"
    assert report.summary["prior_year_coverage_sec"] > 0.85
    assert report.summary["prior_year_coverage"] < 0.70


# ------------------------------------------------------------------------------
# Versus the prior published artifact
# ------------------------------------------------------------------------------
def test_row_count_collapse_vs_prior_blocks():
    prior = _good_df(n=14_000)
    new = prior.iloc[:12_000].copy()
    _assert_blocks(
        validate(_write(new, "collapse_new"), _write(prior, "collapse_prior")),
        "prior_row_delta",
    )


def test_row_count_explosion_vs_prior_blocks():
    prior = _good_df(n=12_000)
    new = _good_df(n=20_000)
    _assert_blocks(
        validate(_write(new, "explode_new"), _write(prior, "explode_prior")),
        "prior_row_delta",
    )


def test_aggregate_aum_collapse_vs_prior_blocks():
    """THE case row count cannot see: every row survives, only the magnitudes
    move. Halving AUM keeps the median inside the plausible band, so the
    standalone checks all pass — only the prior comparison catches it."""
    prior = _good_df()
    new = prior.copy()
    new["aum"] = new["aum"] * 0.5

    solo = validate(_write(new, "haircut_new"))
    assert solo.ok, f"standalone checks were supposed to miss this: {solo.failures}"

    report = validate(TMPDIR / "haircut_new.parquet", _write(prior, "haircut_prior"))
    _assert_blocks(report, "prior_aum_delta")
    assert "prior_row_delta" not in _failed(report), "row count should be untouched"
    assert "prior_crd_overlap" not in _failed(report), "CRDs should be untouched"


def test_aggregate_aum_explosion_vs_prior_blocks():
    prior = _good_df()
    new = prior.copy()
    new["aum"] = new["aum"] * 1.9   # under the x1000 units alarm, over the band
    report = validate(_write(new, "inflate_new"), _write(prior, "inflate_prior"))
    _assert_blocks(report, "prior_aum_delta")


def test_crd_overlap_collapse_vs_prior_blocks():
    """Same row count, same totals, but a different universe of firms — every
    shared/saved deal keyed on CRD would break."""
    prior = _good_df()
    new = prior.copy()
    new["crd"] = new["crd"] + 9_000_000
    report = validate(_write(new, "newcrd_new"), _write(prior, "newcrd_prior"))
    _assert_blocks(report, "prior_crd_overlap")
    assert "prior_row_delta" not in _failed(report)
    assert "prior_aum_delta" not in _failed(report)


def test_unreadable_prior_blocks():
    """Passing --prior and silently skipping the delta checks would be a gate
    that quietly got weaker on exactly the run nobody is watching."""
    bad_prior = TMPDIR / "bad_prior.parquet"
    bad_prior.write_bytes(b"not a parquet at all")
    _assert_blocks(
        validate(_write(_good_df(), "up_new"), bad_prior), "prior_readable"
    )


# ------------------------------------------------------------------------------
# Provenance manifest — the one break that is invisible inside the parquet
#
# `discover_snapshots(min_months_prior=12)` takes the NEWEST snapshot at least
# 12 months older than the current one. If the SEC prunes the 12-15-month-old
# files off its listing page, it silently settles for a ~24-month-old file, and
# `aum_prior_year` becomes a two-years-ago value that the app keeps rendering
# as a year-over-year growth rate. Every number in the parquet stays plausible,
# so only the sidecar can catch it.
# ------------------------------------------------------------------------------
def test_manifest_happy_path_passes_clean():
    df = _good_df()
    path = _write(df, "manifest_good")
    m = _write_manifest(_manifest_for(df), "manifest_good")
    report = validate(path, manifest_path=m)
    assert report.ok, f"an honest manifest was blocked: {report.findings}"
    assert report.findings == [], f"unexpected findings: {report.findings}"
    assert report.summary["snapshot_gap_months"] == 12
    assert report.summary["provenance_verified"] is True
    assert "12 month gap" in report.summary["provenance"]
    json.dumps(report.to_dict())


def test_manifest_snapshot_gap_alone_no_longer_blocks():
    """The rule that blocked the Oct 2026 refresh for nothing.

    ADV AUM is restated once a year, so a 16- or 24-month snapshot gap still
    compares two consecutive annual filings when the filing season says so.
    The gap is now a WARN; the filing-season check is the gate."""
    df = _good_df()
    path = _write(df, "gap_warn")
    for gap in (10, 14, 16, 24):
        m = _write_manifest(_manifest_for(df, gap_months=gap), f"gap_warn_{gap}")
        report = validate(path, manifest_path=m)
        assert report.ok, f"a {gap}-month gap blocked on its own: {report.failures}"
        assert "manifest_snapshot_gap" in {c for _, c, _ in report.findings}, (
            f"a {gap}-month gap should still be flagged for a human glance"
        )


def test_manifest_gap_within_one_month_of_twelve_is_silent():
    df = _good_df()
    path = _write(df, "gap_edges")
    for gap in (11, 12, 13):
        m = _write_manifest(_manifest_for(df, gap_months=gap), f"gap_ok_{gap}")
        report = validate(path, manifest_path=m)
        assert report.ok, f"a {gap}-month gap was blocked: {report.failures}"
        assert report.findings == [], report.findings
        assert report.summary["snapshot_gap_months"] == gap


def _season(**fs):
    base = {"paired_firms": 14_599, "valid_gap_firms": 14_599,
            "unchanged_aum_share": 0.0074, "median_filing_gap_years": 1.018,
            "blanked_firms": 336, "blanked_share": 0.023}
    base.update(fs)
    return base


def test_filing_season_same_season_blocks():
    """Real numbers from pairing May 2026 with Oct 2026: both inside one filing
    season, 94% of firms unchanged, the app would show ~0% growth for all."""
    df = _good_df()
    path = _write(df, "fs_same")
    payload = _manifest_for(df, filing_season=_season(
        paired_firms=15_877, unchanged_aum_share=0.9398, median_filing_gap_years=0.0))
    report = validate(path, manifest_path=_write_manifest(payload, "fs_same"))
    _assert_blocks(report, "manifest_filing_season")
    msg = " ".join(x for _, _, x in report.failures)
    assert "same annual filing season" in msg, msg
    assert _failed(report) == {"manifest_filing_season"}, sorted(_failed(report))
    assert report.summary["provenance_verified"] is False


def test_filing_season_two_seasons_apart_blocks():
    """Two annual filings back: growth would really be two-year growth."""
    df = _good_df()
    path = _write(df, "fs_two")
    payload = _manifest_for(df, gap_months=24,
                            filing_season=_season(median_filing_gap_years=2.01))
    _assert_blocks(validate(path, manifest_path=_write_manifest(payload, "fs_two")),
                   "manifest_filing_season")


def test_filing_season_edges():
    df = _good_df()
    path = _write(df, "fs_edges")
    for gap_years, should_pass in ((0.74, False), (0.75, True), (1.5, True), (1.51, False)):
        payload = _manifest_for(df, filing_season=_season(median_filing_gap_years=gap_years))
        report = validate(path, manifest_path=_write_manifest(payload, f"fs_e_{gap_years}"))
        assert report.ok is should_pass, (gap_years, report.failures)
    for share, should_pass in ((0.50, True), (0.51, False)):
        payload = _manifest_for(df, filing_season=_season(unchanged_aum_share=share))
        report = validate(path, manifest_path=_write_manifest(payload, f"fs_u_{share}"))
        assert report.ok is should_pass, (share, report.failures)


def test_filing_season_warns_before_it_blocks():
    df = _good_df()
    path = _write(df, "fs_warn")
    payload = _manifest_for(df, filing_season=_season(
        unchanged_aum_share=0.30, median_filing_gap_years=1.3))
    report = validate(path, manifest_path=_write_manifest(payload, "fs_warn"))
    assert report.ok, report.failures
    warned = [m for sev, c, m in report.findings if c == "manifest_filing_season"]
    assert len(warned) == 2, report.findings


def test_filing_season_missing_or_thin_blocks():
    df = _good_df()
    path = _write(df, "fs_missing")
    for label, fs in (("null", None), ("not a dict", "yes"),
                      ("thin pairing", _season(paired_firms=1_200)),
                      ("null share", _season(unchanged_aum_share=None)),
                      ("null gap", _season(median_filing_gap_years=None))):
        payload = _manifest_for(df, filing_season=fs)
        report = validate(path, manifest_path=_write_manifest(payload, f"fs_m_{label[:4]}"))
        assert "manifest_filing_season" in _failed(report), (label, report.failures)


def test_manifest_that_lies_about_its_own_gap_blocks():
    """A manifest whose recorded gap disagrees with its own snapshot dates is
    not evidence of anything."""
    df = _good_df()
    path = _write(df, "gap_liar")
    payload = _manifest_for(df, gap_months=24)   # dates really are 24 apart
    payload["snapshot_gap_months"] = 12          # ...but it claims 12
    report = validate(path, manifest_path=_write_manifest(payload, "gap_liar"))
    _assert_blocks(report, "manifest_snapshot_gap")
    assert report.summary["provenance_verified"] is False
    assert "internally inconsistent" in " ".join(x for _, _, x in report.failures)


def test_manifest_with_unknown_gap_blocks():
    """build_adv_data.py records null when it cannot parse a snapshot month out
    of the source URLs. Null is not "fine", it is "never checked"."""
    df = _good_df()
    path = _write(df, "gap_null")
    payload = _manifest_for(df)
    payload["snapshot_gap_months"] = None
    payload["current_snapshot_date"] = None
    payload["prior_snapshot_date"] = None
    _assert_blocks(
        validate(path, manifest_path=_write_manifest(payload, "gap_null")),
        "manifest_snapshot_gap",
    )


def test_manifest_row_count_mismatch_blocks():
    """A manifest left over from last month certifies a provenance this file
    does not have — worse than shipping no manifest at all."""
    df = _good_df()
    path = _write(df, "m_rows")
    m = _write_manifest(_manifest_for(df, row_count=N_ROWS - 500), "m_rows")
    report = validate(path, manifest_path=m)
    _assert_blocks(report, "manifest_matches_parquet")
    assert "different file" in " ".join(x for _, _, x in report.failures)


def test_manifest_total_aum_mismatch_blocks():
    df = _good_df()
    path = _write(df, "m_aum")

    payload = _manifest_for(df)
    payload["total_aum"] = payload["total_aum"] * 1.05
    _assert_blocks(
        validate(path, manifest_path=_write_manifest(payload, "m_aum_bad")),
        "manifest_matches_parquet",
    )

    # ...but float noise from the JSON round-trip must not block a good build.
    noise = _manifest_for(df)
    noise["total_aum"] = noise["total_aum"] * (1 + 1e-12)
    report = validate(path, manifest_path=_write_manifest(noise, "m_aum_noise"))
    assert report.ok, f"float round-trip noise blocked publication: {report.failures}"


def test_manifest_skipped_exempt_roster_warns_without_blocking():
    """The exempt download failing is swallowed on purpose — an exempt outage
    should not block a refresh of the 16K SEC registrants — but it must be
    visible in the CI log, because the published universe just got smaller."""
    df = _good_df()
    path = _write(df, "m_exempt")
    payload = _manifest_for(df, exempt_status="download_failed")
    payload["exempt_error"] = "HTTPError(404, 'Not Found')"
    report = validate(path, manifest_path=_write_manifest(payload, "m_exempt"))
    assert report.ok, f"a skipped exempt roster blocked publication: {report.failures}"
    assert "manifest_exempt_roster" in _warned(report)
    joined = " ".join(x for _, _, x in report.findings)
    assert "404" in joined and "download_failed" in joined


def test_malformed_manifest_blocks():
    """Same call as an unreadable --prior: asking for the check and silently
    not getting it is the failure mode this module exists to prevent."""
    df = _good_df()
    path = _write(df, "m_malformed")
    bad = TMPDIR / "broken.manifest.json"
    bad.write_bytes(b'{"manifest_schema_version": 1, "current_snapshot')
    _assert_blocks(validate(path, manifest_path=bad), "manifest_readable")


def test_missing_manifest_file_blocks():
    df = _good_df()
    path = _write(df, "m_missing")
    _assert_blocks(
        validate(path, manifest_path=TMPDIR / "not_written.manifest.json"),
        "manifest_readable",
    )


def test_manifest_missing_required_key_blocks():
    df = _good_df()
    path = _write(df, "m_partial")
    payload = _manifest_for(df)
    del payload["snapshot_gap_months"]
    report = validate(path, manifest_path=_write_manifest(payload, "m_partial"))
    _assert_blocks(report, "manifest_schema")
    assert "snapshot_gap_months" in " ".join(x for _, _, x in report.failures)


def test_manifest_unknown_schema_version_blocks():
    """If build_adv_data.py moved a field, no field below can be trusted."""
    df = _good_df()
    path = _write(df, "m_version")
    payload = _manifest_for(df, manifest_schema_version=V.MANIFEST_SCHEMA_VERSION + 1)
    _assert_blocks(
        validate(path, manifest_path=_write_manifest(payload, "m_version")),
        "manifest_schema",
    )


def test_absent_manifest_is_skipped_cleanly_but_said_out_loud():
    report = validate(_write(_good_df(), "m_absent"))
    assert report.ok, f"omitting --manifest must not block: {report.failures}"
    assert report.findings == []
    assert not [c for c in report.summary["checks_run"] if c.startswith("manifest_")]
    assert report.summary["provenance_verified"] is False
    assert "UNVERIFIED" in report.summary["provenance"]
    assert "UNVERIFIED" in report.render()
    assert "snapshot_gap_months" not in report.summary


def test_gap_is_surfaced_in_the_rendered_summary_every_run():
    """CI reads the rendered block, not the JSON. The gap has to be in it."""
    df = _good_df()
    path = _write(df, "m_render")
    report = validate(path, manifest_path=_write_manifest(_manifest_for(df), "m_render"))
    rendered = report.render()
    assert "snapshot_gap_months" in rendered
    assert "provenance" in rendered
    assert str(TODAY) in rendered


# -- the two modules have to agree on the contract, not just on paper ---------
def test_builder_and_gate_agree_on_the_manifest_contract():
    import build_adv_data as B  # pandas + stdlib only; importing downloads nothing

    assert B.MANIFEST_SCHEMA_VERSION == V.MANIFEST_SCHEMA_VERSION
    assert B.EXEMPT_INCLUDED == V.MANIFEST_EXEMPT_OK
    assert (
        B.manifest_path_for(Path("data/adv_current.parquet")).name
        == "adv_current.manifest.json"
    )
    # Month arithmetic must match the gate's recomputation exactly, or an
    # honest build trips the internal-consistency check.
    for gap in (11, 12, 13, 24):
        prior = _minus_months(TODAY, gap)
        assert B.months_between(
            datetime(prior.year, prior.month, prior.day),
            datetime(TODAY.year, TODAY.month, TODAY.day),
        ) == gap


def _snapshots(groups):
    """Synthetic (current, prior) snapshot frames from (n, gap_years, unchanged,
    missing_date) groups, shaped like build_adv_data.load_snapshot output."""
    from datetime import timedelta
    cur_rows, pri_rows, crd = [], [], 100_000
    base = date(2026, 3, 28)
    for n, gap_years, unchanged, missing in groups:
        for _ in range(n):
            crd += 1
            cur_date = base
            pri_date = base - timedelta(days=int(gap_years * 365.25))
            prior_aum = 400e6 + crd
            cur_aum = prior_aum if unchanged else prior_aum * 1.12
            cur_rows.append({"crd": crd, "aum": cur_aum,
                             "as_of_date": None if missing else cur_date.isoformat()})
            pri_rows.append({"crd": crd, "aum": prior_aum,
                             "as_of_date": pri_date.isoformat()})
    return pd.DataFrame(cur_rows), pd.DataFrame(pri_rows)


def test_builder_filing_season_stats_on_a_mixed_population():
    """The builder's own calculation, not precomputed numbers. A pairing that is
    one season apart for most firms but zero or two for some -- odd fiscal years,
    late filers -- must pass overall AND withhold growth for exactly the odd
    firms, because a population median cannot vouch for each firm."""
    import build_adv_data as B

    cur, pri = _snapshots([
        (8_000, 1.0, False, False),   # the ordinary Dec-FYE majority
        (300, 2.0, False, False),     # two filings back
        (150, 0.2, False, False),     # amendment shortly before annual filing
        (120, 1.0, True, False),      # same season -- AUM identical
        (60, 1.0, False, True),       # unusable filing date
    ])
    stats, blank = B.filing_season_stats(cur, pri)
    assert stats["paired_firms"] == 8_630, stats
    assert stats["valid_gap_firms"] == 8_570, stats
    assert 0.95 <= stats["median_filing_gap_years"] <= 1.05, stats
    assert stats["blanked_firms"] == 630 == len(blank), (stats, len(blank))
    odd = set(range(108_001, 108_631))          # every non-majority firm
    assert blank == odd, "blanked the wrong firms"

    # ...and the gate accepts it, because the odd firms are withheld, not shown.
    df = _good_df()
    path = _write(df, "builder_mixed")
    payload = _manifest_for(df, filing_season=stats)
    report = validate(path, manifest_path=_write_manifest(payload, "builder_mixed"))
    assert report.ok, report.failures


def test_builder_filing_season_stats_catch_a_same_season_pairing():
    import build_adv_data as B

    cur, pri = _snapshots([(9_000, 0.0, True, False), (600, 1.0, False, False)])
    stats, blank = B.filing_season_stats(cur, pri)
    assert stats["unchanged_aum_share"] > 0.9, stats
    assert stats["blanked_share"] > 0.9, stats
    df = _good_df()
    path = _write(df, "builder_same")
    payload = _manifest_for(df, filing_season=stats)
    _assert_blocks(validate(path, manifest_path=_write_manifest(payload, "builder_same")),
                   "manifest_filing_season")


def test_builder_filing_season_stats_mostly_missing_dates_blocks():
    """Codex's case: 9,999 unusable dates and one good one must not produce a
    confident median that passes."""
    import build_adv_data as B

    cur, pri = _snapshots([(9_999, 1.0, False, True), (1, 1.0, False, False)])
    stats, _ = B.filing_season_stats(cur, pri)
    assert stats["valid_gap_firms"] == 1, stats
    df = _good_df()
    path = _write(df, "builder_nodates")
    payload = _manifest_for(df, filing_season=stats)
    _assert_blocks(validate(path, manifest_path=_write_manifest(payload, "builder_nodates")),
                   "manifest_filing_season")


def test_valid_date_gate_stands_on_its_own():
    """In the real builder, missing dates are also blanked, so the blanked-share
    limit usually trips first. This pins the date gate by itself, so it cannot
    quietly rot behind that one: a manifest claiming few usable dates must
    block even when nothing else looks wrong."""
    df = _good_df()
    path = _write(df, "valid_gate")
    payload = _manifest_for(df, filing_season=_season(valid_gap_firms=1))
    report = validate(path, manifest_path=_write_manifest(payload, "valid_gate"))
    _assert_blocks(report, "manifest_filing_season")
    assert "usable filing dates" in " ".join(x for _, _, x in report.failures)


def test_filing_season_internal_inconsistency_blocks():
    df = _good_df()
    path = _write(df, "fs_incons")
    for label, fs in (
        ("negative share", _season(unchanged_aum_share=-0.1)),
        ("share over 1", _season(blanked_share=1.4)),
        ("valid > paired", _season(valid_gap_firms=20_000)),
        ("blanked > paired", _season(blanked_firms=20_000)),
        ("count vs share", _season(blanked_firms=5_000)),
    ):
        report = validate(path, manifest_path=_write_manifest(
            _manifest_for(df, filing_season=fs), f"fs_i_{label[:5]}"))
        assert "internally inconsistent" in " ".join(x for _, _, x in report.failures), (
            label, report.failures)


def test_real_builder_manifest_round_trips_through_the_gate():
    """End to end with the actual manifest writer and the actual stats
    function. Nothing here downloads."""
    import build_adv_data as B

    df = _good_df()
    path = _write(df, "roundtrip")
    base = "https://www.sec.gov/files/investment/data/"
    cur, pri = _snapshots([(9_000, 1.0, False, False)])
    season, _ = B.filing_season_stats(cur, pri)

    current_url = base + "ia063026.zip"
    prior_url = base + "ia063025.zip"
    payload = B.build_manifest(
        out_path=path,
        current_url=current_url,
        prior_url=prior_url,
        current_exempt_url=base + "ia063026-exempt.zip",
        current_date=B.snapshot_date_from_url(current_url),
        prior_date=B.snapshot_date_from_url(prior_url),
        exempt_status=B.EXEMPT_INCLUDED,
        exempt_error=None,
        frame=df,
        filing_season=season,
    )
    out = TMPDIR / "rt_good.manifest.json"
    B.write_manifest(out, payload)
    ok = validate(path, manifest_path=out)
    assert ok.ok, f"the real builder's manifest failed the gate: {ok.findings}"
    assert ok.summary["snapshot_gap_months"] == 12
    assert ok.summary["filing_blanked_share"] == 0.0


# ------------------------------------------------------------------------------
# Reporting behaviour
# ------------------------------------------------------------------------------
def test_every_violation_is_reported_not_just_the_first():
    df = _good_df()
    df.loc[df.index[:50], "crd"] = df["crd"].iloc[0]          # duplicate CRDs
    df.loc[df.index[60:80], "firm_name"] = ""                 # blank names
    df.loc[df.index[90:100], "registration_type"] = "bogus"   # bad enum
    df.loc[df.index[:120], "num_clients"] = -1.0              # negative counts
    report = validate(_write(df, "multi_broken"))

    expected = {"crd_unique", "firm_name_blank", "registration_type", "counts_valid"}
    assert expected <= _failed(report), (
        f"gate stopped early; only found {sorted(_failed(report))}"
    )
    rendered = report.render()
    for check in expected:
        assert check in rendered, f"{check} missing from the rendered report"
    print(f"[info] multi-corruption run surfaced {len(report.failures)} failures")


def test_report_shape():
    report = validate(_write(_good_df(n=400), "shape_src"))
    assert isinstance(report.ok, bool)
    for sev, check, msg in report.findings:
        assert sev in (FAIL, WARN)
        assert isinstance(check, str) and check
        assert isinstance(msg, str) and msg
    assert report.failures and not report.ok
    assert report.failed_checks == {c for _, c, _ in report.failures}


# ------------------------------------------------------------------------------
# CLI contract — exit codes are what the CI job actually branches on
# ------------------------------------------------------------------------------
def _run_cli(argv) -> tuple:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = V.main(argv)
    return code, buf.getvalue()


def test_cli_exit_zero_on_pass_and_writes_json():
    good = _write(_good_df(), "cli_good")
    out = TMPDIR / "report.json"
    code, printed = _run_cli(["--new", str(good), "--json", str(out)])
    assert code == 0, printed
    assert "PASS" in printed
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["row_count"] == N_ROWS
    assert payload["summary"]["data_as_of"]


def test_cli_exit_one_on_fail():
    df = _good_df()
    df["aum"] = df["aum"] * 1000
    bad = _write(df, "cli_bad")
    out = TMPDIR / "report_bad.json"
    code, printed = _run_cli(["--new", str(bad), "--json", str(out)])
    assert code == 1, printed
    assert "FAIL" in printed and "aum_bounds" in printed
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert any(f[1] == "aum_bounds" for f in payload["findings"])


def test_cli_exit_zero_when_only_warnings():
    df = _good_df()
    df["as_of_date"] = pd.to_datetime(df["as_of_date"]).dt.strftime("%m/%d/%Y")
    code, printed = _run_cli(["--new", str(_write(df, "cli_warn"))])
    assert code == 0, printed
    assert "WARNINGS" in printed


def test_cli_missing_file_exits_one():
    code, printed = _run_cli(["--new", str(TMPDIR / "nope.parquet")])
    assert code == 1
    assert "file_exists" in printed


def test_cli_manifest_flag_gates_the_filing_season():
    """The cron job's only lever is the exit code, so the flag has to be wired
    all the way through argparse."""
    df = _good_df()
    path = _write(df, "cli_manifest")

    same = _write_manifest(_manifest_for(df, filing_season={
        "paired_firms": 15_877, "unchanged_aum_share": 0.9398,
        "median_filing_gap_years": 0.0}), "cli_manifest_same")
    code, printed = _run_cli(["--new", str(path), "--manifest", str(same)])
    assert code == 1, printed
    assert "manifest_filing_season" in printed

    # A long snapshot gap alone exits 0, but is visible in the log.
    long_gap = _write_manifest(_manifest_for(df, gap_months=16), "cli_manifest_16")
    code, printed = _run_cli(["--new", str(path), "--manifest", str(long_gap)])
    assert code == 0, printed
    assert "16 months" in printed

    good = _write_manifest(_manifest_for(df), "cli_manifest_good")
    code, printed = _run_cli(["--new", str(path), "--manifest", str(good)])
    assert code == 0, printed
    assert "PASS" in printed
    assert "snapshot_gap_months" in printed   # in the log on a clean run too


def test_cli_without_manifest_says_provenance_is_unverified():
    code, printed = _run_cli(["--new", str(_write(_good_df(), "cli_no_manifest"))])
    assert code == 0, printed
    assert "UNVERIFIED" in printed


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
