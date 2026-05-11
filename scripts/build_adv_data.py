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

Run quarterly. ~5MB per snapshot, ~3-5MB output.

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
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

USER_AGENT = "RIA-Dashboard/1.0 (dhruvjani7@gmail.com)"

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

    sec_current = load_snapshot(current_url, registration_type="sec")
    sec_prior = load_snapshot(prior_url, registration_type="sec")[["crd", "aum"]].rename(
        columns={"aum": "aum_prior_year"}
    )

    sec_current = sec_current.merge(sec_prior, on="crd", how="left")

    frames = [sec_current]
    if current_exempt_url and not args.no_exempt:
        try:
            exempt = load_snapshot(current_exempt_url, registration_type="exempt")
            exempt["aum_prior_year"] = pd.NA  # exempt rosters lack item 5
            frames.append(exempt)
        except Exception as exc:  # noqa: BLE001
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
