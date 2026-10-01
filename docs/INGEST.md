# Database ingest — `ingest.py`

`report_manifest.xlsx` is the controller: one row per sample (or batch), each
pointing at files in the standardized project layout (see
[DATA_FORMATS.md](DATA_FORMATS.md)). `ingest.py` loads them into Postgres using
[`sql/create_tables.sql`](../sql/create_tables.sql).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env            # PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
# optional local database:
POSTGRES_PASSWORD=change-me docker compose -f deploy/compose.yml up -d
```

Settings are read from real environment variables first, then `.env` in the repo
root. If `PGPASSWORD` is unset a warning is logged and the local docker default is
used — set it explicitly for any shared database.

## Commands

```bash
python ingest.py validate                      # check manifest + every referenced file (no DB)
python ingest.py validate --project OceanX --strict
python ingest.py concordance --all             # (re)write meta/concordance.json (no DB)

python ingest.py recreate-all                  # DROP + CREATE all tables, then import every enabled row
python ingest.py recreate-all --no-import --yes
python ingest.py recreate-all --terminate-sessions --yes   # kill sessions (e.g. Metabase) holding locks

python ingest.py import --all                  # import every enabled row (no drop)
python ingest.py import --project MBON
python ingest.py import --project-dir OceanX
python ingest.py import --sample-id MBON0001
python ingest.py import --all --dry-run        # validate the selection, touch nothing
python ingest.py add    --sample-id MBON0002   # alias of import

python ingest.py delete --project MBON --yes   # cascade delete honouring FKs
python ingest.py delete --run Wilder_611808
python ingest.py delete --sample-id MBON0001

python ingest.py inspect-schema                # print live tables + FK layout
```

Global options (before the command): `--projects-dir`, `--manifest`, `--schema`,
`--example` (use `data/example/projects`), `--log-level`, `--log-file`, `-q`.
Add `--no-concordance` to `import` / `add` / `recreate-all` to skip regenerating
`meta/concordance.json`.

Try it end-to-end on the synthetic data:

```bash
python ingest.py --example validate
python ingest.py --example recreate-all --yes
```

## Safety

* `recreate-all` and `delete` ask you to type `yes`; in scripts pass `--yes`. Without a
  terminal and without `--yes` they refuse to run.
* The manifest and every file are validated *before* the database is touched; an
  `import` logs validation problems and skips the affected files.
* Each project is ingested separately. If one fails, its open transaction is rolled
  back, the traceback goes to `logs/ingest.log`, and the remaining projects continue.
* Each Kraken2 report is committed on its own; a malformed report is skipped and listed.
* Values that violate a CHECK constraint (e.g. an unknown `neg_cont_type`) are
  dropped from that row instead of failing it.

## Schema

```
project -> collection -> sample -> run -> data_management -> bioinformatics -> kraken2_report -> kraken2_lcas
sample  -> mbon_samples (1:1 metadata extension)
wilder_results -> (mbon_samples, bioinformatics)
```

* One **collection** per FAIRe checklist / cruise (the dashboard's *Collections* tab).
* One **sample** per physical sample (FAIRe `materialSampleID`).
* One **run** + `data_management` + `bioinformatics` per sample (*Sequencing runs* tab).
* `mbon_samples` holds the FAIRe metadata (*MBON meta* tab).

## Concordance

* **kraken2**: report filename stem == FAIRe `materialSampleID` (or `samp_name`, or an
  MBON-normalised id such as `MBON1` → `MBON0001`) → `samp_number`. For Oxford barcoded
  runs: `barcodeNN` → barcoding workbook tab (`Barcode` → `sample_ID`) → FAIRe sample
  (directly, or by numeric suffix).
* **wilder**: `UID` → `ClientSampleID` → FAIRe sample; when `ClientSampleID` is blank,
  `UID` == FAIRe `samp_number`.
* Unmatched items are listed under `"unmatched"`. Fix the names (or edit the JSON) and
  re-run `concordance`.

Report rows whose sample is not in the FAIRe checklist get a lightweight
auto-created sample in a `<project> reports` collection, so no data is lost.
