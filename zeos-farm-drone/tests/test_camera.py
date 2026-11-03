"""The drone's camera, against a stand-in for Gazebo's frames.

``Camera`` is real here, thread and all; only taking a frame is replaced, by a
function that writes the frame's name into the file it was asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zeos_farm_drone.camera import LIVE, Camera, image_topic


def frames(out: list[str]):  # noqa: ANN201 -- a stand-in for _snap
    def snap(topic: str, path: Path) -> None:
        out.append(path.name)
        path.write_text(topic)

    return snap


def test_a_photo_is_the_frame_taken_for_it(tmp_path: Path) -> None:
    taken: list[str] = []
    camera = Camera("/cam", tmp_path, every_s=3600, snap=frames(taken))
    camera.snap("p-0001")
    camera.close()
    assert (tmp_path / "p-0001.png").read_text() == "/cam"
    assert ".p-0001.png" in taken  # written aside, then renamed into place
    assert not (tmp_path / ".p-0001.png").exists()


def test_between_photos_the_page_gets_what_the_camera_sees(tmp_path: Path) -> None:
    taken: list[str] = []
    camera = Camera("/cam", tmp_path, every_s=0.01, snap=frames(taken))
    while f".{LIVE}.png" not in taken:
        pass
    camera.close()
    assert (tmp_path / f"{LIVE}.png").exists()


def test_a_new_morning_files_its_photos_elsewhere(tmp_path: Path) -> None:
    camera = Camera("/cam", tmp_path / "a", every_s=3600, snap=frames([]))
    camera.file_into(tmp_path / "b")
    camera.snap("p-0001")
    camera.close()
    assert (tmp_path / "b" / "p-0001.png").exists()
    assert not (tmp_path / "a" / "p-0001.png").exists()


def test_a_frame_gazebo_would_not_give_reaches_the_kernels_thread(tmp_path: Path) -> None:
    def broken(topic: str, path: Path) -> None:
        raise RuntimeError(f"no frame from {topic}")

    camera = Camera("/cam", tmp_path, every_s=3600, patience_s=0, snap=broken)
    camera._thread.join(10)  # pyright: ignore[reportPrivateUsage] -- its first frame failed
    with pytest.raises(RuntimeError, match="no frame from /cam"):
        camera.snap("p-0001")


def test_a_gazebo_busy_loading_is_asked_again(tmp_path: Path) -> None:
    taken: list[str] = []
    snap = frames(taken)

    def busy_once(topic: str, path: Path) -> None:
        if not taken:
            taken.append("busy")
            raise RuntimeError(f"no frame from {topic}")
        snap(topic, path)

    camera = Camera("/cam", tmp_path, every_s=3600, snap=busy_once)
    camera.snap("p-0001")
    camera.close()
    camera.check()
    assert (tmp_path / "p-0001.png").exists()


def test_a_camera_silent_for_its_patience_stops_the_farm(tmp_path: Path) -> None:
    calls: list[str] = []

    def then_nothing(topic: str, path: Path) -> None:
        calls.append(path.name)
        if len(calls) > 1:
            raise RuntimeError(f"no frame from {topic}")
        path.write_text(topic)

    camera = Camera("/cam", tmp_path, every_s=0.01, patience_s=0.5, snap=then_nothing)
    camera._thread.join(10)  # pyright: ignore[reportPrivateUsage] -- asked again, then gave up
    assert len(calls) == 3
    with pytest.raises(RuntimeError, match="no frame from /cam"):
        camera.check()


def test_the_gimbal_camera_topic() -> None:
    assert (
        image_topic("farm", "x500_gimbal_0")
        == "/world/farm/model/x500_gimbal_0/link/camera_link/sensor/camera/image"
    )
