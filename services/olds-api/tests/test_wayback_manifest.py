from __future__ import annotations

import gzip
import json
from pathlib import Path
import sqlite3

from jojo_olds_api.archive_sources import (
    archive_source_spec,
    normalize_article_url,
)
from jojo_olds_api.raw_archive_capture import manifest_item_from_row
from jojo_olds_api.wayback_manifest import (
    CDXCapture,
    CDXPage,
    candidate_rank,
    discovery_summary,
    export_capture_manifest,
    infer_published_at,
    initialize_discovery_schema,
    initialize_wsj_bluesky_schema,
    initialize_wsj_google_news_schema,
    initialize_wsj_rss_schema,
    next_discovery_query,
    parse_cdx_json,
    process_wsj_bluesky_page,
    process_wsj_google_news_feed,
    process_wsj_rss_feeds,
    record_discovery_page,
    wsj_catalog_count_for_year,
    wsj_catalog_ready_for_capture,
    wsj_google_news_is_only_catalog_gap,
    wsj_google_news_should_continue,
)


def test_parse_cdx_json_extracts_resume_key():
    payload = json.dumps(
        [
            [
                "timestamp",
                "original",
                "mimetype",
                "statuscode",
                "digest",
                "length",
            ],
            [
                "20200102125527",
                "https://www.bloomberg.com/news/articles/2020-01-01/example",
                "text/html",
                "200",
                "DIGEST",
                "35034",
            ],
            [],
            ["opaque-resume-key"],
        ]
    )

    page = parse_cdx_json(payload)

    assert len(page.captures) == 1
    assert page.captures[0].length == 35_034
    assert page.resume_key == "opaque-resume-key"


def test_source_url_normalization_accepts_articles_and_rejects_hubs():
    ap = archive_source_spec("ap")
    assert normalize_article_url(
        ap,
        "http://www.apnews.com/article/example?utm_source=test",
    ) == "https://apnews.com/article/example"
    assert normalize_article_url(ap, "https://apnews.com/hub/world-news") is None

    wsj = archive_source_spec("wsj")
    assert normalize_article_url(
        wsj,
        (
            "https://www.wsj.com/politics/"
            "modern-section-article-a1b2c3d4?mod=social"
        ),
    ) == (
        "https://www.wsj.com/politics/"
        "modern-section-article-a1b2c3d4"
    )
    assert normalize_article_url(
        wsj,
        "https://www.wsj.com/politics",
    ) is None

    ft = archive_source_spec("ft")
    assert normalize_article_url(
        ft,
        "https://ft.com/content/12345678-1234-1234-1234-123456789abc?share=1",
    ) == (
        "https://www.ft.com/content/"
        "12345678-1234-1234-1234-123456789abc"
    )


def test_date_inference_and_candidate_ranking_prefers_after_publication():
    published = infer_published_at(
        "https://www.nytimes.com/2020/01/02/world/example.html"
    )
    assert published == "2020-01-02T00:00:00+00:00"
    after = candidate_rank("20200102010000", published_at=published)
    before = candidate_rank("20200101010000", published_at=published)
    assert after < before


def test_discovery_keeps_three_best_candidates_and_exports_generic_manifest(
    tmp_path: Path,
):
    spec = archive_source_spec("bloomberg")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2020,
        to_year=2020,
    )
    pattern, _ = next_discovery_query(connection)
    original = (
        "https://www.bloomberg.com/news/articles/2020-01-01/example"
    )
    captures = tuple(
        CDXCapture(
            timestamp=f"2020010{day}120000",
            original=original,
            mimetype="text/html",
            status_code=200,
            digest=f"DIGEST-{day}",
            length=30_000 + day,
        )
        for day in range(1, 6)
    )

    page_result = record_discovery_page(
        connection,
        spec=spec,
        pattern=pattern,
        page=CDXPage(captures=captures, resume_key=None),
    )
    destination = tmp_path / "manifest.jsonl.gz"
    summary = export_capture_manifest(
        connection,
        spec=spec,
        destination=destination,
        from_year=2020,
        to_year=2020,
    )

    assert page_result["seen"] == 5
    assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 3
    assert summary["articles"] == 1
    assert summary["candidates"] == 3
    assert summary["captureReady"] is False
    assert summary["yearCounts"] == {"2020": 1}
    assert discovery_summary(connection)["shouldContinue"] is True
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    item = manifest_item_from_row(row, publisher="bloomberg")
    assert item.canonical_url == original
    assert len(item.candidates) == 3
    assert item.candidates[0].byte_count is not None

    ready_summary = export_capture_manifest(
        connection,
        spec=spec,
        destination=destination,
        from_year=2020,
        to_year=2020,
        capture_minimum_per_year=1,
    )
    assert ready_summary["captureReady"] is True


