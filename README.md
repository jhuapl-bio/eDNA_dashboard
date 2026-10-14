# eDNA_processing

Tools for turning marine eDNA taxonomic results into **interactive, map-based HTML
reports** and a queryable Postgres database — built for the Marine Biodiversity
Observation Network (MBON).

| Component | What it does |
|---|---|
| [`build_mbon_html.py`](build_mbon_html.py) | Reads Wilderlab ASV workbooks, Kraken2 reports and FAIRe station metadata (`.xlsx` / `.csv` / `.json`) and writes a single self-contained HTML report: station map, cross-sample analysis, explorer, data table, metadata. |
| [`ingest.py`](ingest.py) | Loads the same inputs into the MBON Postgres schema ([`sql/create_tables.sql`](sql/create_tables.sql)) and writes report↔sample concordance files. |
| [`api/`](api) + [`frontend/`](frontend) | Flask API + static dashboard (Vue/D3/Leaflet) for browsing the database, spreadsheets or Google Sheets. |
| [`make_example_data.py`](make_example_data.py) | Regenerates the bundled **synthetic** example dataset in [`data/example/`](data/example). |
| [`edna_common.py`](edna_common.py) | Shared logging, path and input-validation helpers. |

Every report and the dashboard ship with example data, so you can see what they
do before pointing them at your own files. Open
[`examples/example_report.html`](examples/example_report.html) in a browser.

---

## Quick start

```bash
git clone https://github.com/jhuapl-bio/eDNA_processing.git
cd eDNA_processing
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Demo: build a report from the bundled synthetic data
python build_mbon_html.py --example            # -> examples/example_report.html

# 2. Your data: lay it out under data/projects/ (see below), then
python build_mbon_html.py --validate           # check every input, write nothing
python build_mbon_html.py                      # -> reports/station_top10_species_map.html
```

Python 3.9+ is required. The HTML reports load their JavaScript libraries
(Leaflet, D3, SheetJS, …) from public CDNs, so viewing them needs an internet
connection.

## Folder layout

All paths are **relative to the repository**; nothing is tied to a particular
machine. Real data under `data/` is git-ignored; only `data/example/` is committed.

```
eDNA_processing/
├── build_mbon_html.py   ingest.py   make_example_data.py   edna_common.py
├── templates/station_top10_species_map_template.html   # report template (opens as a demo)
├── examples/            # example_report.html + example_payload.json (built from data/example)
├── data/
│   ├── example/projects/…   # synthetic demo data (committed)
│   └── projects/            # YOUR data (git-ignored)
│       ├── report_manifest.xlsx
│       └── <project>/{faire,kraken2,wilder,meta}/
├── reports/             # generated HTML, default output (git-ignored)
├── outputs/             # your per-project reports, e.g. outputs/mbon.html (git-ignored)
├── logs/                # run logs (git-ignored)
├── sql/create_tables.sql
├── api/  frontend/  deploy/  docs/  tests/
```

The input formats and the manifest columns are documented in
[docs/DATA_FORMATS.md](docs/DATA_FORMATS.md).

## Building reports — `build_mbon_html.py`

```bash
python build_mbon_html.py                                  # everything in the manifest
python build_mbon_html.py --projects OceanX --split        # one project, + one HTML per project
python build_mbon_html.py --runs 20260524_MV1_test02       # selected runs only
python build_mbon_html.py --skip-runs run_a,run_b          # leave runs out without editing the manifest
python build_mbon_html.py --projects-dir /mnt/share/projects -o reports/latest.html
python build_mbon_html.py --projects-dir data/projects/MBON -o outputs/mbon.html
                                                           # one project folder -> builds just that project
python build_mbon_html.py --json-out reports/latest.json   # also save the data payload
python build_mbon_html.py --from-json reports/latest.json  # re-render a saved payload
python build_mbon_html.py --help                           # all options
```

`--projects-dir` may point at the projects root (`data/projects`), at a single
project folder (`data/projects/MBON`) or at the shorthand `data/MBON`; the last two
resolve to the root and restrict the build to that project. `outputs/` and
`reports/` are both git-ignored, so per-project builds never get committed.

The report has Cross-Sample, Map, Explore, **Fish Compare**, Data Table and Metadata tabs:

- **Map** — a *Map options* panel offers keyless basemaps (Esri Light Gray / Ocean /
  Satellite / Topographic, GEBCO bathymetry, CARTO, OpenStreetMap, vector coastlines,
  none) with automatic failover, overlays (place labels, OpenSeaMap sea marks, lat/long
  grid, scale bar, station labels, cursor coordinates), marker size/colour by reads,
  samples, richness, dominant group or one taxon, cluster behaviour, dimming of
  stations that don't match the Filters, and *Export PNG*. Drawn regions can be shown
  on their own (*Regions only*), tiling the region windows to fill the pane.
