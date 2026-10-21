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
"""Write the SYNTHETIC Ocean DNA Explorer fixtures used by tests/ode_mock.py
(tests/fixtures/ode/). Everything — projects, places, coordinates, organisms,
sequences, read counts — is invented from a fixed random seed; no real survey
data is included. It mimics the two file styles ODE serves:

* ``demo-shelf-2023`` — standard Tourmaline names (table_* / asvTaxaFeatures_*),
  `featureid` header, kingdom..species + Confidence, FAIRe sheets with '#'
  comment rows, M/D/YY dates, negative controls without coordinates, two assays.
* ``demo-reef-2025`` — phyloseq-style names (ps_*_ASV_table / ps_*_taxonomy), a
  blank first header cell, `domain` + lower-case `confidence`, ISO dates,
  double-%-encoded file names, A/B replicates per site.

    python tests/make_ode_fixtures.py          # rewrite tests/fixtures/ode/
"""
import hashlib
import json
import random
import re
import shutil
import string
from pathlib import Path

OUT = Path(__file__).resolve().parent / "fixtures" / "ode"
SEED = 20261021

# invented organisms: (class, order, family, genus, species) — all fictional
TAXA = [
    ("Actinopteri", "Demoformes", "Demoidae", "Pseudoscombrus", "Pseudoscombrus alpha"),
    ("Actinopteri", "Demoformes", "Demoidae", "Pseudoscombrus", "Pseudoscombrus beta"),
    ("Actinopteri", "Demoformes", "Demoidae", "Fictogadus", "Fictogadus minor"),
    ("Actinopteri", "Mockiformes", "Mockidae", "Exemplaria", "Exemplaria lucida"),
    ("Actinopteri", "Mockiformes", "Mockidae", "Exemplaria", "Exemplaria nocturna"),
    ("Actinopteri", "Mockiformes", "Testidae", "Synthetichthys", "Synthetichthys rubra"),
    ("Actinopteri", "Mockiformes", "Testidae", "Synthetichthys", ""),
    ("Actinopteri", "Placeholdiformes", "Placeholdidae", "", ""),
    ("Actinopteri", "Placeholdiformes", "", "", ""),
    ("Chondrichthyes", "Imaginiformes", "Imaginidae", "Notarealus", "Notarealus caudatus"),
    ("Mammalia", "Fabricata", "Fabricatidae", "Cetofictus", "Cetofictus demo"),
    ("Aves", "Inventiformes", "Inventidae", "Avisimulata", "Avisimulata marina"),
]


def rnd_id(r, n=30):
    return "".join(r.choice(string.ascii_letters + string.digits) for _ in range(n))


def feat_id(r):
    return hashlib.md5(str(r.random()).encode()).hexdigest()


def seq(r, n=170):
    return "".join(r.choice("ACGT") for _ in range(n))


def tsv(rows, eol="\n"):
    return eol.join("\t".join("" if v is None else str(v) for v in row) for row in rows) + eol


def faire_sample_sheet(header, rows):
    req = ["# requirement_level_code"] + ["M" if h in ("samp_name", "samp_category", "decimalLatitude",
                                                        "decimalLongitude", "eventDate") else "O" for h in header[1:]]
    sec = ["# section"] + ["Sample collection"] * (len(header) - 1)
    return tsv([req, sec, header] + rows)


def project_meta(r, pid, name, assays, extra):
    hdr = ["requirement_level_code", "section", "term_name", "project_level"] + assays
    rows = [["M", "Project", "project_id", pid] + [""] * len(assays),
            ["M", "Project", "project_name", name] + [""] * len(assays),
            ["R", "Project", "institution", "Example Marine Lab (synthetic)"] + [""] * len(assays),
            ["M", "Project", "project_contact", "demo@example.org"] + [""] * len(assays),
            ["R", "Project", "license", "CC0 (synthetic test data)"] + [""] * len(assays)]
    rows += [["M", "Project", k, v] + [""] * len(assays) for k, v in extra.items()]
    rows.append(["M", "PCR", "target_gene", ""] + ["12S rRNA"] * len(assays))
    return tsv([hdr] + rows, "\r\n")