def test_discovery_queries_follow_configured_order_not_lexical_order():
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2020,
        to_year=2020,
    )

    pattern, _ = next_discovery_query(connection)

    assert pattern == "www.wsj.com/articles/a*"


def test_no_date_url_uses_capture_time_for_year_stratification():
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2020,
        to_year=2020,
        collapse="urlkey",
    )
    pattern, _ = next_discovery_query(connection)
    original = "https://www.wsj.com/articles/example-slug"
    record_discovery_page(
        connection,
        spec=spec,
        pattern=pattern,
        page=CDXPage(
            captures=(
                CDXCapture(
                    timestamp="20200615120000",
                    original=original,
                    mimetype="text/html",
                    status_code=200,
                    digest="DIGEST",
                    length=50_000,
                ),
            ),
            resume_key=None,
        ),
    )

    published_at = connection.execute(
        "SELECT published_at FROM candidates WHERE canonical_url=?",
        (original,),
    ).fetchone()[0]

    assert published_at == "2020-06-15T12:00:00+00:00"


def test_urlkey_discovery_round_robins_patterns():
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2020,
        to_year=2020,
        collapse="urlkey",
    )
    first_pattern, _ = next_discovery_query(connection)
    connection.execute(
        """
        UPDATE discovery_queries
        SET status='running', pages=5, resume_key='resume'
        WHERE pattern=?
        """,
        (first_pattern,),
    )

    next_pattern, _ = next_discovery_query(connection)

    assert next_pattern != first_pattern
    assert next_pattern == "www.wsj.com/articles/b*"


class StubBlueskyResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "cursor": "2025-01-01T00:00:00.000Z",
            "feed": [
                {
                    "post": {
                        "uri": "at://did:plc:wsj/post/one",
                        "record": {
                            "createdAt": "2025-01-02T03:04:05.000Z",
                        },
                        "embed": {
                            "external": {
                                "uri": (
                                    "https://www.wsj.com/politics/"
                                    "modern-story-a1b2c3d4?mod=social"
                                )
                            }
                        },
                    }
                },
                {
                    "post": {
                        "uri": "at://did:plc:wsj/post/two",
                        "record": {
                            "createdAt": "2025-01-01T03:04:05.000Z",
                        },
                        "embed": {
                            "external": {
                                "uri": "https://www.wsj.com/politics"
                            }
                        },
                    }
                },
            ],
        }


class StubBlueskyClient:
    def get(self, url, params):
        assert url.endswith("app.bsky.feed.getAuthorFeed")
        assert params["actor"] == "wsj.com"
        assert params["filter"] == "posts_with_links"
        return StubBlueskyResponse()


def test_wsj_bluesky_discovers_modern_section_urls(tmp_path: Path):
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2024,
        to_year=2026,
        collapse="urlkey",
    )
    initialize_wsj_bluesky_schema(connection)

    result = process_wsj_bluesky_page(
        connection,
        spec=spec,
        http_client=StubBlueskyClient(),
        from_year=2024,
        to_year=2026,
    )
    destination = tmp_path / "wsj-manifest.jsonl.gz"
    summary = export_capture_manifest(
        connection,
        spec=spec,
        destination=destination,
        from_year=2024,
        to_year=2026,
    )

    assert result["seen"] == 2
    assert result["accepted"] == 1
    assert result["hasMore"] is True
    assert wsj_catalog_count_for_year(connection, 2025) == 1
    assert summary["articles"] == 1
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    assert row["canonicalUrl"].endswith("/modern-story-a1b2c3d4")
    assert row["publishedAt"] == "2025-01-02T03:04:05+00:00"
    assert len(row["candidates"]) == 3
    assert all(
        candidate["provider"] == "wayback"
        for candidate in row["candidates"]
    )


