# Importing from the Ocean DNA Explorer

The [Ocean DNA Explorer](https://www.oceandnaexplorer.org) (ODE) hosts eDNA projects
as FAIRe metadata sheets plus Tourmaline ASV tables. This repo can pull any ODE
project — or a set of its samples — straight into a report, three ways:

| Way | Use it for |
|---|---|
| **Report dialog** — *🌊 Import from Ocean DNA Explorer* | Interactive: search, preview on a map, pick samples and analyses, watch the download, then switch between projects. |
| **`ode_client.py` CLI** | Scripted pulls into `data/projects/<project>/` (+ manifest rows) for built reports. |
| **Bridge HTTP API** (`/api/ode/*`) | Low-level calls with job status, from the report, `curl`, or any other tool. |

`ode_client.py` uses only the Python standard library. Building report layers on
the bridge also needs pandas (it imports `build_mbon_html.py`); without it the
report parses the files itself.

## How the pieces connect

The ODE metadata API (`https://www.oceandnaexplorer.org/api/...`) sends **no CORS
headers**, so a report opened from disk cannot call it. The ODE **file store**
(`*.public.blob.vercel-storage.com`) does allow cross-origin reads, so a report can
download project files directly once it knows their URLs. That gives the dialog
three connection modes, shown as a chip in its header:

| Chip | Meaning |
|---|---|
| **Live · local bridge** | `python3 ode_client.py serve` (port 5051) or `bash api/run.sh` (port 5050) answered. Live project list, sample search across projects, server-side caching, imports built by the report builder, optional *save to data/projects* + manifest. |
| **Catalog · YYYY-MM-DD** | No bridge. Projects come from the catalog snapshot embedded when the report was built (`data/ode_catalog.json`); files still download live from ODE in the browser. Sample search across projects is off. |
| **Offline** | No bridge and no catalog. The dialog shows the command to start the bridge. |

Click the chip to see details, change the bridge URL, or reconnect.

## The report dialog

Open it from the 📁 project menu in the header, the Filters panel (*Add data*), or
the Data panel. It has three steps:

1. **Find** — type a project name or id, or switch to **Samples** to search sample
   names / stations / places across every project (bridge only). Pasting an ODE link
   (e.g. `https://www.oceandnaexplorer.org/explore/sample/noaa-aoml-mire/MIRe_31B_Horseshoe_CB-1_2024-09-18`)
   jumps straight to that project with that sample selected.
2. **Preview & select** — the project's sample sheet on a mini map plus a sample list
   (filter by text, cruise, controls, date range; *Select shown / Clear shown / Only shown
   / Invert*; *Paste names…* takes sample names or library ids; click a map dot to
   toggle its samples, or *select samples in view*). Analyses are listed with libraries,
   ASVs, taxa and whether ODE has **reviewed** them. Reviewed analyses not already in the
   report are pre-selected; analyses already loaded are marked *already loaded* — ticking
   one re-imports it and **replaces** that layer (one layer per project + analysis).
   With the bridge you can also *save to data/projects* (+ *manifest*).
3. **Import** — one row per step with a status icon (waiting ◯, spinner, ✓, ✕, ↷ skipped),
   bytes downloaded and an overall bar. *Run in background* closes the dialog and leaves
   a status pill bottom-left; *Cancel* stops it. On success: per-layer samples / stations /
   taxa and *Show on map*; on failure: the failing step, the error, *Retry*, *Copy error*
   and (bridge mode) *Try direct download*.

Each analysis becomes its own **Ocean DNA Explorer** layer labelled
`<project> · <marker>` (`· N samples` for a subset). Imports are kept in this browser
(IndexedDB) and reopen with the same report file; *Reset to original* in the Data
panel removes them.

### Switching projects

The 📁 menu in the header lists every project in the report (built-in and imported)
with its methods, samples and layers. Choosing one limits every tab, the map, the
Run list and the *Report method* choices to that project; the map flies to it.
‹ › beside the menu step through projects; the Filters panel has the same **Project**
select. The choice is remembered per report file. The menu also lists imports saved
in this browser that aren't loaded (*Load* / forget), the import entry, and the
*Reopen my imports when this report loads* option.

### Deep links and the JavaScript API

`report.html#ode=noaa-aoml-mire` opens the dialog on that project;
`#ode=noaa-aoml-mire/MIRe_31B_Horseshoe_CB-1_2024-09-18` also preselects the sample.
From the console: `MBONOde.open({project: "noaa-aoml-mire", samples: [...]})`,
`MBONOde.setProject("MBON")`, `MBONOde.projects()`, `MBONOde.restore()`.

## GitHub Pages and "save nothing" mode

GitHub Pages only serves static files, so it can't run the bridge — but the published
report can use a bridge on the **visitor's own computer**, and nothing needs to be saved:

* **Pages build** (`.github/workflows/pages.yml`): fetches a fresh catalog with
  `python ode_client.py --cache "" catalog` (metadata only, written to the runner's temp
  folder, not committed) and builds the example report with
  `--ode-no-store --ode-catalog …`. Visitors can import any listed project with no setup:
  files download straight from ODE into their browser.
* **`--ode-no-store` reports** keep imports in memory only — no IndexedDB, no localStorage,
  and file downloads bypass the HTTP cache (`cache: "no-store"`); a reload removes them. The
  📁 menu says so (🔒). In other reports the viewer can untick *Keep my imports in this
  browser* for the same behaviour.
* **Live search from the Pages report**: the visitor runs
  `python3 ode_client.py serve --no-store` locally. The bridge then keeps downloads in memory
  only (bounded, gone when it stops), refuses *save to data/projects* (403), and skips the
  server-side build (which needs a temporary folder) — the browser parses the returned
  files instead. The dialog finds it at `http://127.0.0.1:5051/api/ode` as usual; its
  connection help shows the `--no-store` command when the page is hosted.
* **Browsers**: Chrome, Edge and Firefox let an https page call `http://127.0.0.1`
  (localhost counts as secure). Recent Chrome may show a "local network access" prompt —
  allow it; the bridge answers the private-network preflight. Safari may block the call;
  then open the report file from disk instead.

## Command line

```bash
python3 ode_client.py projects                      # every project + analyses
python3 ode_client.py show noaa-aoml-mire --samples # samples, dates, extent, libraries per assay
python3 ode_client.py find-samples Horseshoe        # samples across projects
python3 ode_client.py pull noaa-aoml-mire --analysis MiFish --match Horseshoe --manifest --build
python3 ode_client.py catalog                       # data/ode_catalog.json (embedded in reports)
python3 ode_client.py serve                         # the bridge, http://127.0.0.1:5051/api/ode
python3 ode_client.py serve --no-store              # same, but writes nothing to disk
```

`pull` options: `--analysis` (repeatable; analysis_run_name, assay name or marker such
as `MiFish`), `--samples a,b,c` or `--samples @names.txt`, `--match TEXT` (substring of
sample name / station / place / cruise / date), `--no-controls`, `--trusted` (reviewed
analyses only), `--out` (default `data/projects`), `--manifest` (adds rows to
`report_manifest.xlsx` after a timestamped backup; skips rows already there; refuses
while the workbook is open in Excel), `--build` (then builds `reports/<project>.html`).

It writes the layout the builder already reads:

```
data/projects/<project_id>/faire/       <project>_{project,sample,experimentRun}Metadata.tsv
data/projects/<project_id>/tourmaline/  table_<analysis>-<id>.tsv + asvTaxaFeatures_<analysis>-<id>.tsv
data/projects/<project_id>/meta/        ode_import.json (provenance), analysisMetadata_<analysis>.tsv
```

Files whose ODE names don't follow `table_` / `asvTaxaFeatures_` (e.g. phyloseq exports
`ps_mifish_ASV_table-….tsv`) are renamed so the builder pairs them, and a blank first
header cell in a feature table is written as `featureid`.

