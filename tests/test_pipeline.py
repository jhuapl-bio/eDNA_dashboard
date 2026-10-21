# ##############################################################################################
# # Copyright 2026 The Johns Hopkins University Applied Physics Laboratory LLC
# # All rights reserved.
# # Permission is hereby granted, free of charge, to any person obtaining a copy of this
# # software and associated documentation files (the "Software"), to deal in the Software
# # without restriction, including without limitation the rights to use, copy, modify,
# # merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# # permit persons to whom the Software is furnished to do so.
# #
# # THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# # INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# # PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# # LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# # TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
# # OR OTHER DEALINGS IN THE SOFTWARE.
# #
"""End-to-end tests for the report builder, ingest validation and the
input failsafes, all run against freshly generated synthetic example data."""
import json
import re
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_mbon_html as B  # noqa: E402
import edna_common as ec  # noqa: E402
import ingest as I  # noqa: E402
import make_example_data as M  # noqa: E402


@pytest.fixture()
def example(tmp_path):
    """A private copy of the synthetic example data (so tests can break it)."""
    out = tmp_path / "example"
    assert M.main(["--out", str(out), "--log-file", "-", "-q"]) == 0
    return out / "projects"


def _payload(html_path):
    t = Path(html_path).read_text(encoding="utf-8")
    m = re.search(r"/\*@@MBON_DATA_START@@\*/(.*?)/\*@@MBON_DATA_END@@\*/", t, re.S)
    assert m, "data markers missing from rendered report"
    return json.loads(m.group(1))


def _build(projects, out, *extra):
    return B.main(["--projects-dir", str(projects), "-o", str(out),
                   "--log-file", "-", "-q", *extra])


def test_build_example_report(example, tmp_path):
    out = tmp_path / "report.html"
    assert _build(example, out) == 0
    d = _payload(out)
    kinds = sorted(r["kind"] for r in d["runs"])
    assert kinds == ["kraken2", "manifest"]          # one Kraken2 + one ASV run
    for r in d["runs"]:
        assert len(r["stations"]) == 6
        assert r["taxa"], "no taxa parsed"
        assert all(-90 <= s["lat"] <= 90 for s in r["stations"])
    # disabled manifest row must not appear
    assert not any(s["sampleId"] == "EX9999" for r in d["runs"] for s in r["samples"])


def test_validate_ok_and_missing_file(example, tmp_path):
    assert _build(example, tmp_path / "x.html", "--validate") == 0
    victim = next((example / "ExampleCoast" / "kraken2").rglob("*.kraken2.report.txt"))
    victim.unlink()
    assert _build(example, tmp_path / "x.html", "--validate") == 1
    assert not (tmp_path / "x.html").exists(), "--validate must not write a report"


def test_corrupt_workbook_is_skipped_not_fatal(example, tmp_path):
    wb = next((example / "ExampleCoast" / "wilder").glob("*.xlsx"))
    wb.write_bytes(b"this is not a zip file")
    out = tmp_path / "r.html"
    rc = _build(example, out)
    assert rc == 1                       # errors were logged ...
    assert out.exists()                  # ... but the report was still written
    d = _payload(out)
    assert any(r["kind"] == "kraken2" and r["stations"] for r in d["runs"])


def test_missing_projects_dir_is_clean_error(tmp_path):
    assert _build(tmp_path / "nope", tmp_path / "r.html") == 2


def test_skip_runs_flag(example, tmp_path):
    out = tmp_path / "r.html"
    assert _build(example, out, "--skip-runs", "Wilder_WLMEX0002") == 0
    d = _payload(out)
    n = {len(r["samples"]) for r in d["runs"]}
    assert max(n) == 13                  # only cruise 1 (12 samples + 1 blank)


def test_payload_escapes_script_close():
    js = B.payload_to_js({"x": "</script><script>alert(1)</script>"})
    assert "</script>" not in js
    assert json.loads(js.replace("<\\/", "</"))["x"].startswith("</script>")


