#!/usr/bin/env python3
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
"""
make_example_data.py — generate the bundled SYNTHETIC example dataset.

Everything written here is invented: station names, coordinates, sample IDs,
read counts and ASV sequences are generated from a fixed random seed so the
output is reproducible. Species names are real marine taxa typical of a
temperate Atlantic coast, but their TaxIDs are illustrative only and the
detections are NOT real observations. No personal data is included.

Layout produced (mirrors a real data/projects/ tree, see docs/DATA_FORMATS.md)::

    data/example/
        dashboard_example.xlsx                    # samples + observations (API "xlsx" source)
        projects/
            report_manifest.xlsx                  # the controller sheet
            ExampleCoast/
                faire/ExampleCoast_FAIRe_checklist.xlsx      # sampleMetadata (coords + env)
                wilder/WLMEX0001_260720.xlsx                 # Wilder ASV workbook, cruise 1
                wilder/WLMEX0002_260925.xlsx                 # Wilder ASV workbook, cruise 2
                kraken2/ExampleDB_v1/EX_K2_RUN01/<sample>.kraken2.report.txt
                meta/                                        # concordance.json (ingest.py concordance)

Usage::

    python make_example_data.py                 # (re)write data/example/
    python make_example_data.py --out /tmp/ex   # somewhere else
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import edna_common as ec

log = logging.getLogger("make_example_data")

SEED = 20260701
PROJECT = "ExampleCoast"
DB_NAME = "ExampleDB_v1"
K2_RUN = "EX_K2_RUN01"

# Fictional stations on a shelf transect (nearshore -> offshore).
# (station, lat, lon, bottom depth m, distance index 0..1 offshore)
STATIONS = [
    ("EXS1", 38.705, -75.060, 8, 0.00),
    ("EXS2", 38.640, -74.930, 14, 0.20),
    ("EXS3", 38.575, -74.800, 19, 0.40),
    ("EXS4", 38.510, -74.665, 24, 0.60),
    ("EXS5", 38.445, -74.530, 30, 0.80),
    ("EXS6", 38.380, -74.395, 36, 1.00),
]
CRUISES = [("EXC01", date(2026, 7, 14), "WLMEX0001_260720", 900101),
           ("EXC02", date(2026, 9, 22), "WLMEX0002_260925", 900201)]

# Kraken2-style lineage nodes: code -> (taxid, name). TaxIDs illustrative.
L = {
    "euk": ("D", 2759, "Eukaryota"), "bac": ("D", 2, "Bacteria"),
    "metazoa": ("K", 33208, "Metazoa"), "viridi": ("K", 33090, "Viridiplantae"),
    "chordata": ("P", 7711, "Chordata"), "arthro": ("P", 6656, "Arthropoda"),
    "mollusca": ("P", 6447, "Mollusca"), "cnid": ("P", 6073, "Cnidaria"),
    "cteno": ("P", 10197, "Ctenophora"), "chloro": ("P", 3041, "Chlorophyta"),
    "bacil": ("P", 2836, "Bacillariophyta"), "pseudo": ("P", 1224, "Pseudomonadota"),
    "actino": ("C", 186623, "Actinopteri"), "mamm": ("C", 40674, "Mammalia"),
    "hexa": ("C", 72037, "Hexanauplia"), "mala": ("C", 6681, "Malacostraca"),
    "branch": ("C", 6658, "Branchiopoda"), "biv": ("C", 6544, "Bivalvia"),
    "scypho": ("C", 6142, "Scyphozoa"), "tenta": ("C", 10198, "Tentaculata"),
    "mamiello": ("C", 1035538, "Mamiellophyceae"), "medio": ("C", 33849, "Mediophyceae"),
    "gamma": ("C", 1236, "Gammaproteobacteria"),
}

# species, common name, Wilder group, assay target, TaxID, lineage keys,
# nearshore preference (-1 offshore .. +1 nearshore), base abundance, prevalence
SPECIES = [
    ("Anchoa mitchilli", "Bay anchovy", "Fish", "12S", 224718, ["euk", "metazoa", "chordata", "actino"], 0.8, 9000, 0.9),
    ("Brevoortia tyrannus", "Atlantic menhaden", "Fish", "12S", 55118, ["euk", "metazoa", "chordata", "actino"], 0.6, 6000, 0.8),
    ("Menidia menidia", "Atlantic silverside", "Fish", "12S", 8096, ["euk", "metazoa", "chordata", "actino"], 1.0, 3000, 0.6),
    ("Morone saxatilis", "Striped bass", "Fish", "12S", 34816, ["euk", "metazoa", "chordata", "actino"], 0.7, 1200, 0.4),
    ("Paralichthys dentatus", "Summer flounder", "Fish", "12S", 8257, ["euk", "metazoa", "chordata", "actino"], -0.2, 1500, 0.5),
    ("Pomatomus saltatrix", "Bluefish", "Fish", "12S", 8167, ["euk", "metazoa", "chordata", "actino"], -0.4, 2200, 0.5),
    ("Micropogonias undulatus", "Atlantic croaker", "Fish", "12S", 29154, ["euk", "metazoa", "chordata", "actino"], -0.1, 2500, 0.6),
    ("Tursiops truncatus", "Common bottlenose dolphin", "Mammals", "12S", 9739, ["euk", "metazoa", "chordata", "mamm"], 0.3, 800, 0.35),
    ("Phocoena phocoena", "Harbor porpoise", "Mammals", "12S", 9742, ["euk", "metazoa", "chordata", "mamm"], -0.5, 400, 0.2),
    ("Megaptera novaeangliae", "Humpback whale", "Mammals", "12S", 9773, ["euk", "metazoa", "chordata", "mamm"], -1.0, 300, 0.15),
    ("Acartia tonsa", "Copepod", "Crustaceans", "COI", 136180, ["euk", "metazoa", "arthro", "hexa"], 0.5, 15000, 0.95),
    ("Centropages typicus", "Copepod", "Crustaceans", "COI", 463189, ["euk", "metazoa", "arthro", "hexa"], -0.8, 5000, 0.7),
    ("Callinectes sapidus", "Blue crab", "Crustaceans", "COI", 6763, ["euk", "metazoa", "arthro", "mala"], 1.0, 1800, 0.5),
    ("Penilia avirostris", "Water flea", "Crustaceans", "COI", 124140, ["euk", "metazoa", "arthro", "branch"], -0.3, 2600, 0.5),
    ("Crassostrea virginica", "Eastern oyster", "Molluscs", "COI", 6565, ["euk", "metazoa", "mollusca", "biv"], 1.0, 2000, 0.45),
    ("Mytilus edulis", "Blue mussel", "Molluscs", "COI", 6550, ["euk", "metazoa", "mollusca", "biv"], 0.4, 1400, 0.45),
    ("Aurelia aurita", "Moon jelly", "Cnidarians", "18S", 6145, ["euk", "metazoa", "cnid", "scypho"], 0.2, 1100, 0.4),
    ("Mnemiopsis leidyi", "American comb jelly", "Other", "18S", 27923, ["euk", "metazoa", "cteno", "tenta"], 0.6, 3500, 0.6),
    ("Micromonas pusilla", "Green alga", "Green algae", "18S", 38833, ["euk", "viridi", "chloro", "mamiello"], 0.0, 4000, 0.85),
    ("Bathycoccus prasinos", "Green alga", "Green algae", "18S", 41875, ["euk", "viridi", "chloro", "mamiello"], -0.3, 3200, 0.8),
    ("Skeletonema costatum", "Diatom", "Diatoms", "18S", 2843, ["euk", "bacil", "medio"], 0.6, 5200, 0.75),
    ("Thalassiosira pseudonana", "Diatom", "Diatoms", "18S", 35128, ["euk", "bacil", "medio"], 0.1, 2400, 0.6),
    ("Vibrio vulnificus", None, "Bacteria", "16S", 672, ["bac", "pseudo", "gamma"], 0.8, 900, 0.3),
]


def _rng():
    return random.Random(SEED)


def build_samples():
    """One surface + one bottom sample per station per cruise, plus a field
    blank per cruise. Returns a list of dicts in FAIRe sampleMetadata terms."""
    out, n = [], 0
    rnd = _rng()
    for ci, (cruise, day, _job, _uid0) in enumerate(CRUISES):
        for st, lat, lon, zmax, off in STATIONS:
            for depth_lbl, zmin_, zmax_ in (("Surface", 1, 1), ("Bottom", zmax - 2, zmax)):
                n += 1
                summer = ci == 0
                surf = depth_lbl == "Surface"
                temp = (25.5 if summer else 22.0) - (0 if surf else (7.5 if summer else 4.0)) \
                    - 1.2 * off + rnd.uniform(-0.4, 0.4)
                sal = 29.0 + 3.0 * off + (0 if surf else 0.6) + rnd.uniform(-0.3, 0.3)
                out.append({
                    "samp_number": f"EX{n:04d}",
                    "samp_name": f"{st}_{'S' if surf else 'B'}_{day:%m%d%y}",
                    "Cruise Number": cruise,
                    "Station": st,
                    "Sample Depth": depth_lbl,
                    "samp_category": "sample",
                    "neg_cont_type": None,
                    "pos_cont_type": None,
                    "decimalLatitude": lat,
                    "decimalLongitude": lon,
                    "geo_loc_name": "Example Coast (synthetic)",
                    "eventDate": day.isoformat(),
                    "minimumDepthInMeters": zmin_,
                    "maximumDepthInMeters": zmax_,
                    "temp": round(temp, 1),
                    "salinity": round(sal, 2),
                    "samp_collect_device": "Niskin bottle",
                    "samp_store_sol": "Longmire's buffer",
                    "volume_filtered": "1L",
                    "env_broad_scale": "marine biome [ENVO:00000447]",
                    "_offshore": off, "_surface": surf, "_cruise": ci,
                })
        n += 1
        out.append({
            "samp_number": f"EX{n:04d}", "samp_name": f"FIELDBLANK_{day:%m%d%y}",
            "Cruise Number": cruise, "Station": "EXS1", "Sample Depth": "Surface",
            "samp_category": "negative control", "neg_cont_type": "field negative",
            "pos_cont_type": None, "decimalLatitude": STATIONS[0][1],
            "decimalLongitude": STATIONS[0][2], "geo_loc_name": "Example Coast (synthetic)",
            "eventDate": day.isoformat(), "minimumDepthInMeters": 0, "maximumDepthInMeters": 0,
            "temp": None, "salinity": None, "samp_collect_device": "deionised water blank",
            "samp_store_sol": "Longmire's buffer", "volume_filtered": "1L",
            "env_broad_scale": "marine biome [ENVO:00000447]",
            "_offshore": 0.0, "_surface": True, "_cruise": ci, "_blank": True,
        })
    for s in out:
        s["materialSampleID"] = s["samp_name"]
    return out


def simulate_reads(samples):
    """{samp_number: {species: reads}} with plausible spatial structure."""
    rnd = _rng()
    reads = {}
    for s in samples:
        row = {}
        if s.get("_blank"):
            # a little contamination in blanks
            for sp in rnd.sample(SPECIES, 2):
                row[sp[0]] = rnd.randint(3, 40)
            reads[s["samp_number"]] = row
            continue
        nearshore = 1 - s["_offshore"]
        for name, _c, _g, _t, _tid, _lin, pref, base, prev in SPECIES:
            # habitat suitability 0..1 from nearshore preference
            suit = 0.5 + 0.5 * pref * (2 * nearshore - 1)
            p = prev * (0.35 + 0.65 * suit)
            if rnd.random() > p:
                continue
            mult = (1.3 if s["_surface"] else 0.8) * (1.15 if s["_cruise"] == 0 else 0.9)
            val = int(base * suit * mult * rnd.lognormvariate(0, 0.6))
            if val > 0:
                row[name] = val
        reads[s["samp_number"]] = row
    return reads


# ---------------------------------------------------------------- writers
def write_faire(path: Path, samples):
    cols = ["samp_number", "samp_name", "materialSampleID", "Cruise Number", "Station",
            "Sample Depth", "samp_category", "neg_cont_type", "pos_cont_type",
            "decimalLatitude", "decimalLongitude", "geo_loc_name", "eventDate",
            "minimumDepthInMeters", "maximumDepthInMeters", "temp", "salinity",
            "samp_collect_device", "samp_store_sol", "volume_filtered", "env_broad_scale"]
    req = ["M", "HR", "M", "R", "R", "R", "M", "M", "M", "M", "M", "M", "M",
           "HR", "HR", "R", "R", "R", "R", "R", "M"]
    sec = ["Sample collection"] * len(cols)
    rows = [["# requirement_level"] + req[1:], ["# section"] + sec[1:], cols]
    rows += [[s.get(c) for c in cols] for s in samples]
    proj = pd.DataFrame({"term_name": ["project_name", "project_contact", "license", "note"],
                         "project_level": [PROJECT, "analyst@example.org", "CC0",
                                           "SYNTHETIC example data generated by make_example_data.py"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="sampleMetadata", header=False, index=False)
        proj.to_excel(xw, sheet_name="projectMetadata", index=False)


def _seq(rnd, n):
    return "".join(rnd.choice("ACGT") for _ in range(n))


def write_wilder(path: Path, job: str, uid0: int, samples, reads):
    rnd = random.Random(uid0)
    subs = [s for s in samples if s["Cruise Number"] == job[0]]
    meta_rows = [["Wilderlab Sample Batch Metadata", None], [None, None],
                 ["JobID:", job[1]], ["SubmissionDate:", job[2]],
                 ["AccountName:", "Example Lab (synthetic data)"],
                 ["ContactName:", "Example Analyst"], ["ContactEmail:", "analyst@example.org"],
                 ["AssayPanel:", "Example marine panel"], ["NumberOfSamples:", len(subs)],
                 [None, None]]
    hdr = ["UID", "ClientSampleID", "CollectedBy", "CollectionDate", "Latitude", "Longitude",
           "VolumeFilteredML", "EnvironmentType", "ClientNotes", "Assays"]
    meta = [r + [None] * (len(hdr) - 2) for r in meta_rows] + [hdr]
    uids = {}
    for i, s in enumerate(subs):
        uid = uid0 + i
        uids[s["samp_number"]] = uid
        meta.append([uid, s["samp_number"], "Example Team", s["eventDate"],
                     s["decimalLatitude"], s["decimalLongitude"], "1000",
                     "Control" if s.get("_blank") else "Marine",
                     "synthetic example sample", "CI,RV,GD,BX"])
    info = ["ScientificName", "Rank", "TaxID", "CommonName", "Group"]
    agg = [info + [uids[s["samp_number"]] for s in subs]]
    full = [["Sequence", "Target"] + info + [uids[s["samp_number"]] for s in subs]]
    for name, common, group, target, tid, _lin, *_ in SPECIES:
        if group == "Bacteria":
            continue                        # Wilder eukaryote panel: no bacteria
        vals = [reads[s["samp_number"]].get(name, 0) for s in subs]
        if not any(vals):
            continue
        agg.append([name, "species", tid, common, group] + vals)
        # split each species into 1-2 ASVs for the 'full' sheet
        k = 2 if rnd.random() < 0.4 else 1
        parts = [vals] if k == 1 else [[v - v // 3 for v in vals], [v // 3 for v in vals]]
        for part in parts:
            full.append([_seq(rnd, rnd.randint(80, 160)), target, name, "species", tid,
                         common, group] + part)
    # a genus-level row, which report builders must ignore for species views
    agg.append(["Acartia", "genus", 6835, None, "Crustaceans"] +
               [rnd.randint(0, 60) for _ in subs])
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(meta).to_excel(xw, sheet_name="metadata", header=False, index=False)
        pd.DataFrame(agg).to_excel(xw, sheet_name="aggregated", header=False, index=False)
        pd.DataFrame(full).to_excel(xw, sheet_name="full", header=False, index=False)


def kraken2_report_text(sample_reads, total_extra_unclassified):
    """Standard 6-column Kraken2 report (pct, clade, direct, rank, taxid, name)
    with two-space indentation per depth, built from the lineage table."""
    # build tree: node key (tuple path) -> clade reads
    tree = {}
    for name, *_rest in SPECIES:
        r = sample_reads.get(name, 0)
        if not r:
            continue
        sp = next(s for s in SPECIES if s[0] == name)
        lineage = [L[k] for k in sp[5]]
        genus = name.split()[0]
        path = [("R1", 131567, "cellular organisms")] + lineage + \
            [("G", sp[4] * 10 + 1, genus), ("S", sp[4], f"{name} ({sp[1]})" if sp[1] else name)]
        for i in range(1, len(path) + 1):
            key = tuple(path[:i])
            tree[key] = tree.get(key, 0) + r
    classified = sum(v for k, v in tree.items() if len(k) == 1)
    total = classified + total_extra_unclassified
    lines = []

    def pct(v):
        return f"{100.0 * v / total:6.2f}" if total else "  0.00"

    lines.append(f"{pct(total_extra_unclassified)}\t{total_extra_unclassified}\t"
                 f"{total_extra_unclassified}\tU\t0\tunclassified")
    lines.append(f"{pct(classified)}\t{classified}\t0\tR\t1\troot")

    def children(prefix):
        return sorted((k for k in tree if len(k) == len(prefix) + 1 and k[:len(prefix)] == prefix),
                      key=lambda k: -tree[k])

    def walk(prefix, depth):
        for k in children(prefix):
            code, tid, nm = k[-1]
            direct = tree[k] if code == "S" else 0
            lines.append(f"{pct(tree[k])}\t{tree[k]}\t{direct}\t{code}\t{tid}\t{'  ' * depth}{nm}")
            walk(k, depth + 1)

    walk((), 1)
    return "\n".join(lines) + "\n"


def write_kraken2(folder: Path, samples, reads):
    rnd = _rng()
    folder.mkdir(parents=True, exist_ok=True)
    for s in samples:
        # Kraken2 sees a bit more (bacteria) and slightly different counts
        r = {k: int(v * rnd.uniform(0.6, 1.4)) for k, v in reads[s["samp_number"]].items()}
        txt = kraken2_report_text(r, rnd.randint(200, 2500))
        (folder / f"{s['samp_name']}.kraken2.report.txt").write_text(txt, encoding="utf-8")


def write_manifest(path: Path, samples):
    rows = []
    for s in samples:
        ci = next(i for i, c in enumerate(CRUISES) if c[0] == s["Cruise Number"])
        rows.append({
            "sample_id": s["samp_number"],
            "run": f"Wilder_{CRUISES[ci][2].split('_')[0]}",
            "project": PROJECT,
            "project_dir": PROJECT,
            "cruise": CRUISES[ci][1].strftime("%Y%m%d"),
            "metadata": f"{PROJECT}_FAIRe_checklist.xlsx",
            "k2_report": f"{K2_RUN}/{s['samp_name']}.kraken2.report.txt",
            "asv_report": f"{CRUISES[ci][2]}.xlsx",
            "barcodes": None,
            "disable": False,
            "platform": "Illumina",
        })
    # demonstrates the `disable` switch: this row is ignored by both tools
    rows.append({"sample_id": "EX9999", "run": "Wilder_WLMEX0001", "project": PROJECT,
                 "project_dir": PROJECT, "cruise": "20260714",
                 "metadata": f"{PROJECT}_FAIRe_checklist.xlsx",
                 "k2_report": None, "asv_report": None, "barcodes": None,
                 "disable": True, "platform": "Illumina"})
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_excel(path, sheet_name="Sheet1", index=False)


def write_dashboard_workbook(path: Path, samples, reads):
    """Two-sheet workbook for the dashboard API's Excel data source."""
    srows = [{k: v for k, v in s.items() if not k.startswith("_")} for s in samples]
    orows = []
    lookup = {sp[0]: sp for sp in SPECIES}
    for s in samples:
        for name, r in reads[s["samp_number"]].items():
            sp = lookup[name]
            orows.append({"sample_id": s["samp_name"], "scientific_name": name,
                          "common_name": sp[1], "organism_group": sp[2],
                          "taxonomic_rank": "species", "read_count": r})
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(srows).to_excel(xw, sheet_name="samples", index=False)
        pd.DataFrame(orows).to_excel(xw, sheet_name="observations", index=False)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ec.DATA_DIR / "example"),
                    help="output folder (default: data/example)")
    ec.add_logging_args(ap)
    args = ap.parse_args(argv)
    ec.setup_logging("make_example_data", args.log_level, args.log_file, args.quiet)

    out = ec.resolve_path(args.out)
    proj = out / "projects" / PROJECT
    samples = build_samples()
    reads = simulate_reads(samples)

    write_faire(proj / "faire" / f"{PROJECT}_FAIRe_checklist.xlsx", samples)
    for ci, (cruise, day, job, uid0) in enumerate(CRUISES):
        sub_day = date(day.year, day.month, min(day.day + 6, 28))
        write_wilder(proj / "wilder" / f"{job}.xlsx",
                     (cruise, job.split("_")[0][3:], sub_day.isoformat()), uid0,
                     samples, reads)
    write_kraken2(proj / "kraken2" / DB_NAME / K2_RUN, samples, reads)
    (proj / "meta").mkdir(parents=True, exist_ok=True)
    write_manifest(out / "projects" / ec.MANIFEST_NAME, samples)
    write_dashboard_workbook(out / "dashboard_example.xlsx", samples, reads)
    n_det = sum(len(v) for v in reads.values())
    log.info("wrote synthetic example data to %s: %d samples, %d stations, %d species, "
             "%d detections", ec.rel(out), len(samples), len(STATIONS), len(SPECIES), n_det)
    return 0


if __name__ == "__main__":
    sys.exit(main())
