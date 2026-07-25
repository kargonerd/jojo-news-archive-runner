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
    next_discovery_query,
    parse_cdx_json,
    record_discovery_page,
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
