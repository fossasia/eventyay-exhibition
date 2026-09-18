from exhibition.utils import build_exhibitor_video_embed


def test_build_exhibitor_video_embed_handles_youtu_be():
    assert build_exhibitor_video_embed("https://youtu.be/abc123") == {
        "type": "iframe",
        "url": "https://www.youtube.com/embed/abc123",
    }


def test_build_exhibitor_video_embed_handles_youtube_watch():
    assert build_exhibitor_video_embed("https://www.youtube.com/watch?v=abc123") == {
        "type": "iframe",
        "url": "https://www.youtube.com/embed/abc123",
    }


def test_build_exhibitor_video_embed_handles_youtube_embed():
    assert build_exhibitor_video_embed("https://www.youtube.com/embed/abc123") == {
        "type": "iframe",
        "url": "https://www.youtube.com/embed/abc123",
    }


def test_build_exhibitor_video_embed_handles_youtube_shorts():
    assert build_exhibitor_video_embed("https://www.youtube.com/shorts/abc123") == {
        "type": "iframe",
        "url": "https://www.youtube.com/embed/abc123",
    }


def test_build_exhibitor_video_embed_handles_youtube_live():
    assert build_exhibitor_video_embed("https://www.youtube.com/live/abc123") == {
        "type": "iframe",
        "url": "https://www.youtube.com/embed/abc123",
    }


def test_build_exhibitor_video_embed_handles_vimeo_watch_url():
    assert build_exhibitor_video_embed("https://vimeo.com/123456") == {
        "type": "iframe",
        "url": "https://player.vimeo.com/video/123456",
    }


def test_build_exhibitor_video_embed_handles_vimeo_embed_url():
    assert build_exhibitor_video_embed("https://player.vimeo.com/video/123456") == {
        "type": "iframe",
        "url": "https://player.vimeo.com/video/123456",
    }


def test_build_exhibitor_video_embed_handles_direct_video_file():
    assert build_exhibitor_video_embed("https://example.com/video.mp4") == {
        "type": "video",
        "url": "https://example.com/video.mp4",
    }


def test_build_exhibitor_video_embed_handles_generic_embed_url():
    assert build_exhibitor_video_embed("https://example.com/embed/demo") == {
        "type": "iframe",
        "url": "https://example.com/embed/demo",
    }


def test_build_exhibitor_video_embed_returns_none_for_invalid_url():
    assert build_exhibitor_video_embed("https://example.com/video-page") is None


def test_build_exhibitor_video_embed_returns_none_for_empty_value():
    assert build_exhibitor_video_embed("") is None