def test_coordinates_failsafes():
    assert B._coerce_latlon("abc", 3) == (None, None)
    assert B._coerce_latlon(None, None) == (None, None)
    assert B._coerce_latlon(0, 0) == (None, None)
    assert B._coerce_latlon(-120.5, 36.0) == (36.0, -120.5)      # swapped -> fixed
    assert B._coerce_latlon(95, 200) == (None, None)


def test_kraken_parser_rejects_garbage(tmp_path):
    f = tmp_path / "bad.kraken2.report.txt"
    f.write_text("hello\nworld\n")
    assert B.parse_kraken2_report(f) == []
    assert B.parse_kraken2_report(tmp_path / "missing.report") == []


def test_ingest_validate_and_concordance(example):
    args = ["--projects-dir", str(example), "--log-file", "-", "-q"]
    assert I.main(args + ["validate"]) == 0
    assert I.main(args + ["concordance", "--all"]) == 0
    conc = json.loads((example / "ExampleCoast" / "meta" / "concordance.json").read_text())
    assert len(conc["kraken2"]) == 26 and not conc["unmatched"]["kraken2"]
    assert len(conc["wilder"]) == 26 and not conc["unmatched"]["wilder"]


def test_ingest_manifest_missing_column(example):
    man = example / ec.MANIFEST_NAME
    df = pd.read_excel(man).drop(columns=["sample_id"])
    df.to_excel(man, index=False)
    with pytest.raises(ec.InputError, match="sample_id"):
        I.read_manifest(man)


def test_ingest_destructive_needs_yes(example):
    args = ["--projects-dir", str(example), "--log-file", "-", "-q"]
    # stdin is not a TTY under pytest -> must refuse without --yes
    assert I.main(args + ["delete", "--project", "ExampleCoast"]) == 1


def test_require_file_messages(tmp_path):
    with pytest.raises(ec.InputError, match="not found"):
        ec.require_file(tmp_path / "x.xlsx")
    empty = tmp_path / "e.xlsx"
    empty.write_bytes(b"")
    with pytest.raises(ec.InputError, match="empty"):
        ec.require_file(empty)
    lock = tmp_path / "~$book.xlsx"
    lock.write_bytes(b"x")
    with pytest.raises(ec.InputError, match="lock"):
        ec.require_file(lock)


def test_committed_example_matches_generator(tmp_path):
    """data/example must be reproducible from make_example_data.py."""
    out = tmp_path / "regen"
    M.main(["--out", str(out), "--log-file", "-", "-q"])
    committed = ROOT / "data" / "example" / "projects"
    for f in (out / "projects").rglob("*.kraken2.report.txt"):
        rel = f.relative_to(out / "projects")
        assert (committed / rel).read_text() == f.read_text(), rel
    shutil.rmtree(out)


