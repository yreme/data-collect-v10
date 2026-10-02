import time

from foxglove_messages import SceneConfig, build_scene_update
from udp_parser import OicrFlags, SmhFlags, build_packet, parse_packet


def _sample_packet() -> object:
    raw = build_packet(
        SmhFlags(spr_sp40=True, mh_spr_lcked=True),
        OicrFlags(),
        15000,
        8000,
        3000,
        12000,
        1,
    )
    packet = parse_packet(raw, "10.172.237.32", 12730, time.time())
    assert packet is not None
    return packet


def test_scene_entities_and_frame() -> None:
    packet = _sample_packet()
    cfg = SceneConfig(
        frame_id="ground",
        trolley_fixed_height_m=30.0,
        ground_size_m=80.0,
        marker_size_m=2.0,
        ground_alpha=0.08,
    )
    scene = build_scene_update(packet, 1_000_000_000, cfg)
    ids = [entity.id for entity in scene.entities]
    assert ids == [
        "ground_reference",
        "gantry_mc",
        "trolley_mt",
        "spreader_mh",
        "container_height_cntrh",
    ]
    for entity in scene.entities:
        assert entity.frame_id == "ground"


def test_scene_marker_positions() -> None:
    packet = _sample_packet()
    cfg = SceneConfig(
        frame_id="ground",
        trolley_fixed_height_m=50.0,
        ground_size_m=80.0,
        marker_size_m=1.0,
        ground_alpha=0.08,
    )
    scene = build_scene_update(packet, 1_000_000_000, cfg)

    gantry = next(e for e in scene.entities if e.id == "gantry_mc")
    trolley = next(e for e in scene.entities if e.id == "trolley_mt")
    spreader = next(e for e in scene.entities if e.id == "spreader_mh")
    container = next(e for e in scene.entities if e.id == "container_height_cntrh")

    assert gantry.cubes[0].pose.position.x == 3.0
    assert gantry.cubes[0].pose.position.y == 0.0

    assert trolley.cubes[0].pose.position.x == 3.0
    assert trolley.cubes[0].pose.position.y == 8.0
    assert trolley.cubes[0].pose.position.z == 50.0

    assert spreader.cubes[0].pose.position.x == 3.0
    assert spreader.cubes[0].pose.position.y == 8.0
    assert spreader.cubes[0].pose.position.z == 15.0

    assert container.triangles[0].points[0].z == 12.0
    assert len(container.cubes) == 4


if __name__ == "__main__":
    test_scene_entities_and_frame()
    test_scene_marker_positions()
    print("ok")