- **Fish Compare** — pick fish (or any group's taxa) and compare them across stations,
  cruises, depths, months or drawn regions as a matrix, mini maps, trends,
  head-to-head, co-occurrence or location cards; controls/blanks excluded by default.
- **Values: Reads / % of sample** — a report-wide toggle (Filters panel, mirrored on
  Cross-Sample, Explore, Data Table, the station panel and Map options). “% of sample”
  shows each organism's share of all reads in its sample — or in the pooled samples /
  group a view aggregates — while raw reads stay in tooltips and cells.
- **Sample grouping** — *Filters → Sample grouping → Group samples…* opens a dialog to
  group samples by one or more metadata columns (station, cruise, depth, month,
  category, drawn map region or any categorical column; several columns combine).
  Each group can be shown, highlighted or hidden, and grouping can be switched off.
  Groups drive map pies + legend, Data Table “Per sample group”, Explore group panels,
  Fish Compare “Sample groups”, Cross-Sample group dots and a Metadata group column;
  hidden groups (and hidden samples) drop out of every tab and the map.
- **Cross-Sample → Occupancy bar → Stacked by group** — hover a legend item to
  highlight that group; Ctrl/⌘+click legend items or bar segments, then *Compare*
  to open them side by side (dialog, new window or CSV).

You can
switch between ASV and Kraken2 results, filter by run, database, group or sample,
draw map regions to compare areas, and export any chart or table (PNG/SVG/PDF/CSV/XLSX).
Files dropped into the report's **Data** panel overwrite samples that are already
loaded (matched on sample ID) instead of duplicating them — a FAIRe `sampleMetadata`
sheet replaces their metadata, an ASV workbook or Kraken2 report their reads — and
**Reset to original** drops every upload and manual edit.
More files can be dropped into the report's **Data Sources** panel in the browser.

## Loading the database — `ingest.py`

```bash
cp .env.example .env                        # set PGHOST / PGPASSWORD …
docker compose -f deploy/compose.yml up -d  # optional local Postgres (needs POSTGRES_PASSWORD)

python ingest.py validate                   # no DB needed
python ingest.py concordance --all          # no DB needed
python ingest.py recreate-all               # asks for confirmation (or --yes)
python ingest.py import --project MBON
```

See [docs/INGEST.md](docs/INGEST.md) for the schema and every command.

## Dashboard — `api/` + `frontend/`

```bash
bash api/run.sh                                     # API on http://127.0.0.1:5050
bash frontend/make-config.sh && python3 -m http.server 8280 -d frontend
```

With no database configured the dashboard starts on the bundled example workbook
(**Sources → Example data**). See [docs/DASHBOARD.md](docs/DASHBOARD.md).

## Failsafes and logging

* Every input is checked before use: missing or empty files, Office lock files
  (`~$…`), wrong extensions, unreadable or password-protected workbooks, missing
  sheets/columns, non-numeric or impossible coordinates (swapped lat/lon are repaired,
  `(0, 0)` and out-of-range values are dropped), duplicate sample IDs, and Kraken2
  files that aren't really Kraken2 reports.
* A bad file is **logged and skipped**; it never aborts the whole run. Reports are
  written atomically, so a failed build never leaves a half-written HTML.
* `--validate` (builder) and `validate` (ingest) list *every* problem in one pass
  without writing anything.
* Destructive database commands (`recreate-all`, `delete`) need confirmation or `--yes`;
  each project is ingested in its own transaction and rolled back on failure.
* Logs go to the console and to rotating files in `logs/` (`build_mbon_html.log`,
  `ingest.log`, `api.log`). Use `--log-level DEBUG` for detail, `--log-file -` to
  disable the file, `-q` for warnings only. Exit codes: `0` ok, `1` errors were
  logged, `2` bad arguments / no usable input.

## Development

```bash
pip install -r requirements-dev.txt
pytest                                   # pipeline + API tests
python make_example_data.py              # regenerate data/example/
python build_mbon_html.py --example      # rebuild examples/example_report.html
python build_mbon_html.py --example -o templates/station_top10_species_map_template.html
                                         # refresh the demo payload embedded in the template
```

CI (`.github/workflows/ci.yml`) runs lint, the tests, input validation and an
example build on every push and pull request.

## About the example data

Everything in `data/example/` is **synthetic**: stations, coordinates, sample IDs, read
counts and sequences are generated from a fixed seed by `make_example_data.py`.
Species names are real Atlantic coastal taxa, but the detections are invented and
TaxIDs are illustrative only. It contains no personal information.