# ---------------------------------------------------------------- Ocean DNA Explorer
def _write_ode_project(projects):
    """A tiny Ocean DNA Explorer project: FAIRe TSVs with '#' comment rows plus a
    Tourmaline feature table / asvTaxaFeatures pair (random download suffixes)."""
    proj = projects / "ode-demo"
    (proj / "faire").mkdir(parents=True)
    (proj / "tourmaline").mkdir()
    (proj / "faire" / "FAIRe_ode-demo - sampleMetadata-AbCdEfGhIjKlMnOpQr.tsv").write_text(
        "# requirement_level_code\tM\tM\tO\tO\tM\tM\tO\tO\tO\tO\tO\n"
        "# section\tSample information\tx\tx\tx\tx\tx\tx\tx\tx\tx\tx\n"
        "samp_name\tdecimalLatitude\tdecimalLongitude\tctd_bottle_number\texpedition_id"
        "\tsamp_category\tneg_cont_type\tdepth_category\teventDate\tminimumDepthInMeters"
        "\tmaximumDepthInMeters\ttemp\n"
        "S1-CTD1-100\t28.5\t-86.0\tCTD1\tX19-01\tsample\tnot applicable\tBottom\t7/6/19\t100\t100\t15.1\n"
        "S2-CTD2-50\t28.7\t-86.2\tCTD2\tX19-01\tsample\tnot applicable\tMid-water\t7/8/19\t50\t50\t18.2\n"
        "S3-CTD2-120\t28.7\t-86.2\tCTD2\tX19-01\tsample\tnot applicable\tBottom\t7/8/19\t120\t120\t14.0\n"
        "ExtNeg_1\tnot applicable\tnot applicable\tnot applicable\tnot applicable\tnegative control"
        "\textraction negative\tnot applicable\t9/10/19\tnot applicable\tnot applicable\tnot applicable\n")
    (proj / "faire" / "FAIRe_ode-demo - experimentRunMetadata-ZyXwVuTsRqPoNmLk (2).tsv").write_text(
        "# requirement_level_code\tM\tM\tM\n# section\tPCR\tPCR\tLib\n"
        "samp_name\tassay_name\tlib_id\tseq_run_id\n"
        "S1-CTD1-100\tFish-12S-MiFish-Miya-Stoeckle\tS1-CTD1-100_MiFish_S1\trun_mifish\n"
        "S2-CTD2-50\tFish-12S-MiFish-Miya-Stoeckle\tS2-CTD2-50_MiFish_S2\trun_mifish\n"
        "S3-CTD2-120\tFish-12S-MiFish-Miya-Stoeckle\tS3-CTD2-120_MiFish_S3\trun_mifish\n"
        "ExtNeg_1\tFish-12S-MiFish-Miya-Stoeckle\tExtNeg_1_MiFish_S4\trun_mifish\n")
    (proj / "faire" / "FAIRe_ode-demo - projectMetadata-QqWwEeRrTtYyUuIi.tsv").write_text(
        "requirement_level_code\tsection\tterm_name\tproject_level\tFish-12S-MiFish-Miya-Stoeckle\n"
        "R\tProject\tproject_name\tDemo cruise\t\n"
        "HR\tPCR\ttarget_gene\t\t12S rRNA\n")
    (proj / "tourmaline" / "table_demo_mifish_v1-T9UVz1iHJ3CWCBbyKyVL.tsv").write_text(
        "featureid\tS1-CTD1-100_MiFish_S1\tS2-CTD2-50_MiFish_S2\tS3-CTD2-120_MiFish_S3\tExtNeg_1_MiFish_S4\n"
        "aaa\t100\t0\t5\t0\nbbb\t0\t40\t0\t2\nccc\t7\t7\t0\t0\nddd\t3\t0\t0\t0\n")
    (proj / "tourmaline" / "asvTaxaFeatures_demo_mifish_v1-gSmSC3yZ1DGFSoejVu.tsv").write_text(
        "featureid\tdna_sequence\ttaxonomy\tverbatimIdentification\tkingdom\tphylum\tclass\torder"
        "\tfamily\tgenus\tspecies\tConfidence\n"
        "aaa\tACGT\tEukaryota;Chordata;Actinopteri;Stomiiformes;Sternoptychidae;Maurolicus;Maurolicus weitzmani"
        "\tx\tEukaryota\tChordata\tActinopteri\tStomiiformes\tSternoptychidae\tMaurolicus\tMaurolicus weitzmani\t0.99\n"
        "bbb\tACGA\tEukaryota;Chordata;Actinopteri;Scombriformes\tx\tEukaryota\tChordata\tActinopteri"
        "\tScombriformes\t\t\t\t0.97\n"
        "ccc\tACGC\tEukaryota;Chordata;Actinopteri;Stomiiformes;Sternoptychidae;Maurolicus;Maurolicus weitzmani"
        "\tx\tEukaryota\tChordata\tActinopteri\tStomiiformes\tSternoptychidae\tMaurolicus\tMaurolicus weitzmani\t0.98\n"
        "ddd\tACGG\tBacteria\tx\tBacteria\t\t\t\t\t\t\t0.8\n")
    man = projects / ec.MANIFEST_NAME
    df = pd.read_excel(man)
    row = {c: None for c in df.columns}
    row.update({"sample_id": "demo_mifish", "run": "run_mifish", "project": "ode-demo",
                "metadata": "FAIRe_ode-demo - sampleMetadata-AbCdEfGhIjKlMnOpQr.tsv; "
                            "FAIRe_ode-demo - experimentRunMetadata-ZyXwVuTsRqPoNmLk (2).tsv; "
                            "FAIRe_ode-demo - projectMetadata-QqWwEeRrTtYyUuIi.tsv",
                "ode_report": "table_demo_mifish_v1"})
    if "project_dir" in row:
        row["project_dir"] = "ode-demo"
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_excel(man, index=False)
    return proj