**Refresh the catalog** before building reports so the dialog's offline list is current:
`python3 ode_client.py catalog && python3 build_mbon_html.py …` (`--ode-catalog PATH`
embeds another file, `--no-ode-catalog` none).

## Bridge HTTP API

All routes are `GET` unless noted, return JSON, and allow cross-origin calls (including
`file://` pages; Chrome's private-network preflight is answered). Errors are
`{"error": "...", "status": n}` with the matching HTTP status.

| Route | Returns |
|---|---|
| `/api/ode/health?upstream=1` | `{ok, service: "ode-bridge", version, upstream, upstreamOk, builder, cache}` |
| `/api/ode/projects?q=` | `{projects: [{project_id, project_name, institution, counts, files, url, analyses: [{analysis_run_name, assay_name, short, trusted, counts, files}]}]}` |
| `/api/ode/projects/<id>` | `{project}` (404 with suggestions when unknown) |
| `/api/ode/projects/<id>/preview` | `{project, projectMeta, summary: {samples, controls, withCoords, stations, cruises, dateRange, bbox, librariesByAssay}, samples: [...]}` |
| `/api/ode/samples?q=&project_id=&limit=` | `{samples: [{project_id, samp_name, decimalLatitude, decimalLongitude, eventDate, station_id, ...}]}` |
| `/api/ode/catalog` | the catalog snapshot, live |
| `/api/ode/file?url=` | the file (TSV), cached; only ODE file hosts |
| `POST /api/ode/jobs` | body `{project_id, analyses?, samples?, match?, noControls?, trustedOnly?, build?: true, files?: false, save?: false, manifest?: false}` → `202 {job}` |
| `/api/ode/jobs/<id>` | `{job: {state: queued\|running\|done\|error\|cancelled, message, error, steps: [{key, label, state, done, total, detail}], progress}}` |
| `/api/ode/jobs/<id>/result` | `{job, result: {project, analyses, samples, runs?, files?, saved?, manifestAdded?}}` — `409` until done |
| `DELETE /api/ode/jobs/<id>` | cancel |

`save` is only accepted from a local page (`file://`, `localhost`, `127.0.0.1`).

```bash
B=http://127.0.0.1:5051/api/ode
curl "$B/projects?q=reef"
JOB=$(curl -s -X POST "$B/jobs" -H 'Content-Type: application/json' \
      -d '{"project_id":"noaa-aoml-mire","analyses":["MiFish"],"match":"Horseshoe"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["job"]["id"])')
curl "$B/jobs/$JOB"            # poll: steps + state
curl "$B/jobs/$JOB/result"     # runs (report JSON) once done
```

`health` reports `store: false` for a `--no-store` bridge.

Settings (environment): `ODE_BASE_URL` (default `https://www.oceandnaexplorer.org`),
`ODE_CACHE_DIR` (default `data/ode_cache`), `ODE_BRIDGE_HOST` / `ODE_BRIDGE_PORT`
(default `127.0.0.1:5051`), `ODE_FILE_HOSTS` (extra allowed file hosts, for mirrors/tests).

## Tests

`tests/test_ode_client.py` runs everything offline against `tests/ode_mock.py`, which
serves **synthetic** projects from `tests/fixtures/ode/` — no real survey data. The
fixtures are written by `python tests/make_ode_fixtures.py` (fixed seed, invented
places, organisms, sequences and read counts) in both file styles ODE serves:
`demo-shelf-2023` (standard `table_*` / `asvTaxaFeatures_*`, FAIRe `#` rows, M/D/YY
dates, controls without coordinates, two assays) and `demo-reef-2025` (phyloseq-style
`ps_*_ASV_table` / `ps_*_taxonomy`, blank first header, `domain` + `confidence`,
double-encoded file names, A/B replicates). `python tests/ode_mock.py --port 5099 --slow 0.05`
starts the mock on its own, e.g. for trying the dialog with
`ODE_BASE_URL=http://127.0.0.1:5099 ODE_FILE_HOSTS=127.0.0.1 python3 ode_client.py serve`.
