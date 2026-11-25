"""Video attachments reach the model only when the agent is told they can.

The ``video_url`` block is not part of the common OpenAI-compatible surface: a
provider that does not know it rejects the whole request, so a video must stay a
plain path reference unless ``videoInput`` is on.
"""

import base64

from nanobot.agent.context import ContextBuilder
from nanobot.utils.document import reference_non_image_attachments


def _write_video(tmp_path, name="clip.mp4", size=32):
    path = tmp_path / name
    path.write_bytes(b"\x00" * size)
    return str(path)


def test_video_is_a_path_reference_by_default(tmp_path):
    video = _write_video(tmp_path)
    content, media = reference_non_image_attachments("look", [video])

    assert media == []
    assert f"[Attachment: {video}]" in content


def test_video_is_kept_as_media_when_allowed(tmp_path):
    video = _write_video(tmp_path)
    content, media = reference_non_image_attachments("look", [video], allow_video=True)

    assert media == [video]
    assert "Attachment" not in content


def test_build_user_content_emits_video_url_block(tmp_path):
    video = _write_video(tmp_path, size=64)
    builder = ContextBuilder(workspace=tmp_path)

    content = builder.build_user_content("what happens here", [video], video_fps=2.0)

    assert isinstance(content, list)
    block = content[0]
    assert block["type"] == "video_url"
    assert block["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert block["video_url"]["fps"] == 2.0
    payload = block["video_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(payload) == b"\x00" * 64
    assert content[-1] == {"type": "text", "text": "what happens here"}


def test_video_fps_is_omitted_when_not_configured(tmp_path):
    video = _write_video(tmp_path)
    builder = ContextBuilder(workspace=tmp_path)

    content = builder.build_user_content("hi", [video])

    assert isinstance(content, list)
    assert "fps" not in content[0]["video_url"]


def test_oversized_video_is_skipped_rather_than_sent(tmp_path, monkeypatch):
    """A body over the provider cap cannot succeed — do not spend the upload."""
    monkeypatch.setattr("nanobot.agent.context._MAX_INLINE_VIDEO_BYTES", 16)
    video = _write_video(tmp_path, size=64)
    builder = ContextBuilder(workspace=tmp_path)

    content = builder.build_user_content("too big", [video])

    assert content == "too big"


def test_images_still_work_alongside_video(tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    )
    video = _write_video(tmp_path)
    builder = ContextBuilder(workspace=tmp_path)

    content = builder.build_user_content("both", [str(png), video])

    assert isinstance(content, list)
    kinds = [b["type"] for b in content]
    assert kinds == ["image_url", "video_url", "text"]