def test_faire_tsv_with_comment_rows(example):
    proj = _write_ode_project(example)
    f = next((proj / "faire").glob("*sampleMetadata*.tsv"))
    smap = B.load_station_map_from_file(f)
    assert set(smap) == {"S1-CTD1-100", "S2-CTD2-50", "S3-CTD2-120", "ExtNeg_1"}
    s1 = smap["S1-CTD1-100"]
    assert s1["Station"] == "CTD1" and s1["Cruise Number"] == "X19-01"
    assert s1["Sample Depth"] == "Bottom" and s1["eventDate"] == "2019-07-06"
    assert B.faire_file_kind(next((proj / "faire").glob("*experimentRun*"))) == "experimentRun"
    assert B.faire_file_kind(next((proj / "faire").glob("*projectMetadata*"))) == "project"


def test_ode_tourmaline_run(example, tmp_path):
    _write_ode_project(example)
    out = tmp_path / "r.html"
    assert _build(example, out) == 0
    d = _payload(out)
    kinds = sorted(r["kind"] for r in d["runs"])
    assert kinds == ["kraken2", "manifest", "ode"]          # other formats still load
    r = next(r for r in d["runs"] if r["kind"] == "ode")
    assert "MiFish" in r["label"]
    assert len(r["stations"]) == 2                          # S2 + S3 share CTD2's position
    sids = {s["sampleId"]: s for s in r["samples"]}
    assert set(sids) == {"S1-CTD1-100", "S2-CTD2-50", "S3-CTD2-120", "ExtNeg_1"}
    assert sids["ExtNeg_1"]["category"] == "control"
    assert sids["S1-CTD1-100"]["category"] == "sample"
    assert sids["S1-CTD1-100"]["meta"]["Library"] == "S1-CTD1-100_MiFish_S1"
    taxa = {t["species"]: t for t in r["taxa"]}
    # two ASVs collapse into one species; coarser hits are labelled by rank
    assert taxa["Maurolicus weitzmani"]["reads"] == 119 and taxa["Maurolicus weitzmani"]["asvCount"] == 2
    assert "Scombriformes (order)" in taxa and "Bacteria (kingdom)" in taxa
    assert r["meta"]["Project name"] == "Demo cruise" and r["meta"]["Target gene"] == "12S rRNA"
    assert _build(example, tmp_path / "v.html", "--validate") == 0


def test_ode_pairing_and_lib_suffixes(tmp_path):
    assert B._ode_pair_key("table_gu1901_mifish_v2024.10_241001-T9UVz1iHJ3CWCBbyKyVLMa4ridCZai.tsv") == \
        B._ode_pair_key("asvTaxaFeatures_gu1901_mifish_v2024.10_241001-gSmSC3yZ1DGFSoejVuxAAKKFDtDGui.tsv")
    smap = {"GU190706-CTD11-220": {}}
    res = lambda q: q if q in smap else None
    assert B.ode_lib_to_sample("GU190706-CTD11-220_MiFish_S30", {}, res)[0] == "GU190706-CTD11-220"
    assert B._ode_short_assay("Fish-12S-MiFish-Miya-Stoeckle") == "MiFish"
