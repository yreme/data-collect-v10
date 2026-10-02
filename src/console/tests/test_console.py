import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sensorhub_console.main import Console, create_app
from sensorhub_console.store import AliasStore, ConfigStore

SAMPLE = Path(__file__).resolve().parents[2] / "configs" / "sensors.yaml"


@pytest.fixture()
def dirs(tmp_path):
    cfg = tmp_path / "configs"
    cfg.mkdir()
    shutil.copy(SAMPLE, cfg / "sensors.yaml")
    return cfg, tmp_path / "data"


def test_alias_store_persists(tmp_path):
    p = tmp_path / "a.json"
    s = AliasStore(p)
    s.set("camera/cam0", alias="左上角相机", note="吊具左侧")
    assert AliasStore(p).get("camera/cam0")["alias"] == "左上角相机"
    s.set("camera/cam0", alias="", note="")
    assert AliasStore(p).get("camera/cam0") == {}


def test_config_store_versions_and_rollback(dirs):
    cfg, _ = dirs
    cs = ConfigStore(cfg / "sensors.yaml")
    assert [v["version"] for v in cs.history()] == [1]
    original = cs.current()["content"]

    bad = cs.save("cameras:\n  devices:\n    - {name: BAD}\n", comment="bad")
    assert not bad["ok"] and bad["errors"]
    assert cs.current()["content"] == original

    edited = original.replace("hz: 20\n    trigger_mode", "hz: 10\n    trigger_mode")
    r = cs.save(edited, comment="相机改 10Hz")
    assert r["ok"] and r["changed"] and r["version"] == 2
    assert cs.save(edited, comment="same")["changed"] is False
    assert "-    hz: 20" in cs.diff(1, 2)

    cs.tag(1, "known_good")
    rb = cs.rollback(1)
    assert rb["version"] == 3 and cs.current()["content"] == original
    assert cs.current()["version"] == 3
    assert ConfigStore(cfg / "sensors.yaml").history()[0]["version"] == 3

    (cfg / "sensors.yaml").write_text(original + "\n# 外部修改\n", encoding="utf-8")
    assert ConfigStore(cfg / "sensors.yaml").history()[0]["comment"].startswith("启动时快照")


def test_config_store_prune_keeps_tagged(dirs):
    cfg, _ = dirs
    cs = ConfigStore(cfg / "sensors.yaml", max_versions=3)
    cs.tag(1, "known_good")
    base = cs.current()["content"]
    for i in range(5):
        cs.save(base + f"\n# {i}\n", comment=str(i))
    versions = [v["version"] for v in cs.history()]
    assert len(versions) == 3 and 1 in versions


def test_api_without_zenoh(dirs):
    cfg, data = dirs
    console = Console(config_dir=cfg, data_dir=data, session=None)
    with TestClient(create_app(console)) as c:
        ov = c.get("/api/overview").json()
        assert ov["prefix"] == "rig"
        cams = [n for n in ov["nodes"] if n["kind"] == "camera"]
        assert len(cams) == 7 and all(n["state"] in ("offline", "disabled") for n in cams)
        assert next(n for n in cams if n["name"] == "cam_corner_0")["alias"] == "右上角相机(Q1)"

        assert c.post("/api/aliases/camera/cam_corner_0", json={"alias": "Q1 相机", "note": "n"}).status_code == 200
        ov = c.get("/api/overview").json()
        assert next(n for n in ov["nodes"] if n["name"] == "cam_corner_0")["alias"] == "Q1 相机"
        assert c.post("/api/aliases/camera/BAD", json={"alias": "x"}).status_code == 400

        cur = c.get("/api/config").json()
        assert c.post("/api/config/validate", json={"content": "x: [1"}).json()["ok"] is False
        assert c.post("/api/config", json={"content": "cameras: {devices: [{name: B-1}]}"}).status_code == 422
        r = c.post("/api/config", json={"content": cur["content"] + "\n# edit\n", "comment": "t"}).json()
        assert r["version"] == 2
        assert "+# edit" in c.get("/api/config/diff", params={"a": 1}).text
        assert c.post("/api/config/rollback/1", json={}).json()["version"] == 3
        assert len(c.get("/api/config/history").json()) == 3
        assert c.get("/").status_code == 200 and "sensorhub" in c.get("/").text
        assert c.get("/static/app.js").status_code == 200
