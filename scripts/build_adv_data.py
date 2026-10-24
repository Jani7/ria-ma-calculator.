"""Regenerate data/adv_current.parquet from the SEC's bulk Form ADV dataset.

Source landing page:
    https://www.sec.gov/data-research/sec-markets-data/
    information-about-registered-investment-advisers-exempt-reporting-advisers

The SEC publishes a monthly snapshot of all SEC-registered investment advisers
(`ia<MMDDYY>.zip`) and a parallel snapshot of exempt reporting advisers
(`ia<MMDDYY>-exempt.zip`). This script auto-discovers the most recent snapshot
and the one >=12 months prior, downloads both pairs, normalizes the columns we
need, joins on CRD for year-over-year AUM, tags each row with a
`registration_type`, and writes a small Parquet.

State-registered RIAs (~17K firms) are NOT included. Per the SEC's own FOIA
page (`/foia-services/frequently-requested-documents/form-adv-data`), the
agency does not maintain bulk data for state-registered advisers ---  that data
lives with FINRA's IARD system and the individual state regulators, and the
IAPD `/compilation` endpoint is a JavaScript SPA with no public bulk download
or documented API. NASAA's site does not publish a bulk file either. If a
bulk state-registered feed becomes available later, extend `STATE_SOURCES`.

Runs monthly via .github/workflows in ria-ma-data. ~5MB per snapshot, ~0.7MB output.

Alongside the Parquet this writes a provenance sidecar,
`adv_current.manifest.json` (override with --manifest). Nothing inside the
Parquet records WHICH two SEC snapshots produced it, and that gap is the one
thing the artifact cannot be audited for after the fact: `aum_prior_year` is
rendered by the app as a year-over-year growth rate, and whether it really is
one year back cannot be seen from inside the Parquet.

Note what "one year back" means here. Form ADV AUM is restated once a year, in
the annual amendment due within 90 days of fiscal year end; the monthly SEC
files repeat that number in between. So growth is honestly year-over-year when
the two snapshots straddle exactly ONE filing season -- not when they are 12
calendar months apart (the SEC skips months: none for Jul-Nov 2025 or Jun-Sep
2026). The manifest records that via `filing_season_stats`, and
`validate_adv_data.py --manifest` gates on it.

Schema mapping (Form ADV Part 1A -> our columns):
    Organization CRD#       -> crd
    Primary Business Name   -> firm_name  (falls back to Legal Name)
    5F(2)(c)                -> aum                (total regulatory AUM)
    5C(1)                   -> num_clients        (approximate # of clients)
    5F(2)(f)                -> num_accounts       (total accounts)
    Latest ADV Filing Date  -> as_of_date         (ISO-formatted)
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# SEC fair-access asks for a contact in the UA; the app's public URL serves
# that purpose without putting a personal (or squattable invented) email in
# public source. The +URL form is the standard bot-identification convention.
USER_AGENT = "RIA-Dashboard/1.0 (+https://ria-ma-calc.streamlit.app)"

LISTING_URL = (
    "https://www.sec.gov/data-research/sec-markets-data/"
    "information-about-registered-investment-advisers-exempt-reporting-advisers"
)

# Pattern matches the SEC's monthly snapshot filenames: ia<MMDDYY>.zip /
# ia<MMDDYY>.xlsx (SEC-registered) and the optional -exempt variant.
# Captured groups: (full filename, MMDDYY, optional `-exempt` flag, extension)
_FILENAME_RE = re.compile(
    r"ia(?P<date>[0-9]{6})(?P<exempt>-exempt)?\.(?P<ext>zip|xlsx)",
    re.IGNORECASE,
)
# Pattern for hrefs that point to a snapshot file. We want the full href so we
# preserve the SEC's somewhat inconsistent URL prefixes (some files live under
# /files/investment/data/other/..., others under /files/investment/data/...).
_HREF_RE = re.compile(
    r'href="(?P<href>[^"]*ia[0-9]{6}(?:-exempt)?\.(?:zip|xlsx))"',
    re.IGNORECASE,
)

COLUMNS_NEEDED = [
    "Organization CRD#",
    "Primary Business Name",
    "Legal Name",
    "Latest ADV Filing Date",
]
# Item 5 columns only appear on SEC-registered (full ADV) filings, not on
# exempt-reporter rosters. We handle their absence gracefully.
ITEM_5_COLUMNS = ["5F(2)(c)", "5C(1)", "5F(2)(f)"]

OUT_PATH = Path(__file__).parent.parent / "data" / "adv_current.parquet"

# --- Provenance sidecar ------------------------------------------------------
# Bump when the manifest's own key set changes; validate_adv_data.py pins this
# number and FAILs on anything it does not recognize, so the two move together.
MANIFEST_SCHEMA_VERSION = 2
# adv_current.parquet -> adv_current.manifest.json
MANIFEST_SUFFIX = ".manifest.json"

# What happened to the exempt-reporter roster on this run. The download failure
# is swallowed on purpose (an exempt outage should not block a refresh of the
# 16K SEC registrants) --- but "swallowed" must not mean "unrecorded", because
# losing the roster silently changes the population the app searches over.
EXEMPT_INCLUDED = "included"
EXEMPT_SKIPPED_BY_FLAG = "skipped_by_flag"       # operator passed --no-exempt
EXEMPT_MISSING_FROM_LISTING = "missing_from_listing"  # no -exempt href that month
EXEMPT_DOWNLOAD_FAILED = "download_failed"       # the bare-except path below


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def _clean_number(s):
    """Parse SEC's right-padded comma-formatted strings to float. None on empty."""
    if pd.isna(s):
        return None
    s = str(s).strip().replace(",", "")
    if not s or s == ".":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_mmddyy(s: str) -> datetime | None:
    """Parse an MMDDYY string from a SEC snapshot filename. Returns None on bad input."""
    if len(s) != 6 or not s.isdigit():
        return None
    mm, dd, yy = int(s[0:2]), int(s[2:4]), int(s[4:6])
    # SEC files only exist from 2001 onward; treat yy<80 as 20xx, else 19xx.
    year = 2000 + yy if yy < 80 else 1900 + yy
    try:
        return datetime(year, mm, dd)
    except ValueError:
        return None


