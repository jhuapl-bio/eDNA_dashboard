"""
Tests for the spreadsheet data-source adapter and the Explore cross-filter.

Generates a small in-memory workbook (banner row + header row + data, like the
real MBON tracker) so CI needs no committed binary fixture.
"""

import os
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import datasources as ds  # noqa: E402


@pytest.fixture(scope="module")
def workbook():
    path = os.path.join(tempfile.mkdtemp(prefix="mbon_test_"), "tracker.xlsx")
    samples = [
        ["SAMPLES — banner"] + [None] * 8,
        ["SAMPLE_ID", "samp_name", "Station", "decimalLatitude", "decimalLongitude",
         "minimumDepthInMeters", "salinity", "temp", "eventDate"],
        ["A", "A", "St1", 38.5, -73.9, 5,  30.0, 12.0, "2024-03-15"],
        ["B", "B", "St2", 41.0, -70.0, 60, 34.5, 8.0,  "2024-03-16"],
        ["C", "C", "St1", 38.6, -73.8, 8,  29.0, 19.0, "2024-03-17"],
    ]
    obs = [
        ["WILDER — banner"] + [None] * 5,
        ["sample_id", "scientific_name", "common_name", "organism_group",
         "taxonomic_rank", "read_count"],
        ["A", "Gadus morhua", "Atlantic cod", "Fish", "species", 1000],
        ["A", "Delphinus delphis", "Common dolphin", "Mammal", "species", 500],
        ["B", "Gadus morhua", "Atlantic cod", "Fish", "species", 200],
        ["C", "Megaptera novaeangliae", "Humpback whale", "Mammal", "species", 800],
    ]
    with pd.ExcelWriter(path) as xl:
        pd.DataFrame(samples).to_excel(xl, sheet_name="Samples", header=False, index=False)
        pd.DataFrame(obs).to_excel(xl, sheet_name="Wilder Results", header=False, index=False)
    return str(path)


@pytest.fixture(scope="module")
def source(workbook):
    return ds.build_source({"id": "t", "name": "T", "kind": "xlsx",
                            "config": {"kind": "xlsx", "path": workbook}})


def test_auto_mapping(source):
    sch = source.explore_schema()
    assert sch["samples_sheet"] == "Samples"
    assert sch["obs_sheet"] == "Wilder Results"
    assert sch["mapping"]["samples"]["depth"] == "minimumDepthInMeters"
    assert sch["mapping"]["samples"]["key"] == "samp_name"
    assert sch["mapping"]["obs"]["reads"] == "read_count"
    assert set(sch["env_available"]) >= {"depth", "salinity", "temp"}


def test_schema_ranges(source):
    sch = source.explore_schema()
    assert sch["ranges"]["depth"] == [5.0, 60.0]
    assert sch["groups"] == ["Fish", "Mammal"]


def test_unfiltered_counts(source):
    r = source.explore_query({})
    assert r["counts"]["samples"] == 3
    assert r["counts"]["species"] == 3
    assert r["counts"]["total_reads"] == 2500


def test_depth_filter(source):
    r = source.explore_query({"depth": [None, 10]})  # excludes sample B (depth 60)
    keys = {s["sample_key"] for s in r["samples"]}
    assert keys == {"A", "C"}


def test_group_filter(source):
    r = source.explore_query({"groups": ["Mammal"]})
    names = {s["scientific_name"] for s in r["species"]}
    assert names == {"Delphinus delphis", "Megaptera novaeangliae"}


def test_species_filter_and_envelope(source):
    r = source.explore_query({"species": ["Gadus morhua"]})
    assert r["counts"]["samples"] == 2  # A and B
    env = {e["scientific_name"]: e for e in r["envelopes"]}
    assert env["Gadus morhua"]["depth"]["min"] == 5.0
    assert env["Gadus morhua"]["depth"]["max"] == 60.0


def test_bbox_filter(source):
    r = source.explore_query({"bbox": {"s": 38.0, "n": 39.0, "w": -74.0, "e": -73.0}})
    keys = {s["sample_key"] for s in r["samples"]}
    assert keys == {"A", "C"}  # B is at 41N/-70


def test_multiple_bbox_filter(source):
    r = source.explore_query({"bboxes": [
        {"s": 38.0, "n": 39.0, "w": -74.0, "e": -73.0},
        {"s": 40.5, "n": 41.5, "w": -70.5, "e": -69.5},
    ]})
    keys = {s["sample_key"] for s in r["samples"]}
    assert keys == {"A", "B", "C"}


def test_combined_filter(source):
    # shallow + Fish should leave only Gadus morhua in samples A & C
    r = source.explore_query({"depth": [None, 10], "groups": ["Fish"]})
    names = {s["scientific_name"] for s in r["species"]}
    assert names == {"Gadus morhua"}
    assert r["counts"]["samples"] == 1  # only A has a Fish at depth<=10


def test_envelopes_accept_numeric_strings():
    r = ds._assemble(
        source=None,
        species=[{"scientific_name": "Gadus morhua", "total_reads": 3}],
        samples=[],
        points=[
            {"scientific_name": "Gadus morhua", "organism_group": "Fish", "depth": "5"},
            {"scientific_name": "Gadus morhua", "organism_group": "Fish", "depth": "7"},
            {"scientific_name": "Gadus morhua", "organism_group": "Fish", "depth": "not recorded"},
        ],
    )

    assert r["envelopes"][0]["depth"] == {"min": 5.0, "med": 6.0, "max": 7.0, "n": 2}
