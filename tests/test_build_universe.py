import json
import zipfile

import pytest

from evewatch.__main__ import main
from evewatch.universe import Universe, UniverseError, universe_from_sde_zip


def jsonl(rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def make_zip(path, systems=None, gates=None, with_meta=True):
    systems = systems if systems is not None else [
        {"_key": 1, "name": {"en": "Alpha", "ru": "Альфа"}, "securityStatus": 0.9},
        {"_key": 2, "name": {"en": "Beta-1"}, "securityStatus": -0.3},
        {"_key": 3, "name": {"en": "J123456"}, "securityStatus": -0.99},
    ]
    gates = gates if gates is not None else [
        {"_key": 10, "solarSystemID": 1, "destination": {"solarSystemID": 2, "stargateID": 11}},
        {"_key": 11, "solarSystemID": 2, "destination": {"solarSystemID": 1, "stargateID": 10}},
    ]
    with zipfile.ZipFile(path, "w") as z:
        if with_meta:
            z.writestr("_sde.jsonl", jsonl([{"_key": "sde", "buildNumber": 4242}]))
        z.writestr("mapSolarSystems.jsonl", jsonl(systems))
        z.writestr("mapStargates.jsonl", jsonl(gates))
        z.writestr("mapMoons.jsonl", "ignored\n")
    return path


def test_builds_graph_from_sde_zip(tmp_path):
    u = universe_from_sde_zip(make_zip(tmp_path / "sde.zip"))
    assert len(u) == 3 and u.build == 4242
    assert u.jumps("Alpha", "Beta-1") == 1
    assert u.jumps("Alpha", "J123456") is None  # wormhole system: no gates
    assert u.find_systems("camp in beta-1*") == ["Beta-1"]


def test_missing_build_info_is_ok(tmp_path):
    u = universe_from_sde_zip(make_zip(tmp_path / "sde.zip", with_meta=False))
    assert u.build is None


def test_bad_archives(tmp_path):
    not_zip = tmp_path / "x.zip"
    not_zip.write_bytes(b"nope")
    with pytest.raises(UniverseError, match="cannot open"):
        universe_from_sde_zip(not_zip)
    with pytest.raises(UniverseError, match="no systems"):
        universe_from_sde_zip(make_zip(tmp_path / "e.zip", systems=[], gates=[]))
    odd = make_zip(tmp_path / "o.zip", systems=[{"_key": 1, "name": "no-lang-dict"}])
    with pytest.raises(UniverseError, match="mapSolarSystems"):
        universe_from_sde_zip(odd)
    with zipfile.ZipFile(tmp_path / "m.zip", "w") as z:
        z.writestr("other.jsonl", "{}\n")
    with pytest.raises(UniverseError, match="not found in the archive"):
        universe_from_sde_zip(tmp_path / "m.zip")


def test_cli_build_universe_from_zip(tmp_path, capsys):
    archive = make_zip(tmp_path / "sde.zip")
    out = tmp_path / "out" / "universe.json"
    out.parent.mkdir()
    assert main(["-c", str(tmp_path / "none.toml"), "build-universe", "--zip", str(archive), "--output", str(out)]) == 0
    assert "3 systems and 1 gate links (SDE build 4242)" in capsys.readouterr().out
    assert Universe.load(out).jumps("Alpha", "Beta-1") == 1


def test_cli_build_universe_reports_bad_zip(tmp_path, capsys):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"x")
    assert main(["-c", str(tmp_path / "none.toml"), "build-universe", "--zip", str(bad), "--output", str(tmp_path / "u.json")]) == 1
    assert "cannot open" in capsys.readouterr().err
