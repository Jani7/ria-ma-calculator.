"""Where the SEC ADV parquet comes from, and whether it can be trusted.

Streamlit-free on purpose. This is the one code path that only ever executes
in production — locally there is a dev parquet on disk, so the download,
validation and install logic short-circuits and never runs. That combination
(production-only + security-relevant + untestable) is how bugs get shipped, so
the logic lives here where a test can drive it with an injected opener, and
app.py keeps only the caching decorators around it.

Design notes worth keeping:

* The remote is consulted BEFORE the local cache. The previous order returned
  the cached file first and therefore could never observe a new snapshot: a
  refresh published to the data repo stayed invisible until the container died.
* The re-check is a conditional GET. With an ETag the common answer is 304,
  which is cheap enough to run on a short TTL and costs nothing against the
  GitHub rate limit.
* A download is proved to parse before it is installed. "Starts and ends with
  PAR1" says nothing about whether the middle survived, or whether the columns
  are the ones this app reads.
* Failure degrades to the last good file rather than to nothing.
"""

import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

# Columns the app cannot work without. scripts/build_adv_data.py writes eight;
# these three are touched by every lookup path, so a file missing any of them
# would fail in the user's face on every search.
REQUIRED_COLUMNS = ("crd", "firm_name", "aum")

# Operator-controlled secrets are spliced into the API URL. These are set on
# Streamlit Cloud, not supplied by users, so this is a footgun guard rather
# than an SSRF mitigation: a stray space or backslash would otherwise build a
# broken URL or, contrived but possible, point the fetch at another repo.
_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")
_PATH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}")
_REF_RE = re.compile(r"[A-Za-z0-9._/-]{1,100}")

DEFAULT_REPO = "Jani7/ria-ma-data"
DEFAULT_FILE = "adv_current.parquet"
DEFAULT_REF = "main"

# The secrets this module reads. Exported so app.py's st.secrets adapter has
# one list to copy from rather than a second hand-maintained one.
SECRET_KEYS = ("ADV_DATA_TOKEN", "ADV_DATA_REPO", "ADV_DATA_PATH", "ADV_DATA_REF")

_TIMEOUT_SEC = 60

# Ceiling on the download. The real artifact is ~700KB at 16K firms, so 64MB is
# ~90x headroom and still small enough that a wrong or hostile response cannot
# balloon a Streamlit container's memory before the parquet check rejects it.
_MAX_BODY_BYTES = 64 * 1024 * 1024

# An ETag is an opaque quoted ASCII token (RFC 9110), optionally weak-prefixed.
# It arrives from the network, is stored, and is replayed in a request header,
# so it gets validated on both sides of that round trip: a value carrying a
# newline could splice a header, and a value we store but the server never
# accepts would pin us to a permanent stale-cache loop.
_ETAG_RE = re.compile(r'(?:W/)?"[\x21\x23-\x7E]{1,200}"')


def _clean_etag(raw) -> str:
    """Return `raw` if it is a well-formed ETag, else "" (treated as absent)."""
    s = str(raw or "").strip()
    return s if _ETAG_RE.fullmatch(s) else ""


