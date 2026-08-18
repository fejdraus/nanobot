"""Video reaches Anthropic-shaped providers as a ``video`` block.

MiniMax-M3 is the reason this exists: its native surface is the
Anthropic-compatible endpoint, and there video is
``{"type": "video", "source": {...}}`` rather than the OpenAI ``video_url``.
Verified live against ``api.minimax.io/anthropic/v1/messages``.
"""

from nanobot.providers.anthropic_provider import AnthropicProvider


def test_base64_video_becomes_anthropic_source():
    block = AnthropicProvider._convert_video_block({
        "type": "video_url",
        "video_url": {"url": "data:video/mp4;base64,AAAA"},
    })

    assert block == {
        "type": "video",
        "source": {"type": "base64", "media_type": "video/mp4", "data": "AAAA"},
    }


def test_plain_url_video_is_passed_through():
    block = AnthropicProvider._convert_video_block({
        "type": "video_url",
        "video_url": {"url": "https://example.com/clip.mp4"},
    })

    assert block == {
        "type": "video",
        "source": {"type": "url", "url": "https://example.com/clip.mp4"},
    }


def test_fps_rides_along_when_set():
    block = AnthropicProvider._convert_video_block({
        "type": "video_url",
        "video_url": {"url": "data:video/mp4;base64,AAAA", "fps": 2},
    })

    assert block["fps"] == 2


def test_fps_absent_leaves_provider_default():
    block = AnthropicProvider._convert_video_block({
        "type": "video_url",
        "video_url": {"url": "data:video/mp4;base64,AAAA"},
    })

    assert "fps" not in block


def test_empty_url_is_dropped_not_sent_broken():
    assert AnthropicProvider._convert_video_block({"video_url": {"url": ""}}) is None


def test_user_content_converts_video_alongside_image_and_text():
    content = AnthropicProvider._convert_user_content([
        {"type": "text", "text": "what happens here"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}},
    ])

    assert [b["type"] for b in content] == ["text", "image", "video"]


def test_webm_media_type_is_preserved():
    """The container matters: mp4 is not the only thing Telegram delivers."""
    block = AnthropicProvider._convert_video_block({
        "type": "video_url",
        "video_url": {"url": "data:video/webm;base64,AAAA"},
    })

    assert block["source"]["media_type"] == "video/webm"
