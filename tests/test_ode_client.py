"""Ocean DNA Explorer client / bridge / builder integration, run offline
against tests/ode_mock.py, which serves SYNTHETIC projects written by
tests/make_ode_fixtures.py (no real survey data)."""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import ode_client as oc  # noqa: E402
from ode_mock import MockODE  # noqa: E402

SHELF = "demo-shelf-2023"     # standard Tourmaline file names
REEF = "demo-reef-2025"    # phyloseq-style names, blank header, domain / confidence


@pytest.fixture()
def mock():
    m = MockODE().start()
    yield m
    m.stop()


@pytest.fixture()
def client(mock, tmp_path):
    return oc.ODEClient(base=mock.base, cache_dir=tmp_path / "cache", allow_hosts=["127.0.0.1"])


def test_projects_and_lookup(client):
    ps = client.projects()
    assert [p["project_id"] for p in ps] == [REEF, SHELF]
    gu = client.project(SHELF)
    assert {a["short"] for a in gu["analyses"]} == {"MiFish", "Riaz"}
    assert {a["short"] for a in client.project(REEF)["analyses"]} == {"MiFish", "MarVer1"}
    assert gu["files"]["sampleMetadata"].endswith(".tsv")
    assert client.projects("reef")[0]["project_id"] == REEF
    # links resolve to the project
    p = client.project("https://www.oceandnaexplorer.org/explore/sample/demo-reef-2025/DEMO_01A_Lagoon_SiteA-1_2025-05-20")
    assert p["project_id"] == REEF
    with pytest.raises(oc.ODEError) as e:
        client.project("no-such-project")
    assert e.value.status == 404


def test_parse_ref():
    assert oc.parse_ode_ref("demo-reef-2025") == {"project_id": "demo-reef-2025", "samp_name": None}
    r = oc.parse_ode_ref("https://www.oceandnaexplorer.org/explore/sample/demo-reef-2025/DEMO_01A_Lagoon_SiteA-1_2025-05-20")
    assert r == {"project_id": "demo-reef-2025", "samp_name": "DEMO_01A_Lagoon_SiteA-1_2025-05-20"}
    assert oc.short_assay("Fish-12S-MiFish-U-Demo") == "MiFish"
    assert oc.iso_date("7/6/19") == "2019-07-06" and oc.iso_date("2024-05-23T16:15:00Z") == "2024-05-23"


def test_preview_and_sample_search(client):
    pv = client.preview(SHELF)
    sm = pv["summary"]
    assert sm["samples"] == 18 and sm["controls"] == 4 and sm["withCoords"] == 14
    assert sm["dateRange"] == ["2023-07-06", "2023-10-19"]          # M/D/YY normalised
    assert sm["librariesByAssay"] == {"Fish-12S-MiFish-U-Demo": 18, "Fish-12S-Riaz-Demo": 18}
    hits = client.find_samples("Lagoon")
    assert len(hits) == 6 and all(h["project_id"] == REEF for h in hits)
    assert len(client.find_samples("SiteA-1", REEF)) == 2


def test_files_are_cached_and_host_restricted(client, mock):
    url = client.project(SHELF)["files"]["projectMetadata"]
    a = client.fetch_file(url)
    n = len(mock.hits)
    seen = []
    b = client.fetch_file(url, progress=lambda d, t, cached: seen.append(cached))
    assert a == b and len(mock.hits) == n and seen == [True]
    with pytest.raises(oc.ODEError):
        client.fetch_file("https://evil.example.com/x.tsv")