def snapshot_date_from_url(url: str | None) -> datetime | None:
    """Recover the snapshot month from any ia<MMDDYY> URL, discovered or
    hand-passed via --current-url/--prior-url. None if the URL does not carry a
    parseable filename, in which case the manifest records the date as null and
    the validator refuses to certify the gap."""
    if not url:
        return None
    match = _FILENAME_RE.search(url)
    if not match:
        return None
    return _parse_mmddyy(match.group("date"))


def months_between(earlier: datetime, later: datetime) -> int:
    """Whole calendar months from `earlier` to `later`, day-of-month ignored.

    SEC snapshots are monthly and their day-of-month wanders (ia043026 vs
    ia050125), so counting (year, month) steps is the stable measure. The
    validator recomputes this identically and tolerates +/-1 month, which
    absorbs the day drift this deliberately throws away.
    """
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def discover_snapshots(
    listing_url: str = LISTING_URL,
    min_months_prior: int = 12,
) -> tuple[dict, dict]:
    """Scrape the SEC listing page for the most recent ia<MMDDYY> snapshot and
    one >= min_months_prior months earlier.

    Returns a pair of dicts, each with keys:
        date:    datetime
        current: full URL to the SEC-registered snapshot
        exempt:  full URL to the exempt-reporter snapshot (may be None if missing)

    Raises if it can't find at least two distinct months on the page.
    """
    html = _http_get(listing_url).decode("utf-8", errors="replace")

    # Bucket hrefs by (date, kind) so we can pair the SEC-registered and exempt
    # files that share the same MMDDYY. SEC sometimes uses slightly different
    # path prefixes for older months --- we preserve whatever href the page gives.
    by_date: dict[datetime, dict[str, str]] = {}
    for href_match in _HREF_RE.finditer(html):
        href = href_match.group("href")
        fname_match = _FILENAME_RE.search(href)
        if not fname_match:
            continue
        dt = _parse_mmddyy(fname_match.group("date"))
        if dt is None:
            continue
        is_exempt = bool(fname_match.group("exempt"))
        # Build an absolute URL (the page uses site-relative hrefs).
        absolute = urllib.parse.urljoin("https://www.sec.gov/", href)
        slot = by_date.setdefault(dt, {})
        slot["exempt" if is_exempt else "current"] = absolute

    # Keep only dates where we have at least the SEC-registered file.
    eligible = sorted(
        (d for d, urls in by_date.items() if "current" in urls), reverse=True
    )
    if not eligible:
        raise RuntimeError(
            f"No ia<MMDDYY>.zip|xlsx files found on {listing_url}"
        )

    latest_date = eligible[0]
    # Pick the most recent snapshot that is at least min_months_prior months
    # older than latest_date.
    cutoff = latest_date.replace(
        year=latest_date.year - (min_months_prior // 12),
        month=latest_date.month,
    )
    # Bump back min_months_prior % 12 months if not an even year.
    extra = min_months_prior % 12
    if extra:
        m = cutoff.month - extra
        y = cutoff.year
        while m < 1:
            m += 12
            y -= 1
        cutoff = cutoff.replace(year=y, month=m)

    prior_candidates = [d for d in eligible if d <= cutoff]
    if not prior_candidates:
        raise RuntimeError(
            f"Could not find a SEC snapshot at least {min_months_prior} months "
            f"before {latest_date.date()} on {listing_url}"
        )
    prior_date = prior_candidates[0]

    def _pack(d: datetime) -> dict:
        return {
            "date": d,
            "current": by_date[d]["current"],
            "exempt": by_date[d].get("exempt"),
        }

    return _pack(latest_date), _pack(prior_date)


def _read_zip_csv(zip_bytes: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(csv_name) as f:
            return pd.read_csv(f, dtype=str, encoding="latin-1", low_memory=False)


def _read_xlsx(xlsx_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(xlsx_bytes), dtype=str, engine="openpyxl")


def load_snapshot(url: str, registration_type: str) -> pd.DataFrame:
    """Download a SEC snapshot (ZIP or XLSX) and return a DataFrame
    with our normalized columns and a `registration_type` label."""
    print(f"Downloading {url}", file=sys.stderr)
    data = _http_get(url)
    print(f"  Got {len(data):,} bytes", file=sys.stderr)

    lower = url.lower()
    if lower.endswith(".zip"):
        df = _read_zip_csv(data)
    elif lower.endswith(".xlsx"):
        df = _read_xlsx(data)
    else:
        raise ValueError(f"Unknown file type: {url}")

    print(f"  Parsed {len(df):,} rows, {len(df.columns)} columns", file=sys.stderr)

    missing = [c for c in COLUMNS_NEEDED if c not in df.columns]
    if missing:
        raise RuntimeError(f"Snapshot is missing expected columns: {missing}")

    out = pd.DataFrame()
    out["crd"] = pd.to_numeric(df["Organization CRD#"], errors="coerce").astype("Int64")
    name = df["Primary Business Name"].fillna("").str.strip()
    legal = df["Legal Name"].fillna("").str.strip()
    out["firm_name"] = name.where(name != "", legal)

    # Item 5 (AUM/clients/accounts) is only filed by full SEC registrants, not
    # by exempt reporters. NaN-fill where the column is absent.
    for col, mapped in zip(ITEM_5_COLUMNS, ["aum", "num_clients", "num_accounts"]):
        if col in df.columns:
            out[mapped] = df[col].apply(_clean_number)
        else:
            out[mapped] = pd.NA

    out["as_of_date"] = pd.to_datetime(
        df["Latest ADV Filing Date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    out["registration_type"] = registration_type

    out = out.dropna(subset=["crd"])
    out = out[out["firm_name"].str.len() > 0]
    out = out.drop_duplicates(subset=["crd"], keep="first")
    return out.reset_index(drop=True)


# Per-firm test for "these two AUM values are one annual filing apart". The
# date is "Latest ADV Filing Date", which other-than-annual amendments also
# move, so it is a proxy for when the AUM was measured, not the measurement
# date itself -- hence a deliberately wide window. Erring here costs a firm its
# growth figure (the user types one in), never shows a wrong one. On the real
# Oct 2026 pairing this blanks 2.3% of firms.
FIRM_FILING_GAP_MIN_YEARS = 0.5
FIRM_FILING_GAP_MAX_YEARS = 1.75


def filing_season_stats(
    current: pd.DataFrame, prior: pd.DataFrame
) -> tuple[dict, set]:
    """Did these two snapshots straddle exactly one annual filing season?

    Form ADV is not a monthly AUM feed. A firm restates its regulatory AUM
    once a year, in the annual amendment due within 90 days of fiscal year end
    (for most firms, by March 31), and every monthly SEC file in between just
    repeats that number. So `aum_prior_year` is honestly year-over-year exactly
    when the two snapshots sit on either side of ONE filing season -- however
    many calendar months apart the snapshots themselves are. Measured on real
    data (Oct 2026): pairing with Dec 2025 (10 months) or Jun 2025 (16 months)
    gave median growth of 14.3% vs 14.6%, both ~1.0 year between filings.

    The two ways it really goes wrong, and what each looks like here:
      * same season: most firms' AUM is unchanged (May vs Oct 2026: 94%), and
        the app would show ~0% growth for everyone;
      * two seasons apart: the median time between filings is ~2 years.

    A population median cannot vouch for every firm, though: fiscal years and
    late filers differ, so a pairing that is one season apart for most firms
    can be zero or two for some. Returns (stats, crds_to_blank) -- the CRDs
    whose own pairing does not look like one annual filing, so the caller can
    withhold their growth rather than label it year-over-year.
    """
    a = current[["crd", "aum", "as_of_date"]].merge(
        prior[["crd", "aum", "as_of_date"]], on="crd", suffixes=("", "_p")
    )
    a = a[(a["aum"] > 0) & (a["aum_p"] > 0)]
    if a.empty:
        return {"paired_firms": 0, "valid_gap_firms": 0,
                "unchanged_aum_share": None, "median_filing_gap_years": None,
                "blanked_firms": 0, "blanked_share": None}, set()
    gap = (
        pd.to_datetime(a["as_of_date"], errors="coerce")
        - pd.to_datetime(a["as_of_date_p"], errors="coerce")
    ).dt.days / 365.25
    unchanged = a["aum"] == a["aum_p"]
    blank = (
        unchanged
        | gap.isna()
        | (gap < FIRM_FILING_GAP_MIN_YEARS)
        | (gap > FIRM_FILING_GAP_MAX_YEARS)
    )
    valid = gap.dropna()
    stats = {
        "paired_firms": int(len(a)),
        "valid_gap_firms": int(len(valid)),
        "unchanged_aum_share": round(float(unchanged.mean()), 4),
        "median_filing_gap_years": round(float(valid.median()), 3) if len(valid) else None,
        "blanked_firms": int(blank.sum()),
        "blanked_share": round(float(blank.mean()), 4),
    }
    return stats, set(a.loc[blank, "crd"].dropna().astype(int).tolist())


def manifest_path_for(out_path: Path) -> Path:
    """Default sidecar path: <out>.parquet -> <out>.manifest.json, same dir."""
    return out_path.with_name(out_path.stem + MANIFEST_SUFFIX)


def build_manifest(
    *,
    out_path: Path,
    current_url: str,
    prior_url: str,
    current_exempt_url: str | None,
    current_date: datetime | None,
    prior_date: datetime | None,
    exempt_status: str,
    exempt_error: str | None,
    frame: pd.DataFrame,
    filing_season: dict | None = None,
) -> dict:
    """Assemble the provenance record for one build. Pure -- no I/O."""
    gap = (
        months_between(prior_date, current_date)
        if current_date is not None and prior_date is not None
        else None
    )
    gap_days = (
        (current_date - prior_date).days
        if current_date is not None and prior_date is not None
        else None
    )
    aum = pd.to_numeric(frame["aum"], errors="coerce").dropna()
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parquet_path": str(out_path),
        # The two snapshots this artifact is made of. `aum_prior_year` is the
        # prior one; the app divides by it and calls the result year-over-year.
        "current_snapshot_date": current_date.date().isoformat() if current_date else None,
        "prior_snapshot_date": prior_date.date().isoformat() if prior_date else None,
        "snapshot_gap_months": gap,
        "snapshot_gap_days": gap_days,
        "exempt_status": exempt_status,
        "exempt_error": exempt_error,
        # Whether aum_prior_year really is one year back -- see
        # filing_season_stats. This, not the snapshot gap, is what the
        # validator gates on.
        "filing_season": filing_season,
        "source_urls": {
            "current": current_url,
            "prior": prior_url,
            "current_exempt": current_exempt_url,
            "listing": LISTING_URL,
        },
        "row_count": int(len(frame)),
        "total_aum": float(aum.sum()) if not aum.empty else 0.0,
        "registration_type_counts": {
            str(k): int(v)
            for k, v in frame["registration_type"].value_counts(dropna=False).items()
        },
    }


def write_manifest(path: Path, payload: dict) -> None:
    """Write the sidecar via tmp-then-replace, so an interrupted run leaves
    either the previous manifest or the new one --- never half a JSON object
    that the validator would have to guess about."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--current-url",
        default=None,
        help="Override auto-discovered current snapshot URL (SEC-registered).",
    )
    parser.add_argument(
        "--prior-url",
        default=None,
        help="Override auto-discovered prior-year snapshot URL (SEC-registered).",
    )
    parser.add_argument(
        "--current-exempt-url",
        default=None,
        help="Override auto-discovered current exempt-reporter snapshot URL.",
    )
    parser.add_argument(
        "--no-exempt",
        action="store_true",
        help="Skip the exempt-reporter snapshot entirely.",
    )
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "Path for the provenance sidecar JSON "
            "(default: <out> with a .manifest.json suffix)."
        ),
    )
    args = parser.parse_args()

    if args.current_url and args.prior_url:
        current_url = args.current_url
        prior_url = args.prior_url
        current_exempt_url = args.current_exempt_url
        print(
            "Using URLs from command line (skipping auto-discovery).",
            file=sys.stderr,
        )
    else:
        print(f"Auto-discovering latest snapshot from {LISTING_URL}", file=sys.stderr)
        latest, prior = discover_snapshots()
        current_url = args.current_url or latest["current"]
        prior_url = args.prior_url or prior["current"]
        current_exempt_url = args.current_exempt_url or latest["exempt"]
        print(
            f"  latest: {latest['date'].date()} -> {current_url}",
            file=sys.stderr,
        )
        print(
            f"  prior:  {prior['date'].date()} -> {prior_url}",
            file=sys.stderr,
        )
        if current_exempt_url:
            print(
                f"  exempt: {latest['date'].date()} -> {current_exempt_url}",
                file=sys.stderr,
            )

    # Recover the snapshot months from the URLs actually used, whether they came
    # from discovery or from --current-url/--prior-url. This is the gap the
    # manifest certifies and the validator gates on.
    current_date = snapshot_date_from_url(current_url)
    prior_date = snapshot_date_from_url(prior_url)
    if current_date is not None and prior_date is not None:
        print(
            f"  gap:    {months_between(prior_date, current_date)} months "
            f"({prior_date.date()} -> {current_date.date()})",
            file=sys.stderr,
        )
    else:
        print(
            "WARN: could not parse a snapshot month out of the URLs; the "
            "manifest will record an unverifiable gap.",
            file=sys.stderr,
        )

    sec_current = load_snapshot(current_url, registration_type="sec")
    sec_prior_full = load_snapshot(prior_url, registration_type="sec")
    filing_season, blank_crds = filing_season_stats(sec_current, sec_prior_full)
    print(f"  filing season: {filing_season}", file=sys.stderr)
    sec_prior = sec_prior_full[["crd", "aum"]].rename(
        columns={"aum": "aum_prior_year"}
    )
    # Firms whose own pairing is not one annual filing apart get no prior
    # AUM, so the app shows no growth for them instead of a mislabelled one.
    sec_prior = sec_prior[~sec_prior["crd"].isin(blank_crds)]

    sec_current = sec_current.merge(sec_prior, on="crd", how="left")

    frames = [sec_current]
    exempt_error: str | None = None
    if args.no_exempt:
        exempt_status = EXEMPT_SKIPPED_BY_FLAG
    elif not current_exempt_url:
        exempt_status = EXEMPT_MISSING_FROM_LISTING
    else:
        try:
            exempt = load_snapshot(current_exempt_url, registration_type="exempt")
            exempt["aum_prior_year"] = pd.NA  # exempt rosters lack item 5
            frames.append(exempt)
            exempt_status = EXEMPT_INCLUDED
        except Exception as exc:  # noqa: BLE001
            # Non-fatal by design, but recorded: the published file now covers a
            # smaller universe than the last one, and only the manifest says so.
            exempt_status = EXEMPT_DOWNLOAD_FAILED
            exempt_error = repr(exc)
            print(
                f"WARN: exempt snapshot failed ({exc!r}); continuing with SEC only.",
                file=sys.stderr,
            )

    merged = pd.concat(frames, ignore_index=True)
    # Drop firms with no reported regulatory AUM. This naturally filters out
    # exempt reporters that don't carry Item 5 data but keeps the ones that do.
    merged = merged[merged["aum"].fillna(0) > 0]
    # If a CRD shows up in both rosters (rare but possible during transitions),
    # prefer the SEC-registered row.
    merged = merged.sort_values(
        by="registration_type", key=lambda s: s.map({"sec": 0, "exempt": 1, "state": 2})
    ).drop_duplicates(subset=["crd"], keep="first")

    # Reorder columns to keep canonical ordering plus the new registration_type.
    column_order = [
        "crd",
        "firm_name",
        "aum",
        "num_clients",
        "num_accounts",
        "as_of_date",
        "aum_prior_year",
        "registration_type",
    ]
    merged = merged[column_order].reset_index(drop=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(out_path, compression="snappy", index=False)

    by_type = merged["registration_type"].value_counts().to_dict()
    print(
        f"Wrote {len(merged):,} firms to {out_path} "
        f"({out_path.stat().st_size / 1e6:.1f} MB); "
        f"by type: {by_type}",
        file=sys.stderr,
    )

    # Sidecar last, on purpose: the Parquet is already durable on disk before
    # anything here can raise, so a manifest problem can never cost us the
    # artifact. It also cannot be a Parquet column --- these values are constant
    # across all ~16K rows, and a column would be pure duplication.
    manifest_path = (
        Path(args.manifest) if args.manifest else manifest_path_for(out_path)
    )
    try:
        manifest = build_manifest(
            out_path=out_path,
            current_url=current_url,
            prior_url=prior_url,
            current_exempt_url=current_exempt_url,
            current_date=current_date,
            prior_date=prior_date,
            exempt_status=exempt_status,
            exempt_error=exempt_error,
            frame=merged,
            filing_season=filing_season,
        )
        write_manifest(manifest_path, manifest)
        print(
            f"Wrote provenance manifest to {manifest_path} "
            f"(gap {manifest['snapshot_gap_months']} months, "
            f"exempt: {manifest['exempt_status']})",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001
        # Exit 0 anyway: the Parquet is good and re-running the build would cost
        # another ~10MB of SEC downloads. Publication is still gated, because
        # validate_adv_data.py --manifest FAILs on a manifest it cannot read.
        print(
            f"WARN: failed to write the provenance manifest ({exc!r}); "
            f"{out_path} is intact but unprovenanced and validate_adv_data.py "
            f"--manifest will refuse to publish it.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
