"""Face scale guard for scene templates."""

from services.comfyui.scene_quality import _oversized_face


def test_rejects_the_giant_face_from_the_reported_cafe_failure():
    source = [579, 333, 746, 553]
    failed = [461, 285, 817, 785]
    repaired = [583, 333, 741, 547]
    assert _oversized_face(source, failed)
    assert not _oversized_face(source, repaired)


def test_guard_skips_images_without_measurable_faces():
    assert not _oversized_face(None, [100, 100, 600, 600])
    assert not _oversized_face([0, 0, 100, 100], None)
    assert not _oversized_face([0, 0, 0, 0], [0, 0, 200, 200])