def test_pull_subset_writes_builder_layout(client, tmp_path):
    out = tmp_path / "projects"
    res = oc.pull_project(client, REEF, out, analyses=["MiFish"], match="SiteA-1",
                          manifest=out / "report_manifest.xlsx")
    assert sorted(res["samples"]) == ["DEMO_01A_Lagoon_SiteA-1_2025-05-20", "DEMO_01B_Lagoon_SiteA-1_2025-05-20"]
    tdir = out / REEF / "tourmaline"
    tables = sorted(p.name for p in tdir.iterdir())
    assert tables[0].startswith("asvTaxaFeatures_demoreef_mifish") and tables[1].startswith("table_demoreef_mifish")
    hdr = (tdir / tables[1]).read_text().splitlines()[0].split("\t")
    assert hdr[0] == "featureid" and len(hdr) == 3          # blank header fixed, 2 libraries kept
    prov = json.loads((out / REEF / "meta" / "ode_import.json").read_text())
    assert prov["project"]["project_id"] == REEF and len(prov["samples"]) == 2
    assert res["manifestAdded"] == 1
    # the report builder reads the pulled folder
    import build_mbon_html as B
    runs = B.load_ode_folder(tdir, "x", REEF)
    assert len(runs) == 1 and len(runs[0]["samples"]) == 2 and runs[0]["stations"]
    # a second pull doesn't duplicate the manifest row
    res2 = oc.pull_project(client, REEF, out, analyses=["MiFish"], match="SiteA-1", manifest=out / "report_manifest.xlsx")
    assert res2["manifestAdded"] == 0


def test_build_runs_both_file_styles(client):
    p = client.project(SHELF)
    anas = oc.pick_analyses(p)
    texts = oc.download_all(client, p, anas)
    runs, why = oc.build_runs(p, anas, texts)
    assert why is None
    by = {r["label"]: r for r in runs}
    mf, rz = by[f"{SHELF} · MiFish"], by[f"{SHELF} · Riaz"]
    assert (len(mf["taxa"]), len(mf["stations"]), len(mf["samples"])) == (12, 14, 18)
    assert (len(rz["taxa"]), len(rz["stations"])) == (11, 14)
    assert mf["ode"]["analysis_run_name"] == "demo23-mifish-run1" and mf["builtin"] is False
    # phyloseq-style analysis: blank header, 'domain' rank, lower-case confidence
    p = client.project(REEF)
    anas = oc.pick_analyses(p, ["MiFish"])
    runs, _ = oc.build_runs(p, anas, oc.download_all(client, p, anas))
    r = runs[0]
    assert (len(r["taxa"]), len(r["stations"]), len(r["samples"])) == (12, 8, 16)
    named = [t for t in r["taxa"] if t["species"] != "Unassigned"]
    assert named and all(t["isEuk"] and t.get("confidence") for t in named)