def analysis_meta(pid, assay, run):
    return tsv([["term_name", "values"], ["project_id", pid], ["assay_name", assay],
                ["analysis_run_name", run], ["otu_seq_comp_appr", "synthetic"]], "\r\n")


def make_tables(r, libs, n_feat, style):
    """(feature_table_text, taxonomy_text) for one analysis."""
    feats, tax_rows, tab_rows = [], [], []
    for _ in range(n_feat):
        fid = feat_id(r)
        t = r.choice(TAXA + [None])                      # None -> unassigned feature
        counts = [r.choice([0, 0, 0, r.randint(1, 50), r.randint(50, 9000)]) for _ in libs]
        if not any(counts):
            counts[r.randrange(len(libs))] = r.randint(5, 500)
        tab_rows.append([fid] + counts)
        top = "Eukaryota" if t else "Unassigned"
        ranks = ([top, "Chordata"] + list(t)) if t else ["Unassigned", "", "", "", "", "", ""]
        lineage = ";".join(x for x in ranks if x)
        conf = round(r.uniform(0.7, 1.0), 7)
        tax_rows.append([fid, seq(r), lineage, lineage] + ranks + [conf])
        feats.append(fid)
    first = "" if style == "phyloseq" else "featureid"
    top = "domain" if style == "phyloseq" else "kingdom"
    cf = "confidence" if style == "phyloseq" else "Confidence"
    eol = "\r\n" if style == "phyloseq" else "\n"
    table = tsv([[first] + libs] + tab_rows, eol)
    taxo = tsv([["featureid", "dna_sequence", "taxonomy", "verbatimIdentification", top, "phylum", "class",
                 "order", "family", "genus", "species", cf]] + tax_rows, eol)
    return table, taxo


