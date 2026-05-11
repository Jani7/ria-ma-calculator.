"""Regenerate data/adv_current.parquet from the SEC's bulk Form ADV dataset.

Source: https://www.sec.gov/data-research/sec-markets-data/information-about-registered-investment-advisers-exempt-reporting-advisers

The SEC publishes a monthly snapshot of all SEC-registered investment advisers
as a ZIP (CSV inside) or XLSX. This script downloads the current snapshot and
the prior-year snapshot, filters/normalizes the columns we need, joins them
on CRD for the year-over-year AUM, and writes a small Parquet.

Run quarterly. ~5MB download, ~3-5MB output.

Schema mapping (Form ADV Part 1A → our columns):
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
import sys
import zipfile
from pathlib import Path

import pandas as pd
import urllib.request

USER_AGENT = "RIA-Dashboard/1.0 (dhruvjani7@gmail.com)"

# SEC monthly snapshots. Update these URLs when refreshing.
CURRENT_URL = (
    "https://www.sec.gov/files/investment/data/other/"
    "information-about-registered-investment-advisers-exempt-reporting-advisers/"
    "ia050126.zip"
)
PRIOR_URL = (
    "https://www.sec.gov/files/investment/data/"
    "information-about-registered-investment-advisers-exempt-reporting-advisers/"
    "ia050225.xlsx"
)

COLUMNS_NEEDED = [
    "Organization CRD#",
    "Primary Business Name",
    "Legal Name",
    "5F(2)(c)",
    "5C(1)",
    "5F(2)(f)",
    "Latest ADV Filing Date",
]

OUT_PATH = Path(__file__).parent.parent / "data" / "adv_current.parquet"


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _clean_number(s):
    """Parse SEC's right-padded comma-formatted strings to float. NaN on empty."""
    if pd.isna(s):
        return None
    s = str(s).strip().replace(",", "")
    if not s or s == ".":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _read_zip_csv(zip_bytes: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        with zf.open(csv_name) as f:
            return pd.read_csv(f, dtype=str, encoding="latin-1", low_memory=False)


def _read_xlsx(xlsx_bytes: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(xlsx_bytes), dtype=str, engine="openpyxl")


def load_snapshot(url: str) -> pd.DataFrame:
    """Download a SEC snapshot (ZIP or XLSX) and return a DataFrame
    with our normalized columns."""
    print(f"Downloading {url}", file=sys.stderr)
    data = _http_get(url)
    print(f"  Got {len(data):,} bytes", file=sys.stderr)

    if url.lower().endswith(".zip"):
        df = _read_zip_csv(data)
    elif url.lower().endswith(".xlsx"):
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
    out["aum"] = df["5F(2)(c)"].apply(_clean_number)
    out["num_clients"] = df["5C(1)"].apply(_clean_number)
    out["num_accounts"] = df["5F(2)(f)"].apply(_clean_number)
    out["as_of_date"] = pd.to_datetime(
        df["Latest ADV Filing Date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    out = out.dropna(subset=["crd"])
    out = out[out["firm_name"].str.len() > 0]
    out = out.drop_duplicates(subset=["crd"], keep="first")
    return out.reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-url", default=CURRENT_URL)
    parser.add_argument("--prior-url", default=PRIOR_URL)
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()

    current = load_snapshot(args.current_url)
    prior = load_snapshot(args.prior_url)[["crd", "aum"]].rename(
        columns={"aum": "aum_prior_year"}
    )

    merged = current.merge(prior, on="crd", how="left")
    merged = merged[merged["aum"].fillna(0) > 0]  # drop zero/missing AUM rows

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(out_path, compression="snappy", index=False)

    print(
        f"Wrote {len(merged):,} firms to {out_path} "
        f"({out_path.stat().st_size / 1e6:.1f} MB)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