def _get(url, method="GET", body=None, origin=None):
    h = {"Content-Type": "application/json"}
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(url, method=method, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


@pytest.fixture()
def bridge(client, tmp_path):
    box = {}
    t = threading.Thread(target=oc.serve, kwargs=dict(host="127.0.0.1", port=0, client=client,
                                                      projects_dir=tmp_path / "pd",
                                                      ready=lambda h: box.update(h=h)), daemon=True)
    t.start()
    for _ in range(100):
        if "h" in box:
            break
        time.sleep(0.02)
    yield f"http://127.0.0.1:{box['h'].server_address[1]}/api/ode"
    box["h"].shutdown()


def _wait(base, jid):
    for _ in range(300):
        st, _, j = _get(f"{base}/jobs/{jid}")
        if j["job"]["state"] not in ("queued", "running"):
            return j["job"]
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_bridge_job_success_and_cors(bridge):
    st, h, j = _get(bridge + "/health", origin="null")
    assert st == 200 and j["service"] == "ode-bridge" and h["Access-Control-Allow-Origin"] == "null"
    st, _, j = _get(bridge + "/jobs", "POST", json.dumps({"project_id": SHELF, "analyses": ["Riaz"],
                                                          "samples": ["DEMO231019-CTD01-50"]}).encode())
    assert st == 202
    job = _wait(bridge, j["job"]["id"])
    assert job["state"] == "done", job
    assert [s["state"] for s in job["steps"]] == ["done"] * len(job["steps"])
    st, _, r = _get(f"{bridge}/jobs/{job['id']}/result")
    run = r["result"]["runs"][0]
    assert run["label"] == f"{SHELF} · Riaz · 1 sample" and [s["sampleId"] for s in run["samples"]] == ["DEMO231019-CTD01-50"]


def test_bridge_job_failure_states(bridge, mock):
    mock.fail_files.add(next(n for n in mock.fmap if n.startswith("table_demo23_riaz")))
    st, _, j = _get(bridge + "/jobs", "POST", json.dumps({"project_id": SHELF, "analyses": ["Riaz"]}).encode())
    job = _wait(bridge, j["job"]["id"])
    assert job["state"] == "error" and "re-submitted" in job["message"]
    states = {s["key"].split(":")[0]: s["state"] for s in job["steps"]}
    assert states["table"] == "error" and states["sampleMetadata"] == "done"
    st, _, r = _get(f"{bridge}/jobs/{job['id']}/result")
    assert st == 409
    # unknown project / remote page asking to save to disk
    st, _, j = _get(bridge + "/jobs", "POST", json.dumps({"project_id": "nope"}).encode())
    assert _wait(bridge, j["job"]["id"])["state"] == "error"
    st, _, j = _get(bridge + "/jobs", "POST", json.dumps({"project_id": SHELF, "save": True}).encode(),
                    origin="https://example.com")
    assert st == 403


def test_builder_embeds_catalog(tmp_path, client):
    import build_mbon_html as B
    cat = tmp_path / "ode_catalog.json"
    cat.write_text(json.dumps(client.catalog()))
    old = B.ODE_CATALOG
    try:
        B.ODE_CATALOG = cat
        p = B._attach_ode_catalog({"runs": []})
        assert [x["project_id"] for x in p["odeCatalog"]["projects"]] == [REEF, SHELF]
        B.ODE_CATALOG = None
        assert "odeCatalog" not in B._attach_ode_catalog({"runs": []})
    finally:
        B.ODE_CATALOG = old


def test_bridge_no_store_writes_nothing(mock, tmp_path):
    """`serve --no-store` (e.g. for a report on GitHub Pages): memory-only downloads,
    no save, report layers left to the browser (raw files returned)."""
    client = oc.ODEClient(base=mock.base, cache_dir=None, allow_hosts=["127.0.0.1"])
    pd = tmp_path / "pd"
    box = {}
    threading.Thread(target=oc.serve, kwargs=dict(host="127.0.0.1", port=0, client=client, projects_dir=pd,
                                                  ready=lambda h: box.update(h=h), store=False), daemon=True).start()
    for _ in range(100):
        if "h" in box:
            break
        time.sleep(0.02)
    base = f"http://127.0.0.1:{box['h'].server_address[1]}/api/ode"
    try:
        st, h, j = _get(base + "/health", origin="https://jhuapl-bio.github.io")
        assert j["store"] is False and h["Access-Control-Allow-Origin"] == "https://jhuapl-bio.github.io"
        st, _, j = _get(base + "/jobs", "POST", json.dumps({"project_id": SHELF, "save": True}).encode(), origin="null")
        assert st == 403
        st, _, j = _get(base + "/jobs", "POST", json.dumps({"project_id": SHELF, "analyses": ["MiFish"]}).encode(),
                        origin="https://jhuapl-bio.github.io")
        job = _wait(base, j["job"]["id"])
        assert job["state"] == "done" and "build" not in [s["key"] for s in job["steps"]]
        st, _, r = _get(f"{base}/jobs/{job['id']}/result")
        res = r["result"]
        assert "runs" not in res and any(k.startswith("table:") for k in res["files"])
        hits = len(mock.hits)
        client.fetch_file(client.project(SHELF)["files"]["sampleMetadata"])     # served from memory
        assert len(mock.hits) == hits
    finally:
        box["h"].shutdown()
    assert not pd.exists() and not any(tmp_path.rglob("*.tsv"))


def test_builder_ode_no_store_option(tmp_path):
    import build_mbon_html as B
    old = B.ODE_PERSIST
    try:
        B.ODE_PERSIST = False
        assert B._attach_ode_catalog({"runs": []})["odeOptions"] == {"persist": False}
    finally:
        B.ODE_PERSIST = old
