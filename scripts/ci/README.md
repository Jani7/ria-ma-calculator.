# SEC ADV refresh automation

`refresh-adv-data.yml` automates what `CLAUDE.md` describes as a manual quarterly
chore: rebuild `adv_current.parquet` from the SEC bulk snapshot and publish it to
the private data repo.

## Why the workflow file lives here but runs there

It installs into **`Jani7/ria-ma-data`**, not this repo:

```
Jani7/ria-ma-data/.github/workflows/refresh-adv-data.yml
```

Running it from the public calculator repo would require a write-scoped PAT in a
public repo's secrets. Running it from the private data repo lets the built-in
`GITHUB_TOKEN` commit to its own repository, so the pipeline needs **no new
credential**. The scripts are still pulled from this repo at run time, so there
is one source of truth for the build and validation logic.

It is kept under version control here because it is part of this project's
delivery surface — the scripts it calls live in `scripts/`, and a change to
either has to be made in step with the other.

## Install

1. Copy `refresh-adv-data.yml` into `ria-ma-data` at
   `.github/workflows/refresh-adv-data.yml`, commit, push.
2. In `ria-ma-data` → Settings → Actions → General → Workflow permissions,
   confirm **Read and write permissions** is enabled (the `permissions:
   contents: write` block in the workflow needs it).
3. Trigger a first run by hand: Actions → *Refresh SEC ADV snapshot* → Run
   workflow. Optionally pass the expected snapshot month (e.g. `2026-04-30`) to
   tighten the freshness gate for that run.

The `ADV_DATA_TOKEN` secret in **Streamlit Cloud** is unrelated and unchanged —
it stays `Contents: read`, scoped to `ria-ma-data`. The refresh job writes; the
app only reads.

## The gate

`scripts/validate_adv_data.py` runs between the build and the commit, and a
non-zero exit blocks publication. It checks schema and dtypes, CRD uniqueness,
AUM plausibility (including a ×1000 units-change guard), null rates, filing-date
recency, and — against the previous month's artifact — row-count, aggregate-AUM,
and CRD-overlap deltas. Row count alone would miss a units error that keeps every
row, which is why the aggregate check exists.

A failed run leaves the last-known-good parquet in place and notifies through the
normal Actions failure path.

## How a refresh reaches users

The app re-checks the data repo every 15 minutes with a conditional GET
(`_resolve_adv` in `app.py`), so a successful push goes live without a redeploy
or container restart. Before this pipeline, a refreshed file was invisible until
a container happened to cold-start. Freshness is shown in the sidebar as
"data as of &lt;month&gt;", read from the newest filing date in the data itself.

## Running it locally

```bash
python scripts/build_adv_data.py --out /tmp/adv.parquet --manifest /tmp/adv.manifest.json
python scripts/validate_adv_data.py --new /tmp/adv.parquet --manifest /tmp/adv.manifest.json --prior data/adv_current.parquet
```

Downloads roughly 10&nbsp;MB from the SEC and takes 1–3 minutes.
