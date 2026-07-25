from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3

from jojo_olds_api.archive_sources import archive_source_spec
from jojo_olds_api.raw_archive_capture import manifest_item_from_row
from jojo_olds_api.sitemap_manifest import (
    export_sitemap_manifest,
    initialize_sitemap_schema,
    next_sitemap_query,
    parse_sitemap_index,
    parse_url_sitemap,
    record_sitemap,
    sitemap_wayback_candidates,
    sitemap_source,
    wayback_candidates,
)


INDEX_XML = b"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.nytimes.com/sitemaps/new/sitemap-2019-12.xml.gz</loc></sitemap>
  <sitemap><loc>https://www.nytimes.com/sitemaps/new/sitemap-2020-01.xml.gz</loc></sitemap>
  <sitemap><loc>https://www.nytimes.com/sitemaps/new/sitemap-2021-02.xml.gz</loc></sitemap>
</sitemapindex>
"""

URL_XML = b"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.nytimes.com/2020/01/14/dining/example.html?utm_source=x</loc>
    <lastmod>2020-01-14T10:00:00Z</lastmod>
  </url>
  <url>
    <loc>https://www.nytimes.com/crosswords/game</loc>
    <lastmod>2020-01-14T10:00:00Z</lastmod>
  </url>
</urlset>
"""


def test_index_and_url_sitemap_parsing():
    source = sitemap_source("nyt")
    children = parse_sitemap_index(
        INDEX_XML,
        source=source,
        from_year=2020,
        to_year=2020,
    )
    assert children == [
        (
            "https://www.nytimes.com/sitemaps/new/sitemap-2020-01.xml.gz",
            2020,
            1,
        )
    ]
    assert parse_url_sitemap(URL_XML)[0][1] == "2020-01-14T10:00:00Z"


def test_sitemap_state_exports_publication_near_wayback_candidates(
    tmp_path: Path,
):
    source = sitemap_source("nyt")
    connection = sqlite3.connect(":memory:")
    initialize_sitemap_schema(
        connection,
        source=source,
        from_year=2020,
        to_year=2020,
        sitemap_index=INDEX_XML,
    )
    query = next_sitemap_query(connection)
    assert query is not None
    result = record_sitemap(
        connection,
        publisher_spec=archive_source_spec("nyt"),
        sitemap_url=query[0],
        year=query[1],
        month=query[2],
        content=URL_XML,
    )
    destination = tmp_path / "manifest.jsonl.gz"
    summary = export_sitemap_manifest(
        connection,
        publisher="nyt",
        destination=destination,
        from_year=2020,
        to_year=2020,
    )

    assert result == {"seen": 2, "accepted": 1}
    assert summary["complete"] is True
    assert summary["articles"] == 1
    assert summary["candidates"] == 3
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    item = manifest_item_from_row(row, publisher="nyt")
    assert item.published_at == "2020-01-14T00:00:00+00:00"
    assert len(item.candidates) == 3
    assert "/web/20200115000000id_/" in item.candidates[0].snapshot_url
    assert item.candidates[0].captured_at is None


def test_candidate_fallback_for_unknown_publication_date_uses_latest():
    result = wayback_candidates(
        "https://www.ft.com/content/example",
        published_at=None,
    )
    assert result == [
        {
            "provider": "wayback",
            "snapshotUrl": (
                "https://web.archive.org/web/2id_/"
                "https://www.ft.com/content/example"
            ),
        }
    ]


def test_ft_sitemap_candidates_try_amp_before_canonical():
    result = sitemap_wayback_candidates(
        "ft",
        "https://www.ft.com/content/fd3df9ba-4480-11ea-abea-0c7a29cd66fe",
        published_at="2020-02-01T00:00:00Z",
    )

    assert len(result) == 6
    assert result[0]["snapshotUrl"].endswith(
        "https://amp.ft.com/content/fd3df9ba-4480-11ea-abea-0c7a29cd66fe"
    )
    assert result[3]["snapshotUrl"].endswith(
        "https://www.ft.com/content/fd3df9ba-4480-11ea-abea-0c7a29cd66fe"
    )


def test_current_year_sitemap_candidates_include_live_fallback():
    year = datetime.now(timezone.utc).year
    canonical_url = (
        f"https://www.nytimes.com/{year}/01/02/world/example.html"
    )

    result = sitemap_wayback_candidates(
        "nyt",
        canonical_url,
        published_at=f"{year}-01-02T00:00:00+00:00",
    )

    assert len(result) == 4
    assert result[-1] == {
        "provider": "live-origin",
        "snapshotUrl": canonical_url,
    }
