from __future__ import annotations

from datetime import date
import gzip
import json
from pathlib import Path
import sqlite3

from jojo_olds_api.raw_archive_capture import manifest_item_from_row
from jojo_olds_api.reuters_sitemap_manifest import (
    discover_reuters_sitemap_captures,
    export_reuters_manifest,
    initialize_reuters_sitemap_schema,
    initialize_reuters_urlscan_queries,
    pending_reuters_sitemaps,
    pending_reuters_urlscan_queries,
    process_reuters_sitemap,
    process_reuters_urlscan_query,
)


CDX_RESPONSE = json.dumps(
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
            "20230101031000",
            (
                "https://www.reuters.com/arc/outboundfeeds/sitemap/"
                "?outputType=xml&amp;from=100"
            ),
            "application/xml",
            "200",
            "DIGEST",
            "12345",
        ],
    ]
)

SITEMAP_XML = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.reuters.com/world/example-story-2022-12-31/</loc>
    <lastmod>2022-12-31T10:00:00Z</lastmod>
  </url>
  <url>
    <loc>https://www.reuters.com/graphics/example/</loc>
    <lastmod>2022-12-31T10:00:00Z</lastmod>
  </url>
</urlset>
"""


class StubResponse:
    status_code = 200
    text = CDX_RESPONSE

    def raise_for_status(self):
        return None


class StubHTTPClient:
    def get(self, url, params):
        assert any(
            key == "filter" and value.startswith("original:")
            for key, value in params
        )
        return StubResponse()


class StubArchiveClient:
    def fetch(self, url, *, maximum_bytes):
        assert "&from=100" in url
        assert len(SITEMAP_XML) < maximum_bytes
        return 200, {"content-type": "application/xml"}, SITEMAP_XML, url


class StubUrlscanResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "results": [
                {
                    "page": {
                        "url": (
                            "https://www.reuters.com/world/"
                            "urlscan-discovered-story-2024-01-03/"
                        )
                    },
                    "task": {"url": "https://example.com/redirect"},
                },
                {
                    "page": {"url": "https://www.reuters.com/"},
                    "task": {"url": "https://www.reuters.com/search/"},
                },
            ]
        }


class StubUrlscanClient:
    def get(self, url, params):
        assert url.endswith("/api/v1/search/")
        assert params["size"] == "100"
        assert "date:[2024-01-01 TO 2024-01-08]" in params["q"]
        return StubUrlscanResponse()


def test_discovers_html_escaped_reuters_sitemap_urls():
    captures = discover_reuters_sitemap_captures(
        from_year=2021,
        to_year=2026,
        client=StubHTTPClient(),
    )

    assert captures == [
        {
            "timestamp": "20230101031000",
            "originalUrl": (
                "https://www.reuters.com/arc/outboundfeeds/sitemap/"
                "?outputType=xml&from=100"
            ),
            "digest": "DIGEST",
            "byteCount": 12345,
        }
    ]


def test_reuters_sitemap_capture_builds_article_manifest(tmp_path: Path):
    captures = discover_reuters_sitemap_captures(
        from_year=2021,
        to_year=2026,
        client=StubHTTPClient(),
    )
    connection = sqlite3.connect(":memory:")
    initialize_reuters_sitemap_schema(
        connection,
        from_year=2021,
        to_year=2026,
        captures=captures,
    )
    pending = pending_reuters_sitemaps(
        connection,
        maximum=10,
        maximum_attempts=3,
    )
    assert len(pending) == 1

    result = process_reuters_sitemap(
        connection,
        snapshot_url=pending[0][0],
        archive_client=StubArchiveClient(),
        from_year=2021,
        to_year=2026,
    )
    destination = tmp_path / "manifest.jsonl.gz"
    summary = export_reuters_manifest(
        connection,
        destination=destination,
        from_year=2021,
        to_year=2026,
        maximum_attempts=3,
    )

    assert result == {"status": "complete", "seen": 2, "accepted": 1}
    assert summary["complete"] is True
    assert summary["articles"] == 1
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    item = manifest_item_from_row(row, publisher="reuters")
    assert item.canonical_url == (
        "https://www.reuters.com/world/example-story-2022-12-31"
    )
    assert item.published_at == "2022-12-31T00:00:00+00:00"
    assert len(item.candidates) == 3


def test_urlscan_fills_historical_reuters_catalog_gaps(tmp_path: Path):
    connection = sqlite3.connect(":memory:")
    initialize_reuters_sitemap_schema(
        connection,
        from_year=2024,
        to_year=2024,
        captures=[],
    )
    added = initialize_reuters_urlscan_queries(
        connection,
        from_year=2024,
        to_year=2024,
        today=date(2025, 1, 1),
    )
    assert added == 53
    pending = pending_reuters_urlscan_queries(
        connection,
        maximum=1,
        maximum_attempts=3,
    )
    assert pending == [("2024-01-01", "2024-01-08")]

    result = process_reuters_urlscan_query(
        connection,
        window_start=pending[0][0],
        window_end=pending[0][1],
        http_client=StubUrlscanClient(),
        from_year=2024,
        to_year=2024,
    )
    connection.execute(
        "UPDATE reuters_urlscan_queries SET status='complete'"
    )
    destination = tmp_path / "urlscan-manifest.jsonl.gz"
    summary = export_reuters_manifest(
        connection,
        destination=destination,
        from_year=2024,
        to_year=2024,
        maximum_attempts=3,
    )

    assert result["status"] == "complete"
    assert result["accepted"] == 1
    assert summary["complete"] is True
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    assert row["canonicalUrl"].endswith(
        "/urlscan-discovered-story-2024-01-03"
    )
    assert row["candidates"][-1] == {
        "provider": "live-origin",
        "snapshotUrl": row["canonicalUrl"],
    }
