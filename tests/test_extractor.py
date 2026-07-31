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


def test_avbebe_detail_embed_and_packed_player_discovery() -> None:
    extractor = AdaptiveExtractor()
    listing = response(
        b"<html><body><a href='/archives/42'>video</a></body></html>",
        "text/html",
        "https://avbebe.com/archives/category/video",
    )
    assert extractor.extract_links(listing) == ["https://avbebe.com/archives/42"]
    detail = response(
        b"""<html><iframe src="https://ads.example/widgets/banner"></iframe>
        <iframe src="https://player.example/e/42"></iframe></html>""",
        "text/html",
        "https://avbebe.com/archives/42",
    )
    assert extractor.extract_embed_links(detail) == [
        "https://player.example/e/42",
        "https://ads.example/widgets/banner",
    ]
    packed = rb"""<html><script>eval(function(p,a,c,k,e,d){e=function(c){return
        c.toString(a)};if(!''.replace(/^/,String)){while(c--){d[c.toString(a)]=k[c]||
        c.toString(a)}k=[function(e){return d[e]}];e=function(){return'\w+'};c=1};
        while(c--){if(k[c]){p=p.replace(new RegExp('\b'+e(c)+'\b','g'),k[c])}}
        return p}('0="1";',2,2,'src|https://cdn.example/video.m3u8'.split('|'),0,{}))
        </script></html>"""
    candidates, profile = extractor.extract(
        response(packed, "text/html", "https://player.example/e/42"),
        (ContentType.VIDEO,),
    )
    assert candidates[0].content_type is ContentType.VIDEO
    assert candidates[0].url == "https://cdn.example/video.m3u8"
    assert candidates[0].referer == "https://player.example/e/42"
    assert profile["mode"] == "html"


def test_nextjs_media_urls_are_unescaped_and_other_types_are_preserved() -> None:
    html = rb"""<html><script>self.__next_f.push(["https:\/\/cdn.example\/movie.mp4?tag=21\\"])</script>
    <img src="/cover.webp"><audio src="/sound.mp3"></audio></html>"""
    candidates, _ = AdaptiveExtractor().extract(
        response(html, "text/html", "https://example.com/all"),
        (ContentType.VIDEO, ContentType.IMAGE, ContentType.AUDIO),
    )
    assert [(item.content_type, item.url) for item in candidates] == [
        (ContentType.VIDEO, "https://cdn.example/movie.mp4?tag=21"),
        (ContentType.AUDIO, "https://example.com/sound.mp3"),
        (ContentType.IMAGE, "https://example.com/cover.webp"),
    ]


def test_random_page_url_preserves_filters_and_uses_discovered_last_page() -> None:
    class FixedRandom:
        @staticmethod
        def randint(start: int, end: int) -> int:
            assert (start, end) == (1, 7262)
            return 4321

    listing = response(
        b"""<a href='/zh-CN/all?sort=favorite&amp;page=2'>2</a>
        <a href='/zh-CN/all?sort=favorite&amp;page=7262'>last</a>""",
        "text/html",
        "https://pektino.com/zh-CN/all",
    )
    assert AdaptiveExtractor.random_page_url(listing, FixedRandom()) == (
        "https://pektino.com/zh-CN/all?sort=favorite&page=4321"
    )


def test_video_resolution_is_inferred_from_common_url_patterns() -> None:
    extractor = AdaptiveExtractor()
    assert extractor._url_resolution("https://cdn.example/vid/1280x720/movie.mp4") == (
        1280,
        720,
    )
    assert extractor._url_resolution("https://cdn.example/movie-1080p.mp4") == (0, 1080)
    assert extractor._url_resolution("https://cdn.example/movie.mp4") == (0, 0)
