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


def test_html_profile_ignores_malformed_saved_rules() -> None:
    extractor = AdaptiveExtractor()
    html = b"<html><body><img src='/valid.png'></body></html>"
    candidates, _ = extractor.extract(
        response(html, "text/html", "https://example.com/page"),
        (ContentType.IMAGE,),
        {
            "mode": "html",
            "selectors": [None, {"selector": None}, {"selector": "[broken"}],
            "json_ld_paths": [None, {"content_type": "unknown", "path": []}],
        },
    )
    assert candidates[0].url == "https://example.com/valid.png"


def test_non_mapping_and_malformed_json_profiles_are_ignored() -> None:
    extractor = AdaptiveExtractor()
    payload = {"data": {"url": "https://cdn.test/new.jpg"}}
    candidates, _ = extractor.extract(
        response(json.dumps(payload).encode(), "application/json"),
        (ContentType.IMAGE,),
        ["invalid profile"],  # type: ignore[arg-type]
    )
    assert candidates[0].url == "https://cdn.test/new.jpg"
    candidates, _ = extractor.extract(
        response(json.dumps(payload).encode(), "application/json"),
        (ContentType.IMAGE,),
        {"mode": "json", "json_paths": [None, {"path": ["missing"]}]},
    )
    assert candidates[0].url == "https://cdn.test/new.jpg"


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


def test_video_manifest_and_m4v_formats_are_recognized() -> None:
    extractor = AdaptiveExtractor()
    assert extractor._url_type("https://cdn.example/manifest.mpd") is ContentType.VIDEO
    assert extractor._url_type("https://cdn.example/movie.m4v") is ContentType.VIDEO
    assert extractor._mime_type("application/vnd.apple.mpegurl") is ContentType.VIDEO
    assert extractor._mime_type("application/dash+xml; charset=utf-8") is ContentType.VIDEO


def test_html_and_json_video_candidates_keep_the_source_referer() -> None:
    extractor = AdaptiveExtractor()
    page_url = "https://source.example/watch/1"
    html_candidates, _ = extractor.extract(
        response(
            b"<video src='https://cdn.example/movie.mp4'></video>",
            "text/html",
            page_url,
        ),
        (ContentType.VIDEO,),
    )
    assert html_candidates[0].referer == page_url
    json_candidates, _ = extractor.extract(
        response(
            json.dumps({"video": "https://cdn.example/movie.mp4"}).encode(),
            "application/json",
            page_url,
        ),
        (ContentType.VIDEO,),
    )
    assert json_candidates[0].referer == page_url


def test_json_video_keys_support_relative_and_extensionless_urls() -> None:
    extractor = AdaptiveExtractor()
    payload = {
        "data": {
            "video_url": "/delivery?id=123",
            "play_url": "https://cdn.example/signed?id=456",
            "manifest": "../streams/main.mpd",
        }
    }
    candidates, profile = extractor.extract(
        response(
            json.dumps(payload).encode(),
            "application/json",
            "https://api.example/v1/items/1",
        ),
        (ContentType.VIDEO,),
    )
    assert [candidate.url for candidate in candidates] == [
        "https://api.example/delivery?id=123",
        "https://cdn.example/signed?id=456",
        "https://api.example/v1/streams/main.mpd",
    ]
    assert all(candidate.content_type is ContentType.VIDEO for candidate in candidates)

    changed = {"data": {"video_url": "/delivery?id=789"}}
    reused, _ = extractor.extract(
        response(
            json.dumps(changed).encode(),
            "application/json",
            "https://api.example/v1/items/1",
        ),
        (ContentType.VIDEO,),
        profile,
    )
    assert reused[0].url == "https://api.example/delivery?id=789"


def test_lazy_embed_and_video_attributes_are_discovered() -> None:
    extractor = AdaptiveExtractor()
    page = response(
        b"""<iframe data-lazy-src='/embed/1'></iframe>
        <video data-video='/delivery?id=1'></video>
        <source data-src='/movie.m4v'>""",
        "text/html",
        "https://example.com/watch",
    )
    assert extractor.extract_embed_links(page) == ["https://example.com/embed/1"]
    candidates, _ = extractor.extract(page, (ContentType.VIDEO,))
    assert {candidate.url for candidate in candidates} == {
        "https://example.com/delivery?id=1",
        "https://example.com/movie.m4v",
    }


def test_image_metadata_prefers_largest_srcset_and_keeps_content_context() -> None:
    html = b"""<html><head><title>Gallery</title></head><body>
    <header><img id='site-logo' src='/logo.png' width='64' height='64'></header>
    <main><figure><img alt='blue cat wallpaper' width='800' height='600'
      src='/fallback.jpg' srcset='/large.jpg 1600w, /small.jpg 320w'>
      <figcaption>Blue cat in the night</figcaption></figure></main>
    </body></html>"""
    candidates, _ = AdaptiveExtractor().extract(
        response(html, "text/html", "https://example.com/gallery"),
        (ContentType.IMAGE,),
    )
    large = next(item for item in candidates if item.url.endswith("/large.jpg"))
    logo = next(item for item in candidates if item.url.endswith("/logo.png"))
    assert large.width == 1600
    assert large.source_kind == "srcset"
    assert large.in_main_content
    assert "blue cat wallpaper" in large.context_text
    assert "Blue cat in the night" in large.context_text
    assert not logo.in_main_content


def test_image_json_keys_allow_extensionless_original_urls() -> None:
    candidates, _ = AdaptiveExtractor().extract(
        response(
            json.dumps(
                {
                    "image_original": "https://cdn.example/delivery?id=1",
                    "thumbnail": "/thumb?id=1",
                }
            ).encode(),
            "application/json",
            "https://api.example/item/1",
        ),
        (ContentType.IMAGE,),
    )
    assert [item.url for item in candidates] == [
        "https://cdn.example/delivery?id=1",
        "https://api.example/thumb?id=1",
    ]
    assert all(item.source_kind == "json" for item in candidates)
