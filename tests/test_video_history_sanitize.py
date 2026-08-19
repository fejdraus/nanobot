"""Video payloads never reach persisted history — only their path does.

A clip is megabytes of base64. Left in the session it would ride along in
every later request; what the video showed already lives in the reply that
answered it, so the path is all that needs to survive.
"""

from types import SimpleNamespace

from nanobot.agent.loop import AgentLoop
from nanobot.utils.helpers import image_placeholder_text, video_placeholder_text


def _sanitize(content, *, truncate=False):
    """Call the sanitizer without building a whole AgentLoop.

    It only touches ``self.max_tool_result_chars``, so a stand-in carrying
    that one attribute exercises the real code path.
    """
    stub = SimpleNamespace(max_tool_result_chars=16_000)
    return AgentLoop._sanitize_persisted_blocks(
        stub, content, should_truncate_text=truncate
    )


def test_video_placeholder_carries_the_path():
    assert video_placeholder_text("/cache/clip.mp4") == "[video: /cache/clip.mp4]"


def test_video_placeholder_without_path():
    assert video_placeholder_text("") == "[video]"
    assert video_placeholder_text(None) == "[video]"


def test_image_placeholder_unchanged():
    """The image contract predates this and other code keys off it."""
    assert image_placeholder_text("/cache/shot.png") == "[image: /cache/shot.png]"
    assert image_placeholder_text(None) == "[image]"


def test_video_block_is_replaced_in_persisted_history():
    content = [
        {"type": "text", "text": "what happens here"},
        {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,AAAABBBB"},
            "_meta": {"path": "/cache/videos/clip.mp4"},
        },
    ]

    out = _sanitize(content)

    assert out[0] == {"type": "text", "text": "what happens here"}
    assert out[1] == {"type": "text", "text": "[video: /cache/videos/clip.mp4]"}
    assert "base64" not in str(out)


def test_video_without_meta_path_still_loses_the_payload():
    content = [{"type": "video_url", "video_url": {"url": "data:video/webm;base64,AAAA"}}]

    out = _sanitize(content)

    assert out[0] == {"type": "text", "text": "[video]"}


def test_image_block_still_replaced():
    content = [{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,BBBB"},
        "_meta": {"path": "/cache/shot.png"},
    }]

    out = _sanitize(content)

    assert out[0] == {"type": "text", "text": "[image: /cache/shot.png]"}


def test_remote_video_url_is_left_alone():
    """Only inline payloads are the problem — a URL costs nothing to keep."""
    block = {"type": "video_url", "video_url": {"url": "https://example.com/clip.mp4"}}

    out = _sanitize([block])

    assert out[0] == block