def parquet_magic_ok(path) -> bool:
    """A parquet file starts and ends with the 4-byte 'PAR1' magic."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            f.seek(-4, 2)
            tail = f.read(4)
        return head == b"PAR1" and tail == b"PAR1"
    except OSError:
        return False


def parquet_is_usable(path) -> bool:
    """Magic bytes, plus an actual read-back and schema check."""
    if not parquet_magic_ok(path):
        return False
    try:
        df = pd.read_parquet(path)
    except Exception:
        return False
    if df.empty:
        return False
    return all(c in df.columns for c in REQUIRED_COLUMNS)


def data_as_of(df) -> str:
    """Newest filing date in the snapshot — the honest 'data as of' stamp.

    Taken from the data, not from a build timestamp: what a user needs to know
    is how current the FILINGS are, not when the file was assembled.
    """
    try:
        if df is None or df.empty or "as_of_date" not in df.columns:
            return ""
        return str(df["as_of_date"].max() or "")
    except Exception:
        return ""


_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fmt_data_as_of(iso_date: str) -> str:
    """'2026-04-30' -> 'Apr 2026', or '' if it isn't a real date.

    Month precision is the honest resolution: the SEC publishes monthly, and a
    to-the-day stamp implies the whole file was current on that date when it is
    really a month's worth of filings.

    The explicit 1..12 range check is load-bearing. Without it Python's
    negative indexing turns a month of "00" into _MONTH_ABBR[-1] and prints a
    corrupt date as a confident "Dec 2026" — the failure mode this stamp exists
    to prevent.
    """
    try:
        y, m, _d = str(iso_date).split("-")[:3]
        month = int(m)
        if not 1 <= month <= 12:
            return ""
        if not (len(y) == 4 and y.isdigit()):
            return ""
        return f"{_MONTH_ABBR[month - 1]} {y}"
    except (ValueError, IndexError, TypeError):
        return ""


def _read_etag(etag_path: Path) -> str:
    try:
        return _clean_etag(etag_path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _local_version(path: Path) -> str:
    """mtime+size, so an in-place rebuild is picked up without a restart."""
    try:
        s = path.stat()
        return f"local-{int(s.st_mtime)}-{s.st_size}"
    except OSError:
        return "local"


def resolve(*, local_path, cache_path, etag_path, secrets, opener=None) -> dict:
    """Locate a usable ADV parquet.

    Returns {"path": Path|None, "status": str, "version": str}. `status` is a
    short PAT-safe code (never a token, URL, or exception string) so the whole
    dict is safe to log. `version` changes when the bytes change and feeds the
    dataframe cache key — without it, a refreshed snapshot at an unchanged path
    keeps serving the previous dataframe for the life of the container.

    `secrets` is a plain dict (app.py adapts st.secrets); `opener` is injectable
    for tests and defaults to urllib.
    """
    local_path = Path(local_path)
    cache_path = Path(cache_path)
    etag_path = Path(etag_path)
    if opener is None:
        opener = urllib.request.urlopen

    # 1. Local dev file wins outright.
    if local_path.exists() and parquet_magic_ok(local_path):
        return {"path": local_path, "status": "local",
                "version": _local_version(local_path)}

    def fall_back(status: str) -> dict:
        """Serve the last good download when the remote can't be reached.

        Full usability check, not just the magic bytes: degrading to stale
        data is the point, but degrading to a corrupt file that fails at
        lookup time is strictly worse than admitting the data is unavailable.
        """
        if parquet_is_usable(cache_path):
            return {"path": cache_path, "status": "cache",
                    "version": _read_etag(etag_path) or "cache"}
        return {"path": None, "status": status, "version": ""}

    token = str(secrets.get("ADV_DATA_TOKEN") or "")
    repo = str(secrets.get("ADV_DATA_REPO") or DEFAULT_REPO)
    file_path = str(secrets.get("ADV_DATA_PATH") or DEFAULT_FILE)
    ref = str(secrets.get("ADV_DATA_REF") or DEFAULT_REF)

    if not (_REPO_RE.fullmatch(repo) and _PATH_RE.fullmatch(file_path)
            and _REF_RE.fullmatch(ref)):
        return {"path": None, "status": "bad_config", "version": ""}
    if not token:
        return {"path": None, "status": "no_token", "version": ""}

    tmp_path = None
    try:
        # Contents API with Accept: application/vnd.github.raw returns the file
        # bytes directly — no separate blob download step.
        api = f"https://api.github.com/repos/{repo}/contents/{file_path}?ref={ref}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ria-ma-calculator",
        }
        # Only claim to hold a copy when we actually hold a GOOD one. A 304
        # says "what you have is current", and we answer it by serving the
        # cached file unexamined — so if the cache were truncated or corrupt
        # while its ETag sidecar survived, asking conditionally would pin us
        # to a broken file that no amount of re-checking could repair. Not
        # sending the ETag makes the server answer 200 with the full body,
        # which the validate-then-install path below then repairs from.
        etag = _read_etag(etag_path)
        if etag and parquet_is_usable(cache_path):
            headers["If-None-Match"] = etag

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(api, headers=headers)
        with opener(req, timeout=_TIMEOUT_SEC) as resp:
            # Read one byte past the ceiling so an oversized body is detected
            # rather than silently truncated into a "corrupt parquet".
            body = resp.read(_MAX_BODY_BYTES + 1)
            new_etag = _clean_etag(resp.headers.get("ETag"))
        if len(body) > _MAX_BODY_BYTES:
            return fall_back("too_large")

        # Land it beside the cache and prove it parses before it becomes the
        # file every lookup reads. Writing straight to the cache path meant a
        # truncated or schema-drifted response replaced a known-good snapshot.
        tmp_path = cache_path.with_name(f"{cache_path.stem}.{os.getpid()}.part")
        tmp_path.write_bytes(body)
        if not parquet_is_usable(tmp_path):
            tmp_path.unlink(missing_ok=True)
            tmp_path = None
            return fall_back("bad_response")
        tmp_path.replace(cache_path)  # atomic on POSIX and Windows
        tmp_path = None
        try:
            etag_path.write_text(new_etag, encoding="utf-8")
        except OSError:
            pass  # a lost ETag only costs a full download on the next re-check
        return {"path": cache_path, "status": "fetched",
                "version": new_etag or f"len-{len(body)}"}

    except urllib.error.HTTPError as e:
        if e.code == 304:
            # Unchanged since our last fetch. Safe to serve unexamined only
            # because a 304 can only arrive in response to an ETag we sent,
            # and we send one solely for a cache that just passed
            # parquet_is_usable — see the If-None-Match guard above.
            return {"path": cache_path, "status": "cache",
                    "version": _read_etag(etag_path) or "cache"}
        # Record only the status code — no body, no headers, no URL.
        return fall_back(f"http_{e.code}")
    except urllib.error.URLError:
        return fall_back("network_error")
    except OSError:
        return fall_back("io_error")
    except Exception:
        # Catch-all so exotic exceptions never reach Streamlit's error overlay,
        # which would surface the private repo name from the f-string above.
        return fall_back("unknown_error")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