def main():
    r = random.Random(SEED)
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "files").mkdir(parents=True)
    files, projects, analyses = {}, [], []

    # ---------------- project 1: standard Tourmaline layout
    pid, pname = "demo-shelf-2023", "Synthetic shelf survey 2023 (demo data)"
    assays = ["Fish-12S-MiFish-U-Demo", "Fish-12S-Riaz-Demo"]
    hdr = ["samp_name", "samp_category", "neg_cont_type", "expedition_id", "station_id", "ctd_bottle_number",
           "decimalLatitude", "decimalLongitude", "geo_loc_name", "eventDate", "minimumDepthInMeters",
           "maximumDepthInMeters", "depth_category", "tot_depth_water_col", "temp", "salinity"]
    samples = []
    for i in range(14):
        m, d = r.choice([7, 8, 9, 10]), r.randint(1, 28)
        st = f"CTD{i + 1:02d}"
        dep = r.choice([50, 120, 190, 260])
        samples.append([f"DEMO23{m:02d}{d:02d}-{st}-{dep}", "sample", "not applicable", "DEMO23-01", st, st,
                        round(r.uniform(27.0, 29.5), 4), round(r.uniform(-88.5, -85.0), 4), "Synthetic Sea",
                        f"{m}/{d}/23", dep, dep, r.choice(["Mid", "Bottom", "Surface"]), dep + 40,
                        round(r.uniform(12, 26), 2), round(r.uniform(34.5, 36.4), 2)])
    for j, kind in enumerate(["field negative", "field negative", "extraction negative", "PCR negative"]):
        samples.append([f"Control{j + 1}_2023", "negative control", kind, "DEMO23-01", "not applicable",
                        "not applicable", "not applicable", "not applicable", "not applicable",
                        f"{r.choice([7, 8, 9])}/{r.randint(1, 28)}/23"] + ["not applicable"] * 6)
    sm_name = f"FAIRe_{pid}%20-%20sampleMetadata-{rnd_id(r)}.tsv"
    lm_name = f"FAIRe_{pid}%20-%20experimentRunMetadata-{rnd_id(r)}.tsv"
    pm_name = f"FAIRe_{pid}%20-%20projectMetadata-{rnd_id(r)}.tsv"
    files[sm_name] = faire_sample_sheet(hdr, samples)
    lib_rows, libs_by = [], {a: [] for a in assays}
    for k, s in enumerate(samples):
        for a, tag in zip(assays, ["MiFish", "Riaz"]):
            lib = f"{s[0]}_{tag}_S{k + 1}"
            libs_by[a].append(lib)
            lib_rows.append([s[0], a, "plate1", lib, "run1"])
    files[lm_name] = tsv([["# requirement_level_code", "M", "M", "M", "R"], ["# section", "PCR", "PCR",
                          "Library preparation sequencing", "Library preparation sequencing"],
                         ["samp_name", "assay_name", "pcr_plate_id", "lib_id", "seq_run_id"]] + lib_rows)
    files[pm_name] = project_meta(r, pid, pname, assays, {"expedition_id": "DEMO23-01"})
    counts = {"Samples": len(samples), "Analyses": 2, "Libraries": len(lib_rows)}
    projects.append({"id": 901, "project_id": pid, "project_name": pname, "institution": "Example Marine Lab (synthetic)",
                     "dateSubmitted": "2026-01-01T00:00:00.000Z", "projectMetadataFileUrl_ODE": "{FILES}/" + pm_name,
                     "sampleMetadataFileUrl_ODE": "{FILES}/" + sm_name, "libraryMetadataFileUrl_ODE": "{FILES}/" + lm_name,
                     "_count": counts})
    for n, (a, tag, trusted, nf) in enumerate(zip(assays, ["mifish", "riaz"], [True, False], [60, 40])):
        key = f"demo23_{tag}_v1_260101"
        run = f"demo23-{tag}-run1"
        tname, xname, aname = (f"table_{key}-{rnd_id(r)}.tsv", f"asvTaxaFeatures_{key}-{rnd_id(r)}.tsv",
                               f"analysisMetadata_{key}-{rnd_id(r)}.tsv")
        files[tname], files[xname] = make_tables(r, libs_by[a], nf, "standard")
        files[aname] = analysis_meta(pid, a, run)
        analyses.append({"id": 9101 + n, "project_id": pid, "analysis_run_name": run, "assay_name": a, "trusted": trusted,
                         "dateSubmitted": "2026-01-02T00:00:00.000Z", "asvFileUrl_ODE": "{FILES}/" + xname,
                         "occurrenceFileUrl_ODE": "{FILES}/" + tname, "analysisMetadataFileUrl_ODE": "{FILES}/" + aname,
                         "_count": {"Libraries": len(libs_by[a]), "Occurrences": 0, "Assignments": nf, "Taxonomies": 0}})

    # ---------------- project 2: phyloseq-style files
    pid, pname = "demo-reef-2025", "Synthetic reef eDNA 2025 (demo data)"
    assays = ["Fish-12S-MiFish-Demo-Reef", "Vertebrates-12S-MarVer1-Demo"]
    hdr = ["samp_name", "samp_category", "neg_cont_type", "materialSampleID", "cruise_id", "station_id",
           "replicate_number", "decimalLongitude", "decimalLatitude", "geo_loc_name", "eventDate", "tot_depth_water_col", "temp"]
    sites = [("Lagoon", "SiteA-1"), ("Lagoon", "SiteA-2"), ("Lagoon", "SiteB-1"), ("Crest", "SiteC-1"),
             ("Crest", "SiteC-2"), ("Crest", "SiteD-1"), ("Slope", "SiteE-1"), ("Slope", "SiteE-2")]
    samples = []
    for i, (place, site) in enumerate(sites):
        la, lo = round(r.uniform(24.5, 25.5), 6), round(r.uniform(-81.0, -80.0), 6)
        day = f"2025-05-{20 + i % 8:02d}"
        for rep in "AB":
            samples.append([f"DEMO_{i + 1:02d}{rep}_{place}_{site}_{day}", "sample", "not applicable", f"{site}_{i + 1}{rep}",
                            "DEMO_REEF1", f"{place}_{site}", rep, lo, la, place, f"{day}T{r.randint(12, 17)}:{r.choice(['00', '15', '40'])}:00Z",
                            r.randint(5, 25), round(r.uniform(26, 31), 2)])
    sm_name = f"FAIRe-DEMO_{pid}%2520-%2520sampleMetadata-{rnd_id(r)}.tsv"
    lm_name = f"FAIRe-DEMO_{pid}%2520-%2520experimentRunMetadata-{rnd_id(r)}.tsv"
    pm_name = f"FAIRe-DEMO_{pid}%2520-%2520projectMetadata-{rnd_id(r)}.tsv"
    files[sm_name] = faire_sample_sheet(hdr, samples)
    lib_rows, libs_by = [], {a: [] for a in assays}
    for s in samples:
        for a, tag in zip(assays, ["12S_MF", "12S_MV"]):
            idx = re.match(r"DEMO_(\d+[AB])", s[0]).group(1)
            lib = f"DEMO1_{tag}_{idx}"
            libs_by[a].append(lib)
            lib_rows.append([s[0], a, "na", lib, "run1"])
    files[lm_name] = tsv([["# requirement_level_code", "M", "M", "M", "M"], ["# section", "PCR", "PCR", "PCR", "PCR"],
                          ["samp_name", "assay_name", "pcr_plate_id", "lib_id", "seq_run_id"]] + lib_rows, "\r\n")
    files[pm_name] = project_meta(r, pid, pname, assays, {})
    projects.insert(0, {"id": 902, "project_id": pid, "project_name": pname, "institution": "Example Reef Institute (synthetic)",
                        "dateSubmitted": "2026-02-01T00:00:00.000Z", "projectMetadataFileUrl_ODE": "{FILES}/" + pm_name,
                        "sampleMetadataFileUrl_ODE": "{FILES}/" + sm_name, "libraryMetadataFileUrl_ODE": "{FILES}/" + lm_name,
                        "_count": {"Samples": len(samples), "Analyses": 2, "Libraries": len(lib_rows)}})
    for n, (a, tag, trusted, nf) in enumerate(zip(assays, ["mifish", "marver1"], [True, False], [50, 30])):
        run = f"demoreef-{tag}-run1"
        tname, xname = f"ps_{tag}_ASV_table-{rnd_id(r)}.tsv", f"ps_{tag}_taxonomy-{rnd_id(r)}.tsv"
        aname = f"{run}_analysisMetadata-{rnd_id(r)}.tsv"
        files[tname], files[xname] = make_tables(r, libs_by[a], nf, "phyloseq")
        files[aname] = analysis_meta(pid, a, run)
        analyses.append({"id": 9201 + n, "project_id": pid, "analysis_run_name": run, "assay_name": a, "trusted": trusted,
                         "dateSubmitted": "2026-02-02T00:00:00.000Z", "asvFileUrl_ODE": "{FILES}/" + xname,
                         "occurrenceFileUrl_ODE": "{FILES}/" + tname, "analysisMetadataFileUrl_ODE": "{FILES}/" + aname,
                         "_count": {"Libraries": len(libs_by[a]), "Occurrences": 0, "Assignments": nf, "Taxonomies": 0}})

    fmap = {}
    for name, text in sorted(files.items()):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
        (OUT / "files" / safe).write_text(text, encoding="utf-8", newline="")
        fmap[name] = safe
    (OUT / "api.json").write_text(json.dumps({"synthetic": True, "seed": SEED, "projects": projects,
                                              "analyses": analyses, "files": fmap}, indent=1), encoding="utf-8")
    print(f"wrote {len(fmap)} synthetic files + api.json to {OUT}")


if __name__ == "__main__":
    main()
