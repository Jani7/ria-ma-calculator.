#!/usr/bin/env python3
"""Publication gate for data/adv_current.parquet.

`build_adv_data.py` scrapes the SEC's monthly Form ADV bulk snapshot and writes
a small Parquet. Once that job runs unattended on a cron, "the script exited 0"
stops meaning "the data is good": a renamed SEC column, a half-downloaded ZIP, a
units change on Item 5, or a stale listing page all produce a *syntactically
valid* Parquet that would ship straight to users. This module is the gate
between "written" and "published".

Usage (CI):

    python scripts/build_adv_data.py --out new.parquet
    python scripts/validate_adv_data.py \
        --new new.parquet --prior data/adv_current.parquet \
        --manifest new.manifest.json --json report.json
    # exit 0 -> commit/publish new.parquet;  exit 1 -> abort, keep the old one

Library:

    from validate_adv_data import validate
    report = validate("new.parquet", "prior.parquet")
    if not report.ok: ...

Two severities:
    FAIL  blocks publication (exit 1)
    WARN  printed, does not block (exit 0)

Dependencies are pandas + pyarrow + stdlib only. Deliberately no streamlit
import so this runs in a bare CI container.

The consumer contract this gate is protecting is `sec_lookup.py`:
`search_firms` does `int(row["crd"])` and `float(row["aum"])`, `get_firm_data`
does `df["crd"] == crd`, and `FirmData.growth_rate` divides by
`aum_prior_year`. Every check below traces back to one of those.

One class of break is invisible to any check that reads only the Parquet: the
file records what the numbers ARE, never where they came from. `--manifest`
reads build_adv_data.py's provenance sidecar and gates on the one fact the
artifact cannot carry --- how far apart the two SEC snapshots behind
`aum_prior_year` actually were.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

# =============================================================================
# TUNABLE THRESHOLDS
# -----------------------------------------------------------------------------
# Every number this gate judges by lives in this block and nowhere else. Each
# one is calibrated against the shipping snapshot (2026-04 vintage: 16,172 rows,
# median AUM $468M, max AUM $11.1T, total AUM $176.8T, 92.0% prior-year
# coverage, 99.7% of filing dates within 455 days of the newest one).
# The rule of thumb used throughout: set the FAIL line far enough from observed
# reality that normal month-to-month drift can never reach it, and close enough
# that a broken pipeline always does.
# =============================================================================

# --- Schema -----------------------------------------------------------------
# Exact column list and order written by build_adv_data.py `column_order`.
# Order matters as a change detector, not for correctness: if this list stops
# matching, build_adv_data.py changed and nobody told the consumer.
EXPECTED_COLUMNS: tuple[str, ...] = (
    "crd",
    "firm_name",
    "aum",
    "num_clients",
    "num_accounts",
    "as_of_date",
    "aum_prior_year",
    "registration_type",
)

# Dtype family per column. "integer"/"numeric"/"text" — see _DTYPE_PREDICATES.
#   crd -> integer:  build_adv_data casts .astype("Int64"). If it ever arrives as
#          object (e.g. zero-padded strings), `df["crd"] == crd` in
#          get_firm_data matches ZERO rows silently and every lookup in the app
#          returns "not found" with no error anywhere.
#   aum -> numeric:  sec_lookup calls float(row["aum"]); if _clean_number
#          regressed and left "1,234,567" strings, that raises ValueError.
#   as_of_date -> text: build_adv_data writes .dt.strftime("%Y-%m-%d") and the
#          app renders the value verbatim; a datetime64 column would surface as
#          "2026-04-30 00:00:00" in the UI.
_DTYPE_FAMILY: dict[str, str] = {
    "crd": "integer",
    "firm_name": "text",
    "aum": "numeric",
    "num_clients": "numeric",
    "num_accounts": "numeric",
    "as_of_date": "text",
    "aum_prior_year": "numeric",
    "registration_type": "text",
}

# --- Size / shape -----------------------------------------------------------
# A partially-written or empty artifact. The real file is ~694 KB; anything
# under 50 KB cannot be a full snapshot even at maximum compression.
MIN_FILE_BYTES = 50_000
# ~16.2K SEC registrants today, and that universe moves single-digit percent a
# year. Half the current count means truncation, not attrition.
MIN_ROWS = 8_000
# SEC-registered (~16K) + exempt reporters (~5K) + every state RIA (~17K) is
# ~38K in the most expansive future world. 60K means rows got duplicated.
MAX_ROWS = 60_000

# --- AUM plausibility -------------------------------------------------------
# Largest single RIA on the file reports ~$11.1T RAUM. $50T for one firm is
# larger than the entire US RIA industry; it is a units error, not a firm.
AUM_MAX_PLAUSIBLE = 5e13
# The robust units detector. Observed median is $468M. This band is ~100x wide
# in each direction — far beyond any real market move or mix shift — but a
# x1000 or /1000 units change on Item 5 5F(2)(c) blows straight through it.
# Median rather than mean because the mean is dominated by a handful of
# trillion-dollar firms.
AUM_MEDIAN_MIN = 5e7  # $50M
AUM_MEDIAN_MAX = 5e10  # $50B

# --- Null rates (fraction of rows, FAIL threshold per column) ---------------
# crd/firm_name/aum/registration_type are 0.0 because build_adv_data explicitly
# drops rows missing any of them — a single null means the filter broke.
# num_clients/num_accounts are 0.20: today they are ~0.0% null, but only full
# ADV filers carry Item 5, so a future roster-mix change could legitimately
# move this. Above 20% the app's client-count autofill is broken for a fifth of
# lookups, which is user-visible wrong-looking data.
# as_of_date 0.02 is a raw-null backstop; unparseable-but-present strings are
# owned by the as_of_parseable check instead.
# aum_prior_year is deliberately ABSENT: a flat whole-file null rate is the
# wrong instrument for it, because build_adv_data hard-sets it to NA on every
# exempt row, so a legitimate roster-mix change looks like a merge failure. The
# prior_year_coverage check below owns that column and scopes to SEC rows.
# Columns with no entry here are reported in the summary but not thresholded.
MAX_NULL_RATE: dict[str, float] = {
    "crd": 0.0,
    "firm_name": 0.0,
    "aum": 0.0,
    "num_clients": 0.20,
    "num_accounts": 0.20,
    "as_of_date": 0.02,
    "registration_type": 0.0,
}
# Warn once a column's null rate reaches this fraction of its FAIL line, so
# gradual degradation is visible in CI logs a month or two before it blocks.
NULL_RATE_WARN_FRACTION = 0.5

# --- Dates ------------------------------------------------------------------
# "Latest ADV Filing Date" can never be in the future. Two days of slack only
# for CI clock/timezone skew, not for real data.
FUTURE_GRACE_DAYS = 2
# The newest filing date on the file. The SEC publishes monthly and the newest
# filing is normally ~1-2 weeks behind the snapshot, so 180 days means we
# scraped a stale listing (or discover_snapshots picked the wrong link) and are
# about to publish last year's data as current.
MAX_SNAPSHOT_AGE_DAYS = 180
# Clustering: every SEC registrant must file an annual updating amendment
# within 90 days of fiscal year end, so essentially the whole file should sit
# within ~15 months of the newest filing date. Observed: 99.7%. A floor of 0.90
# ignores the genuine long tail of stragglers (the real file has rows back to
# 2008) while catching a snapshot that is half old data.
RECENT_WINDOW_DAYS = 455
RECENT_CLUSTER_MIN = 0.90
RECENT_CLUSTER_WARN = 0.95
# When the caller passes --as-of (the snapshot month CI *asked* for), the newest
# filing on the file must land within this many days of it. Observed gap: 11
# days. This is the only check that can catch discover_snapshots silently
# selecting a neighbouring month.
AS_OF_EXPECTED_TOLERANCE_DAYS = 45
# Cosmetic: build_adv_data writes ISO YYYY-MM-DD and the app prints it raw.
# Non-ISO is ugly, not wrong, so WARN.
ISO_DATE_MIN_RATE = 0.99

# --- Registration type ------------------------------------------------------
# build_adv_data only ever emits these three; sec_lookup.FirmMatch documents
# the same set. Anything else means an unreviewed source got merged in.
ALLOWED_REGISTRATION_TYPES = frozenset({"sec", "exempt", "state"})

# --- Prior-year AUM coverage (the YoY-honesty check) ------------------------
# FirmData.growth_rate is rendered in the app as a year-over-year number and is
# computed purely from aum_prior_year. If the prior-snapshot merge breaks (bad
# prior URL, CRD dtype mismatch, listing page missing old months) coverage
# collapses and the app silently stops showing growth for most firms.
# Observed 92.0% on SEC-registered rows. Measured on SEC rows only because
# build_adv_data hard-sets aum_prior_year = NA for every exempt row, so a
# roster-mix change would otherwise look like a merge failure.
PRIOR_YEAR_COVERAGE_MIN = 0.65
PRIOR_YEAR_COVERAGE_WARN = 0.85

# --- Versus the prior published artifact (--prior) --------------------------
# Row count: the registrant universe drifts ~1-2% a quarter. A 10% drop is
# 5-10x any real attrition; nothing legitimate shrinks this file that fast.
# Growth is allowed to be looser because adding a roster (exempt/state) is a
# real, intended way for the file to get bigger.
MAX_ROW_DROP = 0.10
WARN_ROW_DROP = 0.05
MAX_ROW_GROWTH = 0.25
WARN_ROW_GROWTH = 0.15
# Aggregate AUM: this is the check row count cannot do. A units error, or a
# column swap on Item 5, keeps every row and moves the total. Total industry
# RAUM fell ~15% peak-to-trough in 2022, so 30% is beyond any real market move
# between snapshots.
MAX_TOTAL_AUM_DROP = 0.30
WARN_TOTAL_AUM_DROP = 0.15
MAX_TOTAL_AUM_GROWTH = 0.60
WARN_TOTAL_AUM_GROWTH = 0.30
# Identity continuity: fraction of the prior file's CRDs still present. Real
# deregistrations run ~5-8%/yr. Losing 15% means the join key changed shape
# (string vs int, zero-padding) or we are looking at a different universe
# entirely — both of which break every saved/shared deal keyed on CRD.
CRD_OVERLAP_MIN = 0.85
CRD_OVERLAP_WARN = 0.93
# If --prior is passed but cannot be read, that is a FAIL rather than a skip:
# silently degrading to a weaker gate during an unattended run is the exact
# failure mode this module exists to prevent. Omit --prior to skip on purpose.

# --- Provenance manifest (--manifest) ---------------------------------------
# Every threshold above reads the Parquet, and the Parquet does not record
# which two SEC snapshots built it. That blind spot has a specific victim:
# `discover_snapshots(min_months_prior=12)` selects the NEWEST snapshot at
# least 12 months older than the current one, so if the SEC prunes the
# 12-15-month-old files off its listing page, the next eligible candidate is
# ~24 months old and `aum_prior_year` silently becomes a TWO-year-ago value
# while the app keeps labelling FirmData.growth_rate a "YoY AUM proxy".
# Nothing else here can see it: coverage stays ~92%, row counts, medians,
# totals and CRD overlap are all untouched, and the numbers are real --- just
# answering a different question than the label claims. The sidecar
# build_adv_data.py writes is the only artifact that knows, so these checks
# exist to make an unattended cron trip over it.
MANIFEST_SCHEMA_VERSION = 2
# SEC snapshots are monthly, so the legitimate current<->prior gap is 12 months
# plus or minus exactly one monthly file:
#   12  the intended pairing;
#   13  routine --- discover_snapshots takes the newest file >=12 months back,
#       so whenever the exact-12 file is absent (or its day-of-month lands a
#       few days on the wrong side of the cutoff) the next one back is 13;
#   11  reachable via a hand-passed --prior-url pair, and the slack that keeps
#       day-of-month drift from turning a real 12 into an off-by-one, since
#       the gap is counted in whole calendar months.
# The failure being caught is 24, which misses this window by 11 months --- so
# widening the tolerance a little would not weaken the check, and tightening it
# to exactly 12 would fail legitimate months. Anything at 14+ is not "SEC was
# a bit late", it is a hole in the listing page and must be looked at.
#
# CORRECTION (2026-10-18): the premise above was wrong, and it blocked the
# Oct 2026 refresh for nothing. Form ADV AUM is restated once a year, in the
# annual amendment, and the monthly SEC files repeat it in between. So the
# snapshot gap is NOT the growth period: an Oct 2026 file paired with Jun 2025
# (16 months) or Dec 2025 (10 months) compares the same two annual filings and
# gave 14.6% vs 14.3% median growth. What decides honesty is whether the two
# snapshots straddle exactly one filing season -- the filing-season check
# below. The snapshot gap is kept as a WARN, because a gap far from 12 is still
# worth a human glance, but it no longer blocks publication on its own.
SNAPSHOT_GAP_MONTHS_TARGET = 12
SNAPSHOT_GAP_MONTHS_TOLERANCE = 1

# --- Filing season (the check that replaced the gap rule) -------------------
# From build_adv_data.filing_season_stats, over firms present in both files:
#   unchanged_aum_share      same season -> most AUM identical. Observed: 0.7-1%
#                            across a real season, 94% within one season (May
#                            vs Oct 2026). 50% sits far from both.
#   median_filing_gap_years  ~1.0 across one season (observed 0.99 and 1.02),
#                            ~2.0 across two. The FAIL band is wide enough for
#                            fiscal-year-end and amendment timing, and nowhere
#                            near either failure.
#   paired_firms             a pairing that barely overlaps cannot certify
#                            anything; CRD overlap is ~15K in practice.
FILING_UNCHANGED_MAX = 0.50
FILING_UNCHANGED_WARN = 0.15
FILING_GAP_YEARS_MIN = 0.75
FILING_GAP_YEARS_MAX = 1.50
FILING_GAP_YEARS_WARN_LO = 0.85
FILING_GAP_YEARS_WARN_HI = 1.20
FILING_PAIRED_MIN = 5_000
# Firms whose OWN pairing failed the per-firm test in build_adv_data (their
# growth is blanked rather than mislabelled). Observed 2.3% on the real Oct
# 2026 pairing; a quarter of the file means the pairing itself is wrong.
FILING_BLANKED_MAX = 0.25
FILING_BLANKED_WARN = 0.10
# The manifest must describe THIS Parquet. A manifest left over from a previous
# run, or copied from another artifact, is worse than no manifest at all: it
# certifies a provenance the file does not have. Row count must match exactly;
# total AUM is a float that round-trips through JSON and is re-summed by a
# different code path, so it gets a relative tolerance --- a genuinely
# different file is off by percent, never by 1e-9.
MANIFEST_TOTAL_AUM_RTOL = 1e-6
# A --manifest that is missing, unparseable, of an unknown schema version, or
# short a required field is a FAIL rather than a skip, for exactly the reason
# --prior is: asking for the check and silently not getting it is how an
# unattended run gets weaker without anyone noticing. Omit --manifest to skip
# on purpose; the report then says provenance was UNVERIFIED, out loud.
MANIFEST_REQUIRED_KEYS: tuple[str, ...] = (
    "manifest_schema_version",
    "built_at_utc",
    "current_snapshot_date",
    "prior_snapshot_date",
    "snapshot_gap_months",
    "filing_season",
    "exempt_status",
    "source_urls",
    "row_count",
    "total_aum",
)
# build_adv_data.py's EXEMPT_INCLUDED. Any other status means the published
# file covers a smaller universe than usual --- worth saying in the CI log,
# not worth blocking a refresh of the 16K SEC registrants over, so WARN.
MANIFEST_EXEMPT_OK = "included"

# =============================================================================
# End of tunable thresholds.
# =============================================================================

FAIL = "FAIL"
WARN = "WARN"

# Checks owned by the provenance manifest. If any of them failed, the summary
# says so instead of quoting a gap the gate does not stand behind.
_MANIFEST_CHECKS = frozenset(
    {
        "manifest_readable",
        "manifest_schema",
        "manifest_snapshot_gap",
        "manifest_filing_season",
        "manifest_matches_parquet",
    }
)

_DTYPE_PREDICATES = {
    "integer": lambda dt: pd.api.types.is_integer_dtype(dt),
    "numeric": lambda dt: (
        pd.api.types.is_numeric_dtype(dt) and not pd.api.types.is_bool_dtype(dt)
    ),
    "text": lambda dt: (
        pd.api.types.is_object_dtype(dt) or isinstance(dt, pd.StringDtype)
    ),
}


# -----------------------------------------------------------------------------
# Report
# -----------------------------------------------------------------------------
@dataclass
class ValidationReport:
    """Result of a validation run.

    ok        -- False if any FAIL was recorded. This is the publish/abort bit.
    findings  -- [(severity, check_name, message)] for every violated check.
                 Passing checks are not listed here; see summary["checks_run"].
    summary   -- stats the gate computed. CI prints this, and it doubles as the
                 "data as of" source for the app footer (summary["data_as_of"]).
    """

    ok: bool = True
    findings: list[tuple[str, str, str]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    # -- recording -------------------------------------------------------
    def _ran(self, check: str) -> None:
        checks = self.summary.setdefault("checks_run", [])
        if check not in checks:
            checks.append(check)

    def fail(self, check: str, message: str) -> None:
        self._ran(check)
        self.ok = False
        self.findings.append((FAIL, check, message))

    def warn(self, check: str, message: str) -> None:
        self._ran(check)
        self.findings.append((WARN, check, message))

    # -- reading ---------------------------------------------------------
    @property
    def failures(self) -> list[tuple[str, str, str]]:
        return [f for f in self.findings if f[0] == FAIL]

    @property
    def warnings(self) -> list[tuple[str, str, str]]:
        return [f for f in self.findings if f[0] == WARN]

    @property
    def failed_checks(self) -> set[str]:
        return {c for _, c, _ in self.failures}

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def render(self) -> str:
        """Human-readable block for the CI log. Prints EVERY violation."""
        lines: list[str] = []
        lines.append("=" * 72)
        lines.append("ADV snapshot validation: " + ("PASS" if self.ok else "FAIL"))
        lines.append("=" * 72)

        s = self.summary
        lines.append("Summary")
        for key in (
            "new_path", "file_bytes", "row_count", "crd_unique",
            "data_as_of", "as_of_min", "as_of_max", "as_of_median",
            "total_aum", "median_aum", "max_aum",
            "prior_year_coverage_sec", "registration_type_counts",
        ):
            if key in s:
                lines.append(f"  {key:<26} {_fmt(s[key])}")
        if "null_rates" in s:
            lines.append("  null_rates")
            for col, rate in s["null_rates"].items():
                lines.append(f"    {col:<24} {rate:.4%}")
        # Provenance goes in every run's log, verified or not: the whole point
        # is that a 24-month "year-over-year" gap looks fine everywhere else.
        for key in (
            "provenance", "snapshot_current_date", "snapshot_prior_date",
            "snapshot_gap_months", "exempt_status", "manifest_path",
        ):
            if key in s:
                lines.append(f"  {key:<26} {_fmt(s[key])}")
        for key in (
            "prior_path", "prior_row_count", "row_count_delta_pct",
            "prior_total_aum", "total_aum_delta_pct", "crd_overlap_rate",
        ):
            if key in s:
                lines.append(f"  {key:<26} {_fmt(s[key])}")

        if self.failures:
            lines.append("")
            lines.append(f"FAILURES ({len(self.failures)}) - publication blocked")
            for _, check, msg in self.failures:
                lines.append(f"  [FAIL] {check}: {msg}")
        if self.warnings:
            lines.append("")
            lines.append(f"WARNINGS ({len(self.warnings)}) - not blocking")
            for _, check, msg in self.warnings:
                lines.append(f"  [WARN] {check}: {msg}")
        if not self.findings:
            lines.append("")
            lines.append(f"All {len(s.get('checks_run', []))} checks passed.")

        lines.append("=" * 72)
        return "\n".join(lines)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        if abs(v) >= 1e6:
            return f"{v:,.0f}"
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _jsonable(obj: Any) -> Any:
    """Make numpy / pandas scalars survive json.dump."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return f if np.isfinite(f) else None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float):
        return obj if np.isfinite(obj) else None
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return str(obj)
    if obj is pd.NaT or obj is pd.NA:
        return None
    return obj


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
def _numeric(df: pd.DataFrame, col: str) -> Optional[pd.Series]:
    """Column as float64, coercing junk to NaN. None if the column is absent."""
    if col not in df.columns:
        return None
    s = df[col]
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        return s.astype("float64")
    return pd.to_numeric(s, errors="coerce").astype("float64")