class StubRSSResponse:
    content = b"""
    <rss version="2.0">
      <channel>
        <item>
          <link>https://www.wsj.com/finance/stocks/modern-rss-story-a1b2c3d4?mod=rss</link>
          <pubDate>Sat, 25 Jul 2026 12:34:56 GMT</pubDate>
        </item>
        <item>
          <link>https://www.wsj.com/podcasts/example</link>
          <pubDate>Sat, 25 Jul 2026 12:34:56 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    def raise_for_status(self):
        return None


class StubRSSClient:
    def get(self, url):
        assert url == "https://feeds.example/wsj"
        return StubRSSResponse()


def test_wsj_official_rss_discovers_current_section_urls(
    tmp_path: Path,
):
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2024,
        to_year=2026,
        collapse="urlkey",
    )
    initialize_wsj_rss_schema(connection)

    result = process_wsj_rss_feeds(
        connection,
        spec=spec,
        http_client=StubRSSClient(),
        from_year=2024,
        to_year=2026,
        feed_urls=("https://feeds.example/wsj",),
    )
    destination = tmp_path / "wsj-rss-manifest.jsonl.gz"
    summary = export_capture_manifest(
        connection,
        spec=spec,
        destination=destination,
        from_year=2024,
        to_year=2026,
    )

    assert result["feedsChecked"] == 1
    assert result["itemsSeen"] == 2
    assert result["accepted"] == 1
    assert result["errors"] == []
    assert wsj_catalog_count_for_year(connection, 2026) == 1
    assert summary["articles"] == 1
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    assert row["canonicalUrl"].endswith(
        "/modern-rss-story-a1b2c3d4"
    )
    assert row["publishedAt"] == "2026-07-25T12:34:56+00:00"
    assert len(row["candidates"]) == 4
    assert row["candidates"][-1]["provider"] == "live-origin"


class StubGoogleNewsResponse:
    def __init__(self, value: str):
        self.text = value
        self.content = value.encode()

    def raise_for_status(self):
        return None


class StubGoogleNewsClient:
    def __init__(self):
        self.queries: list[str] = []

    def get(self, url, params=None):
        if url.endswith("/rss/search"):
            assert params["q"].startswith("site:wsj.com/articles")
            self.queries.append(params["q"])
            return StubGoogleNewsResponse(
                """
                <rss version="2.0">
                  <channel>
                    <item>
                      <link>https://news.google.com/rss/articles/ENCODED-ID</link>
                      <pubDate>Sat, 21 Sep 2024 07:00:00 GMT</pubDate>
                    </item>
                  </channel>
                </rss>
                """
            )
        assert url.endswith("/rss/articles/ENCODED-ID")
        return StubGoogleNewsResponse(
            '<c-wiz><div data-n-a-sg="SIGNATURE" '
            'data-n-a-ts="1726902000"></div></c-wiz>'
        )

    def post(self, url, data, headers):
        assert url.endswith("/data/batchexecute")
        assert "ENCODED-ID" in data["f.req"]
        assert headers["Origin"] == "https://news.google.com"
        inner = json.dumps(
            [
                "garturlres",
                (
                    "https://www.wsj.com/articles/"
                    "google-news-story-a1b2c3d4"
                ),
            ]
        )
        return StubGoogleNewsResponse(
            ")]}'\n\n"
            + json.dumps([["wrb.fr", "Fbv4je", inner]])
        )


def test_wsj_google_news_fills_historical_catalog_gap(
    tmp_path: Path,
):
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2024,
        to_year=2026,
        collapse="urlkey",
    )
    initialize_wsj_google_news_schema(connection)

    client = StubGoogleNewsClient()
    result = process_wsj_google_news_feed(
        connection,
        spec=spec,
        http_client=client,
        from_year=2024,
        to_year=2026,
        maximum_decodes=1,
        minimum_catalog=1,
    )
    destination = tmp_path / "wsj-google-news-manifest.jsonl.gz"
    summary = export_capture_manifest(
        connection,
        spec=spec,
        destination=destination,
        from_year=2024,
        to_year=2026,
    )

    assert result == {
        "status": "complete-target-met",
        "targetYear": 2024,
        "itemsSeen": 1,
        "decodesAttempted": 1,
        "accepted": 1,
        "catalogCount": 1,
        "errors": [],
    }
    assert wsj_catalog_count_for_year(connection, 2024) == 1
    assert client.queries == [
        "site:wsj.com/articles after:2024-01-01 before:2024-02-01"
    ]
    assert wsj_google_news_should_continue(
        connection,
        from_year=2024,
        to_year=2026,
    ) is True
    assert summary["articles"] == 1
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    assert row["canonicalUrl"].endswith(
        "/google-news-story-a1b2c3d4"
    )
    assert row["publishedAt"] == "2024-09-21T07:00:00+00:00"


def test_wsj_google_news_keeps_filling_2023_after_2024_is_ready():
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2023,
        to_year=2024,
        collapse="urlkey",
    )
    initialize_wsj_google_news_schema(connection)
    rows = [
        (
            f"https://www.wsj.com/articles/ready-2024-{index}",
            "2024-06-01T00:00:00+00:00",
            f"https://news.google.com/rss/articles/2024-{index}",
            "2026-01-01T00:00:00+00:00",
        )
        for index in range(2)
    ]
    rows.append(
        (
            "https://www.wsj.com/articles/one-2023",
            "2023-06-01T00:00:00+00:00",
            "https://news.google.com/rss/articles/2023-1",
            "2026-01-01T00:00:00+00:00",
        )
    )
    connection.executemany(
        """
        INSERT INTO wsj_google_news_articles(
            canonical_url,
            published_at,
            google_news_url,
            updated_at
        ) VALUES (?, ?, ?, ?)
        """,
        rows,
    )

    assert wsj_google_news_should_continue(
        connection,
        from_year=2023,
        to_year=2024,
        minimum_catalog=2,
    ) is True
    connection.execute(
        """
        INSERT INTO wsj_google_news_articles(
            canonical_url,
            published_at,
            google_news_url,
            updated_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            "https://www.wsj.com/articles/two-2023",
            "2023-07-01T00:00:00+00:00",
            "https://news.google.com/rss/articles/2023-2",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    assert wsj_google_news_should_continue(
        connection,
        from_year=2023,
        to_year=2024,
        minimum_catalog=2,
    ) is False


def test_wsj_google_news_can_pause_cdx_when_only_supported_years_are_short():
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2022,
        to_year=2024,
        collapse="urlkey",
    )
    initialize_wsj_google_news_schema(connection)

    assert wsj_google_news_is_only_catalog_gap(
        connection,
        from_year=2022,
        to_year=2024,
        minimum_catalog=1,
    ) is False

    record_discovery_page(
        connection,
        spec=spec,
        pattern=next_discovery_query(connection)[0],
        page=CDXPage(
            captures=(
                CDXCapture(
                    timestamp="20220601000000",
                    original=(
                        "https://www.wsj.com/articles/"
                        "ready-2022-a1b2c3d4"
                    ),
                    mimetype="text/html",
                    status_code=200,
                    digest="READY-2022",
                    length=12_345,
                ),
            ),
            resume_key=None,
        ),
    )

    assert wsj_google_news_is_only_catalog_gap(
        connection,
        from_year=2022,
        to_year=2024,
        minimum_catalog=1,
    ) is True
    assert wsj_catalog_ready_for_capture(
        connection,
        from_year=2022,
        to_year=2024,
        minimum_catalog=1,
    ) is False

    connection.executemany(
        """
        INSERT INTO wsj_google_news_articles(
            canonical_url,
            published_at,
            google_news_url,
            updated_at
        ) VALUES (?, ?, ?, ?)
        """,
        [
            (
                f"https://www.wsj.com/articles/ready-{year}-a1b2c3d4",
                f"{year}-06-01T00:00:00+00:00",
                f"https://news.google.com/rss/articles/{year}",
                "2026-01-01T00:00:00+00:00",
            )
            for year in (2023, 2024)
        ],
    )
    assert wsj_catalog_ready_for_capture(
        connection,
        from_year=2022,
        to_year=2024,
        minimum_catalog=1,
    ) is True


def test_digest_discovery_keeps_exhausting_current_pattern():
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2020,
        to_year=2020,
        collapse="digest",
    )
    first_pattern, _ = next_discovery_query(connection)
    connection.execute(
        """
        UPDATE discovery_queries
        SET status='running', pages=5, resume_key='resume'
        WHERE pattern=?
        """,
        (first_pattern,),
    )

    next_pattern, _ = next_discovery_query(connection)

    assert next_pattern == first_pattern
