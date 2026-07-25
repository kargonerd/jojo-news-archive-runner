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
    next_discovery_query,
    parse_cdx_json,
    process_wsj_bluesky_page,
    record_discovery_page,
    wsj_catalog_count_for_year,
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
    assert discovery_summary(connection)["shouldContinue"] is True
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    item = manifest_item_from_row(row, publisher="bloomberg")
    assert item.canonical_url == original
    assert len(item.candidates) == 3
    assert item.candidates[0].byte_count is not None


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
