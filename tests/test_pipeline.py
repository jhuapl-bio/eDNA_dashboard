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