def _to_datetime(s: pd.Series) -> pd.Series:
    """Parse to datetime64, NaT on junk, without pandas' inference chatter."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return pd.to_datetime(s, errors="coerce")
        except Exception:  # noqa: BLE001 - genuinely mixed junk
            return pd.to_datetime(s, errors="coerce", format="mixed")


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _as_number(v: Any) -> Optional[float]:
    """JSON scalar -> finite float, or None. `bool` is rejected: True would
    otherwise sail through as 1 and quietly satisfy a numeric field."""
    if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)):
        return None
    f = float(v)
    return f if np.isfinite(f) else None


def _as_int(v: Any) -> Optional[int]:
    """JSON scalar -> int, or None if it is not a whole finite number."""
    f = _as_number(v)
    if f is None or f % 1 != 0:
        return None
    return int(f)


def _iso_date(v: Any) -> Optional[date]:
    """"YYYY-MM-DD" -> date, None on anything else."""
    if not isinstance(v, str) or not v:
        return None
    try:
        return pd.Timestamp(v).date()
    except Exception:  # noqa: BLE001 - any junk string
        return None


# -----------------------------------------------------------------------------
# Checks. Each takes (df, report) and records findings. Every one guards on
# column presence so a broken schema does not stop the rest from running --
# the operator must see the full list of what is wrong in one CI run.
# -----------------------------------------------------------------------------
def _check_schema(df: pd.DataFrame, r: ValidationReport) -> None:
    r._ran("schema_columns")
    actual = tuple(df.columns)
    if actual != EXPECTED_COLUMNS:
        missing = [c for c in EXPECTED_COLUMNS if c not in actual]
        extra = [c for c in actual if c not in EXPECTED_COLUMNS]
        bits = []
        if missing:
            bits.append(f"missing={missing}")
        if extra:
            bits.append(f"unexpected={extra}")
        if not missing and not extra:
            bits.append("columns present but out of canonical order")
        r.fail(
            "schema_columns",
            f"column list does not match build_adv_data.py; {'; '.join(bits)}. "
            f"expected={list(EXPECTED_COLUMNS)} actual={list(actual)}",
        )

    r._ran("schema_dtypes")
    for col, family in _DTYPE_FAMILY.items():
        if col not in df.columns:
            continue
        dt = df[col].dtype
        if not _DTYPE_PREDICATES[family](dt):
            r.fail(
                "schema_dtypes",
                f"{col!r} has dtype {dt!r}, expected a {family} dtype "
                f"(sec_lookup.py depends on this)",
            )


def _check_row_count(df: pd.DataFrame, r: ValidationReport) -> None:
    r._ran("row_count")
    n = len(df)
    if n < MIN_ROWS:
        r.fail(
            "row_count",
            f"{n:,} rows is below the floor of {MIN_ROWS:,} - the snapshot is "
            f"truncated or the Item 5 AUM filter dropped everything",
        )
    elif n > MAX_ROWS:
        r.fail(
            "row_count",
            f"{n:,} rows exceeds the ceiling of {MAX_ROWS:,} - rows were "
            f"likely concatenated twice",
        )


def _check_crd(df: pd.DataFrame, r: ValidationReport) -> None:
    if "crd" not in df.columns:
        return
    r._ran("crd_valid")
    r._ran("crd_unique")
    crd = _numeric(df, "crd")

    n_null = int(crd.isna().sum())
    if n_null:
        # search_firms does int(row["crd"]); pd.NA there raises TypeError and
        # takes the whole firm-picker down.
        r.fail("crd_valid", f"{n_null:,} rows have a null CRD")

    n_nonpos = int((crd.dropna() <= 0).sum())
    if n_nonpos:
        r.fail("crd_valid", f"{n_nonpos:,} rows have a non-positive CRD")

    n_nonint = int((crd.dropna() % 1 != 0).sum())
    if n_nonint:
        r.fail("crd_valid", f"{n_nonint:,} rows have a non-integral CRD")

    dupes = int(df["crd"].duplicated(keep=False).sum())
    if dupes:
        # get_firm_data takes .iloc[0] of the match, so a duplicate CRD means
        # one of the two firms is unreachable and which one wins is arbitrary.
        sample = df.loc[df["crd"].duplicated(keep=False), "crd"].head(5).tolist()
        r.fail(
            "crd_unique",
            f"{dupes:,} rows share a CRD with another row (e.g. {sample}); "
            f"build_adv_data.py drop_duplicates did not run or did not hold",
        )


def _check_firm_name(df: pd.DataFrame, r: ValidationReport) -> None:
    if "firm_name" not in df.columns:
        return
    r._ran("firm_name_blank")
    name = df["firm_name"]
    blank = name.isna() | (name.astype("string").fillna("").str.strip() == "")
    n_blank = int(blank.sum())
    if n_blank:
        r.fail(
            "firm_name_blank",
            f"{n_blank:,} rows have a blank firm_name - unsearchable and "
            f"unlabelled in the picker",
        )


def _check_aum(df: pd.DataFrame, r: ValidationReport) -> None:
    if "aum" not in df.columns:
        return
    aum = _numeric(df, "aum")

    r._ran("aum_valid")
    n_inf = int(np.isinf(aum.to_numpy(dtype="float64", na_value=np.nan)).sum())
    if n_inf:
        r.fail("aum_valid", f"{n_inf:,} rows have a non-finite AUM")
    finite = aum[np.isfinite(aum.to_numpy(dtype="float64", na_value=np.nan))]
    n_nonpos = int((finite <= 0).sum())
    if n_nonpos:
        # build_adv_data filters `merged["aum"].fillna(0) > 0`, so any
        # non-positive value here means that filter did not run.
        r.fail(
            "aum_valid",
            f"{n_nonpos:,} rows have AUM <= 0 despite build_adv_data.py's "
            f"aum > 0 filter",
        )

    r._ran("aum_bounds")
    positive = finite[finite > 0]
    if positive.empty:
        r.fail("aum_bounds", "no rows carry a positive AUM at all")
        return
    median = float(positive.median())
    mx = float(positive.max())
    if mx > AUM_MAX_PLAUSIBLE:
        r.fail(
            "aum_bounds",
            f"max AUM ${mx:,.0f} exceeds the plausibility ceiling of "
            f"${AUM_MAX_PLAUSIBLE:,.0f} - no single RIA is that large; "
            f"suspect a units change on Item 5 5F(2)(c)",
        )
    if median < AUM_MEDIAN_MIN or median > AUM_MEDIAN_MAX:
        direction = "smaller" if median < AUM_MEDIAN_MIN else "larger"
        r.fail(
            "aum_bounds",
            f"median AUM ${median:,.0f} is outside the plausible band "
            f"${AUM_MEDIAN_MIN:,.0f}-${AUM_MEDIAN_MAX:,.0f} ({direction} than "
            f"any real mix shift); this is the signature of a x1000 units change",
        )


def _check_counts(df: pd.DataFrame, r: ValidationReport) -> None:
    r._ran("counts_valid")
    for col in ("num_clients", "num_accounts"):
        if col not in df.columns:
            continue
        s = _numeric(df, col)
        arr = s.to_numpy(dtype="float64", na_value=np.nan)
        n_inf = int(np.isinf(arr).sum())
        if n_inf:
            r.fail("counts_valid", f"{col}: {n_inf:,} non-finite values")
        n_neg = int((s.dropna() < 0).sum())
        if n_neg:
            # sec_lookup surfaces these straight into the calculator inputs.
            r.fail("counts_valid", f"{col}: {n_neg:,} negative values")
        n_nonint = int((s.dropna() % 1 != 0).sum())
        if n_nonint:
            r.warn(
                "counts_valid",
                f"{col}: {n_nonint:,} non-integral values (app casts with int())",
            )


def _check_null_rates(df: pd.DataFrame, r: ValidationReport) -> dict[str, float]:
    r._ran("null_rates")
    rates: dict[str, float] = {}
    n = max(len(df), 1)
    for col in EXPECTED_COLUMNS:
        if col not in df.columns:
            continue
        rate = float(df[col].isna().sum()) / n
        rates[col] = rate
        limit = MAX_NULL_RATE.get(col)
        if limit is None:
            continue
        if rate > limit:
            r.fail(
                "null_rates",
                f"{col}: {rate:.2%} null exceeds the {limit:.2%} limit",
            )
        elif limit > 0 and rate > limit * NULL_RATE_WARN_FRACTION:
            r.warn(
                "null_rates",
                f"{col}: {rate:.2%} null is over half of the {limit:.2%} limit",
            )
    return rates


def _check_dates(
    df: pd.DataFrame,
    r: ValidationReport,
    as_of_expected: Optional[Any],
) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if "as_of_date" not in df.columns:
        return stats

    raw = df["as_of_date"]
    parsed = _to_datetime(raw)
    n = max(len(df), 1)

    # Unparseable-but-present strings are this check's business; raw nulls are
    # null_rates' business. Keeping them separate makes the CI message specific.
    r._ran("as_of_parseable")
    unparseable = int((parsed.isna() & raw.notna()).sum())
    unparseable_rate = unparseable / n
    if unparseable_rate > MAX_NULL_RATE["as_of_date"]:
        sample = raw[parsed.isna() & raw.notna()].head(3).tolist()
        r.fail(
            "as_of_parseable",
            f"{unparseable:,} rows ({unparseable_rate:.2%}) have an unparseable "
            f"as_of_date, over the {MAX_NULL_RATE['as_of_date']:.2%} limit "
            f"(e.g. {sample})",
        )

    r._ran("as_of_format")
    if pd.api.types.is_object_dtype(raw) or isinstance(raw.dtype, pd.StringDtype):
        iso = raw.astype("string").str.fullmatch(r"\d{4}-\d{2}-\d{2}")
        iso_rate = float(iso.fillna(False).sum()) / n
        if iso_rate < ISO_DATE_MIN_RATE:
            r.warn(
                "as_of_format",
                f"only {iso_rate:.2%} of as_of_date values are ISO YYYY-MM-DD; "
                f"the app renders this string verbatim",
            )

    valid = parsed.dropna()
    if valid.empty:
        r.fail("as_of_parseable", "no as_of_date value parses as a date at all")
        return stats

    today = pd.Timestamp(date.today())
    dmin, dmax = valid.min(), valid.max()
    stats["as_of_min"] = str(dmin.date())
    stats["as_of_max"] = str(dmax.date())
    stats["as_of_median"] = str(valid.median().date())
    stats["data_as_of"] = str(dmax.date())
    stats["as_of_unparseable_rate"] = unparseable_rate

    r._ran("as_of_future")
    future_cut = today + pd.Timedelta(days=FUTURE_GRACE_DAYS)
    n_future = int((valid > future_cut).sum())
    if n_future:
        # A filing cannot post after the snapshot was cut. This is what a
        # DD/MM-vs-MM/DD flip or a bad year pivot looks like.
        r.fail(
            "as_of_future",
            f"{n_future:,} rows carry a filing date after today "
            f"(max {dmax.date()}); dates are being parsed wrong",
        )

    r._ran("as_of_recency")
    age_days = int((today - dmax).days)
    stats["snapshot_age_days"] = age_days
    if age_days > MAX_SNAPSHOT_AGE_DAYS:
        r.fail(
            "as_of_recency",
            f"newest filing date is {dmax.date()}, {age_days} days old "
            f"(limit {MAX_SNAPSHOT_AGE_DAYS}) - this is a stale snapshot and "
            f"must not publish as current",
        )
    within = float(((dmax - valid).dt.days <= RECENT_WINDOW_DAYS).sum()) / n
    stats["recent_cluster_rate"] = within
    if within < RECENT_CLUSTER_MIN:
        r.fail(
            "as_of_recency",
            f"only {within:.2%} of rows filed within {RECENT_WINDOW_DAYS} days "
            f"of {dmax.date()} (floor {RECENT_CLUSTER_MIN:.0%}) - the file is "
            f"largely historical rows, not a current roster",
        )
    elif within < RECENT_CLUSTER_WARN:
        r.warn(
            "as_of_recency",
            f"{within:.2%} of rows filed within {RECENT_WINDOW_DAYS} days of "
            f"{dmax.date()}, below the usual {RECENT_CLUSTER_WARN:.0%}",
        )

    if as_of_expected is not None:
        r._ran("as_of_expected")
        try:
            expected = pd.Timestamp(as_of_expected)
        except Exception:  # noqa: BLE001
            r.fail(
                "as_of_expected",
                f"--as-of value {as_of_expected!r} is not a parseable date",
            )
        else:
            stats["as_of_expected"] = str(expected.date())
            gap = abs(int((dmax - expected).days))
            if gap > AS_OF_EXPECTED_TOLERANCE_DAYS:
                r.fail(
                    "as_of_expected",
                    f"newest filing {dmax.date()} is {gap} days from the "
                    f"expected snapshot date {expected.date()} (tolerance "
                    f"{AS_OF_EXPECTED_TOLERANCE_DAYS}) - discover_snapshots "
                    f"may have picked the wrong month",
                )
    return stats


def _check_registration_type(df: pd.DataFrame, r: ValidationReport) -> dict[str, int]:
    if "registration_type" not in df.columns:
        return {}
    r._ran("registration_type")
    s = df["registration_type"]
    counts = {str(k): int(v) for k, v in s.value_counts(dropna=False).items()}
    bad = sorted(
        {str(v) for v in s.dropna().unique()} - set(ALLOWED_REGISTRATION_TYPES)
    )
    if bad:
        r.fail(
            "registration_type",
            f"unexpected registration_type values {bad}; allowed "
            f"{sorted(ALLOWED_REGISTRATION_TYPES)}",
        )
    return counts


def _check_prior_year_coverage(df: pd.DataFrame, r: ValidationReport) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if "aum_prior_year" not in df.columns or df.empty:
        return stats
    r._ran("prior_year_coverage")

    overall = float(df["aum_prior_year"].notna().sum()) / max(len(df), 1)
    stats["prior_year_coverage"] = overall

    # Measure on SEC rows only: build_adv_data hard-sets aum_prior_year = NA on
    # every exempt row, so including them turns a roster-mix change into a fake
    # merge failure.
    scope = df
    if "registration_type" in df.columns:
        sec_rows = df[df["registration_type"] == "sec"]
        if not sec_rows.empty:
            scope = sec_rows
    coverage = float(scope["aum_prior_year"].notna().sum()) / max(len(scope), 1)
    stats["prior_year_coverage_sec"] = coverage

    # A positive prior-year AUM is what FirmData.growth_rate actually requires;
    # a zero or negative one makes the property return None.
    usable = _numeric(scope, "aum_prior_year")
    stats["prior_year_usable_rate"] = float((usable > 0).sum()) / max(len(scope), 1)

    if coverage < PRIOR_YEAR_COVERAGE_MIN:
        r.fail(
            "prior_year_coverage",
            f"only {coverage:.2%} of SEC-registered rows carry aum_prior_year "
            f"(floor {PRIOR_YEAR_COVERAGE_MIN:.0%}) - the prior-snapshot merge "
            f"broke and the app's YoY growth would be blank for most firms",
        )
    elif coverage < PRIOR_YEAR_COVERAGE_WARN:
        r.warn(
            "prior_year_coverage",
            f"aum_prior_year coverage {coverage:.2%} is below the usual "
            f"{PRIOR_YEAR_COVERAGE_WARN:.0%}",
        )
    return stats


def _check_versus_prior(
    df: pd.DataFrame, prior_path: Path, r: ValidationReport
) -> dict[str, Any]:
    stats: dict[str, Any] = {"prior_path": str(prior_path)}
    r._ran("prior_readable")
    try:
        prior = pd.read_parquet(prior_path)
    except Exception as exc:  # noqa: BLE001
        r.fail(
            "prior_readable",
            f"--prior {prior_path} could not be read ({exc!r}); the "
            f"delta checks did not run, so this run is NOT gated on them",
        )
        return stats
    if prior.empty:
        r.fail("prior_readable", f"--prior {prior_path} has zero rows")
        return stats

    stats["prior_row_count"] = int(len(prior))

    # -- row count -------------------------------------------------------
    r._ran("prior_row_delta")
    delta = (len(df) - len(prior)) / len(prior)
    stats["row_count_delta_pct"] = delta * 100.0
    if delta < -MAX_ROW_DROP:
        r.fail(
            "prior_row_delta",
            f"row count {len(df):,} vs prior {len(prior):,} ({_pct(delta)}) "
            f"exceeds the {MAX_ROW_DROP:.0%} drop limit - that is corruption, "
            f"not attrition",
        )
    elif delta > MAX_ROW_GROWTH:
        r.fail(
            "prior_row_delta",
            f"row count {len(df):,} vs prior {len(prior):,} ({_pct(delta)}) "
            f"exceeds the {MAX_ROW_GROWTH:.0%} growth limit",
        )
    elif delta < -WARN_ROW_DROP or delta > WARN_ROW_GROWTH:
        r.warn(
            "prior_row_delta",
            f"row count moved {_pct(delta)} vs prior ({len(prior):,} -> "
            f"{len(df):,}); larger than a normal month",
        )

    # -- aggregate AUM ---------------------------------------------------
    # Row count alone cannot see a units change or a swapped Item 5 column:
    # every row survives, only the magnitudes move.
    if "aum" in df.columns and "aum" in prior.columns:
        r._ran("prior_aum_delta")
        new_total = float(_numeric(df, "aum").sum(skipna=True))
        old_total = float(_numeric(prior, "aum").sum(skipna=True))
        stats["prior_total_aum"] = old_total
        if old_total > 0:
            adelta = (new_total - old_total) / old_total
            stats["total_aum_delta_pct"] = adelta * 100.0
            if adelta < -MAX_TOTAL_AUM_DROP:
                r.fail(
                    "prior_aum_delta",
                    f"aggregate AUM ${new_total:,.0f} vs prior ${old_total:,.0f} "
                    f"({_pct(adelta)}) exceeds the {MAX_TOTAL_AUM_DROP:.0%} drop "
                    f"limit - beyond any real market move between snapshots",
                )
            elif adelta > MAX_TOTAL_AUM_GROWTH:
                r.fail(
                    "prior_aum_delta",
                    f"aggregate AUM ${new_total:,.0f} vs prior ${old_total:,.0f} "
                    f"({_pct(adelta)}) exceeds the {MAX_TOTAL_AUM_GROWTH:.0%} "
                    f"growth limit - suspect a units change",
                )
            elif adelta < -WARN_TOTAL_AUM_DROP or adelta > WARN_TOTAL_AUM_GROWTH:
                r.warn(
                    "prior_aum_delta",
                    f"aggregate AUM moved {_pct(adelta)} vs prior",
                )
        else:
            r.fail("prior_aum_delta", "prior artifact has zero aggregate AUM")

    # -- CRD continuity --------------------------------------------------
    if "crd" in df.columns and "crd" in prior.columns:
        r._ran("prior_crd_overlap")
        new_crd = set(_numeric(df, "crd").dropna().astype("int64").tolist())
        old_crd = set(_numeric(prior, "crd").dropna().astype("int64").tolist())
        if old_crd:
            overlap = len(new_crd & old_crd) / len(old_crd)
            stats["crd_overlap_rate"] = overlap
            if overlap < CRD_OVERLAP_MIN:
                r.fail(
                    "prior_crd_overlap",
                    f"only {overlap:.2%} of the prior file's CRDs are still "
                    f"present (floor {CRD_OVERLAP_MIN:.0%}) - the join key "
                    f"changed shape or this is a different universe",
                )
            elif overlap < CRD_OVERLAP_WARN:
                r.warn(
                    "prior_crd_overlap",
                    f"CRD overlap with prior is {overlap:.2%}, below the usual "
                    f"{CRD_OVERLAP_WARN:.0%}",
                )
    return stats


def _check_manifest(
    df: pd.DataFrame, manifest_path: Path, r: ValidationReport
) -> dict[str, Any]:
    """Gate on build_adv_data.py's provenance sidecar.

    This is the only check in the module that does not read the Parquet for its
    verdict, because the fact it is judging -- how far apart the two SEC
    snapshots behind `aum_prior_year` were -- is not in the Parquet and cannot
    be recovered from it.
    """
    stats: dict[str, Any] = {"manifest_path": str(manifest_path)}

    r._ran("manifest_readable")
    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        r.fail(
            "manifest_readable",
            f"--manifest {manifest_path} could not be read or parsed ({exc!r}); "
            f"the snapshot-gap and exempt-roster checks did NOT run, so this "
            f"artifact's provenance is unknown",
        )
        return stats
    if not isinstance(payload, dict):
        r.fail(
            "manifest_readable",
            f"--manifest {manifest_path} parsed as {type(payload).__name__}, "
            f"not a JSON object",
        )
        return stats

    # -- structural sanity before reading any field ----------------------
    r._ran("manifest_schema")
    version = payload.get("manifest_schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        r.fail(
            "manifest_schema",
            f"manifest_schema_version is {version!r}, expected "
            f"{MANIFEST_SCHEMA_VERSION}; build_adv_data.py and this gate are "
            f"out of step, so no field below can be trusted to mean what it "
            f"is named",
        )
        return stats
    missing = [k for k in MANIFEST_REQUIRED_KEYS if k not in payload]
    if missing:
        r.fail(
            "manifest_schema",
            f"--manifest {manifest_path} is missing required keys {missing}; "
            f"a partial manifest cannot certify provenance",
        )
        return stats

    stats["manifest_built_at"] = payload.get("built_at_utc")
    stats["snapshot_current_date"] = payload.get("current_snapshot_date")
    stats["snapshot_prior_date"] = payload.get("prior_snapshot_date")
    stats["exempt_status"] = payload.get("exempt_status")
    stats["manifest_row_count"] = payload.get("row_count")
    stats["manifest_total_aum"] = _as_number(payload.get("total_aum"))
    if _as_number(payload.get("snapshot_gap_days")) is not None:
        stats["snapshot_gap_days"] = _as_int(payload.get("snapshot_gap_days"))

    # -- THE check: how old is "prior year" really? -----------------------
    r._ran("manifest_snapshot_gap")
    lo = SNAPSHOT_GAP_MONTHS_TARGET - SNAPSHOT_GAP_MONTHS_TOLERANCE
    hi = SNAPSHOT_GAP_MONTHS_TARGET + SNAPSHOT_GAP_MONTHS_TOLERANCE
    gap = _as_int(payload.get("snapshot_gap_months"))
    current = _iso_date(payload.get("current_snapshot_date"))
    prior = _iso_date(payload.get("prior_snapshot_date"))

    if gap is None:
        r.fail(
            "manifest_snapshot_gap",
            f"snapshot_gap_months is {payload.get('snapshot_gap_months')!r}, not a "
            f"whole number of months; build_adv_data.py records null when it "
            f"cannot parse a snapshot month out of the source URLs, so the "
            f"{lo}-{hi} month window was never enforced on this artifact",
        )
    else:
        stats["snapshot_gap_months"] = gap
        if current is None or prior is None:
            r.fail(
                "manifest_snapshot_gap",
                f"snapshot_gap_months says {gap} but the snapshot dates "
                f"({payload.get('prior_snapshot_date')!r} -> "
                f"{payload.get('current_snapshot_date')!r}) do not both parse, "
                f"so the gap cannot be corroborated",
            )
        else:
            recomputed = (current.year - prior.year) * 12 + (
                current.month - prior.month
            )
            if recomputed != gap:
                r.fail(
                    "manifest_snapshot_gap",
                    f"manifest is internally inconsistent: it records "
                    f"snapshot_gap_months={gap} but {prior} -> {current} is "
                    f"{recomputed} calendar months",
                )
        if not lo <= gap <= hi:
            # Informational only -- see the CORRECTION note on the constants.
            r.warn(
                "manifest_snapshot_gap",
                f"the two SEC snapshots are {gap} months apart "
                f"({payload.get('prior_snapshot_date')} -> "
                f"{payload.get('current_snapshot_date')}), outside the usual "
                f"{lo}-{hi}. Not blocking: ADV AUM is restated annually, so the "
                f"filing-season check decides whether growth is year-over-year",
            )

    # -- THE check: one annual filing season between the two snapshots? ----
    r._ran("manifest_filing_season")
    fs = payload.get("filing_season")
    if not isinstance(fs, dict):
        r.fail(
            "manifest_filing_season",
            f"filing_season is {fs!r}; without it nothing certifies that "
            f"aum_prior_year is one year back",
        )
    else:
        paired = _as_int(fs.get("paired_firms"))
        valid = _as_int(fs.get("valid_gap_firms"))
        unchanged = _as_number(fs.get("unchanged_aum_share"))
        fgap = _as_number(fs.get("median_filing_gap_years"))
        blanked = _as_number(fs.get("blanked_share"))
        stats["filing_paired_firms"] = paired
        stats["filing_valid_gap_firms"] = valid
        stats["filing_unchanged_aum_share"] = unchanged
        stats["filing_median_gap_years"] = fgap
        stats["filing_blanked_share"] = blanked
        # Internal consistency first: a malformed or hand-edited stats block
        # must not be able to certify anything.
        blanked_n = _as_int(fs.get("blanked_firms"))
        bad = []
        for name, v in (("unchanged_aum_share", unchanged), ("blanked_share", blanked)):
            if v is not None and not 0.0 <= v <= 1.0:
                bad.append(f"{name}={v} is not a share between 0 and 1")
        if paired is not None and valid is not None and valid > paired:
            bad.append(f"valid_gap_firms={valid} exceeds paired_firms={paired}")
        if paired is not None and blanked_n is not None:
            if blanked_n > paired:
                bad.append(f"blanked_firms={blanked_n} exceeds paired_firms={paired}")
            elif paired and blanked is not None and abs(blanked_n / paired - blanked) > 0.001:
                bad.append(f"blanked_firms/paired_firms={blanked_n / paired:.4f} "
                           f"disagrees with blanked_share={blanked}")
        if bad:
            r.fail("manifest_filing_season",
                   "filing_season is internally inconsistent: " + "; ".join(bad))
        if paired is None or paired < FILING_PAIRED_MIN:
            r.fail(
                "manifest_filing_season",
                f"only {paired!r} firms appear in both snapshots (need "
                f"{FILING_PAIRED_MIN:,}+); the pairing cannot certify growth",
            )
        # The median is only as good as the dates behind it: 9,999 unparseable
        # dates and one good one would otherwise produce a confident median.
        if valid is None or valid < FILING_PAIRED_MIN:
            r.fail(
                "manifest_filing_season",
                f"only {valid!r} paired firms have usable filing dates (need "
                f"{FILING_PAIRED_MIN:,}+); the median filing gap is not evidence",
            )
        if blanked is None or blanked > FILING_BLANKED_MAX:
            r.fail(
                "manifest_filing_season",
                f"{blanked!r} of paired firms failed the per-firm one-filing-"
                f"apart test (max {FILING_BLANKED_MAX:.0%}); the pairing itself "
                f"is wrong, not a few odd fiscal years",
            )
        elif blanked > FILING_BLANKED_WARN:
            r.warn(
                "manifest_filing_season",
                f"{blanked:.1%} of firms had growth withheld (usually ~2%)",
            )
        if unchanged is None or unchanged > FILING_UNCHANGED_MAX:
            r.fail(
                "manifest_filing_season",
                f"{unchanged!r} of paired firms report identical AUM in both "
                f"snapshots (max {FILING_UNCHANGED_MAX:.0%}) - they sit in the "
                f"same annual filing season, so the app would show ~0% growth "
                f"for most firms",
            )
        elif unchanged > FILING_UNCHANGED_WARN:
            r.warn(
                "manifest_filing_season",
                f"{unchanged:.1%} of paired firms report unchanged AUM "
                f"(usually ~1%)",
            )
        if fgap is None or not FILING_GAP_YEARS_MIN <= fgap <= FILING_GAP_YEARS_MAX:
            r.fail(
                "manifest_filing_season",
                f"median time between each firm's two filings is {fgap!r} "
                f"years (allowed {FILING_GAP_YEARS_MIN}-{FILING_GAP_YEARS_MAX}) "
                f"- growth would not be year-over-year",
            )
        elif not FILING_GAP_YEARS_WARN_LO <= fgap <= FILING_GAP_YEARS_WARN_HI:
            r.warn(
                "manifest_filing_season",
                f"median time between filings is {fgap:.2f} years (usually "
                f"~1.0)",
            )

    # -- did we publish a smaller universe than usual? --------------------
    r._ran("manifest_exempt_roster")
    exempt_status = payload.get("exempt_status")
    if exempt_status != MANIFEST_EXEMPT_OK:
        detail = payload.get("exempt_error")
        r.warn(
            "manifest_exempt_roster",
            f"exempt-reporter roster not included (exempt_status="
            f"{exempt_status!r}"
            + (f", {detail}" if detail else "")
            + "); this artifact covers a smaller universe of firms than one "
            "built with it",
        )

    # -- does this manifest even describe this file? ----------------------
    # A stale or copied manifest certifies a provenance the Parquet does not
    # have, which is strictly worse than shipping no manifest at all.
    r._ran("manifest_matches_parquet")
    rows = _as_int(payload.get("row_count"))
    if rows is None or rows != len(df):
        r.fail(
            "manifest_matches_parquet",
            f"manifest says row_count={payload.get('row_count')!r} but "
            f"{Path(r.summary.get('new_path', '')).name or 'the parquet'} has "
            f"{len(df):,} rows; this manifest describes a different file, so "
            f"its provenance does not apply here",
        )
    if "aum" in df.columns:
        claimed = _as_number(payload.get("total_aum"))
        actual = float(_numeric(df, "aum").dropna().sum())
        if claimed is None:
            r.fail(
                "manifest_matches_parquet",
                f"manifest total_aum is {payload.get('total_aum')!r}, not a "
                f"finite number",
            )
        else:
            scale = max(abs(actual), 1.0)
            rel = abs(claimed - actual) / scale
            if rel > MANIFEST_TOTAL_AUM_RTOL:
                r.fail(
                    "manifest_matches_parquet",
                    f"manifest says total_aum ${claimed:,.0f} but the parquet "
                    f"sums to ${actual:,.0f} ({_pct((claimed - actual) / scale)} "
                    f"off); this manifest describes a different file",
                )
    return stats


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------
def validate(
    new_path,
    prior_path=None,
    *,
    as_of_expected=None,
    manifest_path=None,
) -> ValidationReport:
    """Validate a freshly built ADV parquet.

    new_path        -- the artifact about to be published
    prior_path      -- the currently published artifact, for delta checks.
                       Omit to skip them; if passed and unreadable, that FAILs.
    as_of_expected  -- the snapshot date CI asked build_adv_data.py for, as a
                       date/datetime/ISO string. Catches month misselection.
    manifest_path   -- build_adv_data.py's provenance sidecar. Omit to skip the
                       provenance checks (the report says so, loudly); if
                       passed and unreadable or malformed, that FAILs.

    Never raises on bad data; every problem comes back as a finding.
    """
    r = ValidationReport()
    new_path = Path(new_path)
    r.summary["new_path"] = str(new_path)
    r.summary["validated_at"] = datetime.now().isoformat(timespec="seconds")
    # Overwritten below if a manifest is passed AND its checks pass. Set here so
    # that every exit path -- including the early returns for a missing or
    # unreadable artifact -- reports the provenance status explicitly rather
    # than by omission.
    r.summary["provenance_verified"] = False
    r.summary["provenance"] = (
        "UNVERIFIED - no --manifest passed; the snapshot gap behind "
        "aum_prior_year and the exempt-roster status were not checked"
        if manifest_path is None
        else f"UNVERIFIED - {manifest_path} was not reached (validation "
        f"stopped on an earlier failure)"
    )

    # -- the artifact exists and is not a stub ---------------------------
    r._ran("file_exists")
    if not new_path.exists():
        r.fail("file_exists", f"{new_path} does not exist")
        return r
    size = new_path.stat().st_size
    r.summary["file_bytes"] = int(size)
    r._ran("file_size")
    if size < MIN_FILE_BYTES:
        r.fail(
            "file_size",
            f"{size:,} bytes is below the {MIN_FILE_BYTES:,}-byte floor - the "
            f"download or the write was truncated",
        )

    # -- read-back: the file we just wrote must actually load -------------
    r._ran("readback")
    try:
        df = pd.read_parquet(new_path)
    except Exception as exc:  # noqa: BLE001
        r.fail("readback", f"pd.read_parquet({new_path}) raised {exc!r}")
        return r  # nothing else is knowable

    r.summary["row_count"] = int(len(df))
    r.summary["column_count"] = int(len(df.columns))
    r.summary["columns"] = list(df.columns)

    _check_schema(df, r)
    _check_row_count(df, r)
    if df.empty:
        # Everything below divides by row count; the row_count FAIL already
        # blocks publication.
        return r

    _check_crd(df, r)
    _check_firm_name(df, r)
    _check_aum(df, r)
    _check_counts(df, r)
    r.summary["null_rates"] = _check_null_rates(df, r)
    r.summary.update(_check_dates(df, r, as_of_expected))
    r.summary["registration_type_counts"] = _check_registration_type(df, r)
    r.summary.update(_check_prior_year_coverage(df, r))

    # -- descriptive stats for the CI log / "data as of" label ------------
    if "aum" in df.columns:
        aum = _numeric(df, "aum").dropna()
        r.summary["total_aum"] = float(aum.sum()) if not aum.empty else None
        r.summary["median_aum"] = float(aum.median()) if not aum.empty else None
        r.summary["max_aum"] = float(aum.max()) if not aum.empty else None
        r.summary["min_aum"] = float(aum.min()) if not aum.empty else None
    if "crd" in df.columns:
        r.summary["crd_unique"] = int(df["crd"].nunique(dropna=True))

    if manifest_path is not None:
        r.summary.update(_check_manifest(df, Path(manifest_path), r))
        verified = not (r.failed_checks & _MANIFEST_CHECKS)
        r.summary["provenance_verified"] = verified
        if verified:
            r.summary["provenance"] = (
                f"{r.summary.get('snapshot_prior_date')} -> "
                f"{r.summary.get('snapshot_current_date')} "
                f"({r.summary.get('snapshot_gap_months')} month gap), "
                f"exempt roster: {r.summary.get('exempt_status')}"
            )
        else:
            r.summary["provenance"] = (
                f"UNVERIFIED - {manifest_path} did not check out; see the "
                f"manifest_* findings"
            )

    if prior_path is not None:
        r.summary.update(_check_versus_prior(df, Path(prior_path), r))

    return r


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a built ADV parquet before publishing it.",
    )
    parser.add_argument(
        "--new", required=True, help="Path to the newly built parquet."
    )
    parser.add_argument(
        "--prior",
        default=None,
        help="Path to the currently published parquet, for delta checks.",
    )
    parser.add_argument(
        "--json", default=None, help="Write the full report to this JSON path."
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Snapshot date CI asked for (YYYY-MM-DD); catches month misselection.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "Path to build_adv_data.py's provenance sidecar "
            "(<out>.manifest.json). Gates on the current<->prior snapshot gap "
            "behind aum_prior_year, which nothing in the parquet records."
        ),
    )
    args = parser.parse_args(argv)

    report = validate(
        args.new,
        args.prior,
        as_of_expected=args.as_of,
        manifest_path=args.manifest,
    )
    print(report.render())

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"Report written to {out}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
