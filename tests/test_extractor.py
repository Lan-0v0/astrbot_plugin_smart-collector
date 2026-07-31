import json

from smart_collector.extractor import AdaptiveExtractor
from smart_collector.models import ContentType, FetchResponse


def response(body: bytes, content_type: str, url: str = "https://example.com/api") -> FetchResponse:
    return FetchResponse(url=url, status=200, content_type=content_type, body=body)


def test_extract_nested_api_and_reuse_profile() -> None:
    extractor = AdaptiveExtractor()
    payload = {"code": 200, "data": {"title": "sample", "media": {"url": "https://cdn.test/a.jpg"}}}
    candidates, profile = extractor.extract(
        response(json.dumps(payload).encode(), "application/json"),
        (ContentType.IMAGE,),
    )
    assert [(item.content_type, item.url) for item in candidates] == [
        (ContentType.IMAGE, "https://cdn.test/a.jpg")
    ]
    assert profile["mode"] == "json"
    changed = {"code": 200, "data": {"title": "new", "media": {"url": "https://cdn.test/b.jpg"}}}
    candidates, same_profile = extractor.extract(
        response(json.dumps(changed).encode(), "application/json"),
        (ContentType.IMAGE,),
        profile,
    )
    assert candidates[0].url.endswith("b.jpg")
    assert same_profile == profile


def test_html_profile_falls_back_after_layout_change() -> None:
    extractor = AdaptiveExtractor()
    html = b"<html><head><title>A</title></head><body><img data-src='/first.webp'></body></html>"
    candidates, profile = extractor.extract(
        response(html, "text/html", "https://example.com/page"),
        (ContentType.IMAGE,),
    )
    assert candidates[0].url == "https://example.com/first.webp"
    assert profile["mode"] == "html"
    changed = b"<html><body><picture><img src='/second.png'></picture></body></html>"
    candidates, new_profile = extractor.extract(
        response(changed, "text/html", "https://example.com/page"),
        (ContentType.IMAGE,),
        profile,
    )
    assert candidates[0].url == "https://example.com/second.png"
    assert new_profile != profile


def test_direct_image_response() -> None:
    candidates, profile = AdaptiveExtractor().extract(
        response(b"png", "image/png", "https://example.com/random"),
        (ContentType.IMAGE,),
    )
    assert candidates[0].selector == "__response__"
    assert profile == {"mode": "direct", "content_type": "image"}


def test_video_ranking_detail_json_ld_and_link_discovery() -> None:
    extractor = AdaptiveExtractor()
    listing = response(
        b"<html><body><a class='block' href='/movie/42'>movie</a></body></html>",
        "text/html",
        "https://twitter-ero-video-ranking.com/",
    )
    assert extractor.extract_links(listing) == ["https://twitter-ero-video-ranking.com/movie/42"]
    detail = response(
        b"""<html><script type="application/ld+json">{"@type":"VideoObject","contentUrl":"https://cdn.example/video?id=42"}</script></html>""",
        "text/html",
        "https://twitter-ero-video-ranking.com/movie/42",
    )
    candidates, profile = extractor.extract(detail, (ContentType.VIDEO,))
    assert candidates[0].content_type is ContentType.VIDEO
    assert candidates[0].url == "https://cdn.example/video?id=42"
    assert profile["mode"] == "html"
