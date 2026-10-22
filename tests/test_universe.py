import json

import pytest

from evewatch.universe import SystemInfo, Universe, UniverseError


def make_universe():
    names = [
        "Obe",
        "4-HWWF",
        "7-K5EL",
        "P3EN-E",
        "WH-JCA",
        "Y-ZXIO",
        "New Caldari",
        "Old Man Star",
        "Jita",
        "0J3L-V",  # not connected to anything
        "Hek",
    ]
    systems = [SystemInfo(i + 1, n, -0.5) for i, n in enumerate(names)]
    ids = {n: i + 1 for i, n in enumerate(names)}
    chain = ["Obe", "4-HWWF", "7-K5EL", "P3EN-E", "WH-JCA", "Y-ZXIO"]
    gates = [(ids[a], ids[b]) for a, b in zip(chain, chain[1:])]
    gates.append((ids["New Caldari"], ids["Jita"]))
    gates.append((ids["Jita"], ids["Old Man Star"]))
    return Universe(systems, gates, build=1)


def test_distances_bfs():
    u = make_universe()
    d = u.distances("Obe", 3)
    assert d == {"Obe": 0, "4-HWWF": 1, "7-K5EL": 2, "P3EN-E": 3}


def test_distances_is_case_insensitive_and_unknown_origin_is_empty():
    u = make_universe()
    assert u.distances("obe", 1) == {"Obe": 0, "4-HWWF": 1}
    assert u.distances("Nowhere", 5) == {}


def test_jumps_between_systems():
    u = make_universe()
    assert u.jumps("Obe", "Y-ZXIO") == 5
    assert u.jumps("Y-ZXIO", "obe") == 5
    assert u.jumps("Obe", "Obe") == 0
    assert u.jumps("Obe", "0J3L-V") is None  # unreachable
    assert u.jumps("Obe", "Nowhere") is None


def test_extra_links_shorten_paths():
    u = make_universe()
    assert u.jumps("Obe", "WH-JCA") == 4
    assert u.add_links([("Obe", "WH-JCA"), ("Obe", "Atlantis")]) == [("Obe", "Atlantis")]
    assert u.jumps("Obe", "WH-JCA") == 1
    assert u.jumps("Obe", "Y-ZXIO") == 2


def test_find_systems_plain_and_starred_links():
    u = make_universe()
    assert u.find_systems("p3en-e*  Khamul Corleone camped") == ["P3EN-E"]
    assert u.find_systems("WH-JCA  clr") == ["WH-JCA"]
    assert u.find_systems("v wh-jca, Pino 7-K5EL*") == ["WH-JCA", "7-K5EL"]


def test_find_systems_multiword_names_prefer_longest():
    u = make_universe()
    assert u.find_systems("hostile in New Caldari now") == ["New Caldari"]
    assert u.find_systems("Old Man Star +3") == ["Old Man Star"]


def test_find_systems_ignores_substrings_and_urls():
    u = make_universe()
    assert u.find_systems("P3EN-EX is not a system") == []
    assert u.find_systems("see https://dscan.info/v/Obe-P3EN-E now") == []
    assert u.find_systems("Observer") == []


def test_find_systems_aliases_and_ignore_list():
    u = make_universe()
    assert u.find_systems("Pen camp", aliases={"pen": "P3EN-E"}) == ["P3EN-E"]
    assert u.find_systems("Hek and Jita", ignore={"hek"}) == ["Jita"]
    assert u.find_systems("alias", aliases={"alias": "Nowhere"}) == []


def test_find_systems_digits_and_hyphens():
    u = make_universe()
    assert u.find_systems("4-HWWF*  Zans Adolt Algaert") == ["4-HWWF"]
    assert u.find_systems("+3 0J3L-V") == ["0J3L-V"]


def test_save_and_load_roundtrip(tmp_path):
    u = make_universe()
    path = tmp_path / "universe.json"
    u.save(path)
    loaded = Universe.load(path)
    assert len(loaded) == len(u)
    assert loaded.build == 1
    assert loaded.jumps("Obe", "Y-ZXIO") == 5
    assert loaded.find_systems("jita") == ["Jita"]
    assert not list(tmp_path.glob("*.tmp"))


def test_load_errors_are_friendly(tmp_path):
    with pytest.raises(UniverseError, match="build-universe"):
        Universe.load(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(UniverseError, match="cannot read"):
        Universe.load(bad)
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"version": 0}), encoding="utf-8")
    with pytest.raises(UniverseError, match="version"):
        Universe.load(old)
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"version": 1, "systems": [{"id": 1}], "gates": []}), encoding="utf-8")
    with pytest.raises(UniverseError, match="malformed"):
        Universe.load(broken)
