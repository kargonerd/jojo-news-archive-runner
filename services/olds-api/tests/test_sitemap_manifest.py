from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3

from jojo_olds_api.archive_sources import archive_source_spec
from jojo_olds_api.ft_syndication_catalog import (
    _next_document_rows,
    _next_resolution_rows,
    ft_syndication_summary,
    initialize_ft_syndication_schema,
    process_ft_infini_documents,
    process_ft_infini_queries,
    process_ft_syndication_resolutions,
    resolve_ft_original_url,
)
from jojo_olds_api.raw_archive_capture import manifest_item_from_row
from jojo_olds_api.nyt_syndication_catalog import (
    initialize_nyt_syndication_schema,
    next_nyt_syndication_resolution,
    next_nyt_syndication_query,
    nyt_syndication_summary,
    record_nyt_syndication_page,
    record_nyt_syndication_resolution,
    resolve_nyt_syndication_search,
)
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


class StubFtSyndicationResponse:
    def __init__(
        self,
        *,
        json_value: object | None = None,
        html_value: str = "",
        status_code: int = 200,
    ):
        self._json_value = json_value
        self.content = html_value.encode()
        self.status_code = status_code

    def json(self):
        return self._json_value

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class StubFtSyndicationClient:
    def post(self, url, json):
        if url.endswith("/find"):
            assert json["query"] == (
                "Copyright The Financial Times Limited"
            )
            return StubFtSyndicationResponse(
                json_value={
                    "count": 1,
                    "segment_by_shard": [[100, 101]],
                    "shard_years": ["2024"],
                }
            )
        assert url.endswith("/get_doc")
        assert json["rank"] == 100
        return StubFtSyndicationResponse(
            json_value={
                "doc_ix": 12345,
                "doc_len": 5_000,
                "metadata": {
                    "url": (
                        "https://www.irishtimes.com/business/2024/03/28/"
                        "amazon-invests-in-ai-start-up/"
                    ),
                    "date": "2024-03-28",
                    "language": "eng",
                    "hostname": "www.irishtimes.com",
                    "warc_source": (
                        "CC-NEWS-20240328160318-02712.warc.gz"
                    ),
                    "title": (
                        "Amazon writes its largest venture cheque yet "
                        "for AI start-up Anthropic"
                    ),
                },
            }
        )

    def get(self, url, params, headers=None):
        assert url == "https://search.yahoo.com/search"
        assert "site:ft.com" in params["p"]
        assert headers and headers["User-Agent"].startswith("Mozilla/5.0")
        return StubFtSyndicationResponse(
            html_value="""
            <html><body><ol id="web"><li>
              <div class="compTitle">
                <a href="https://www.ft.com/content/a604bc55-26a5-42ca-a707-e6537abe0c1d">
                  <h3>Amazon writes its largest venture cheque yet for
                  AI start-up Anthropic</h3>
                </a>
              </div>
            </li></ol></body></html>
            """
        )


def test_ft_infini_catalog_resolves_and_exports_licensed_copy(
    tmp_path: Path,
):
    connection = sqlite3.connect(":memory:")
    initialize_sitemap_schema(
        connection,
        source=sitemap_source("ft"),
        from_year=2024,
        to_year=2024,
        sitemap_index=INDEX_XML,
    )
    initialize_ft_syndication_schema(
        connection,
        from_year=2024,
        to_year=2024,
    )
    client = StubFtSyndicationClient()

    query_result = process_ft_infini_queries(
        connection,
        http_client=client,
        maximum_years=1,
    )
    document_result = process_ft_infini_documents(
        connection,
        http_client=client,
        maximum=1,
        workers=1,
        minimum_request_interval=0,
    )
    resolution_result = process_ft_syndication_resolutions(
        connection,
        http_client=client,
        maximum=1,
        minimum_request_interval=0,
    )
    connection.execute(
        """
        INSERT INTO sitemap_articles(
            canonical_url,
            published_at,
            source_sitemap,
            updated_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            (
                "https://www.ft.com/content/"
                "a604bc55-26a5-42ca-a707-e6537abe0c1d"
            ),
            "2024-03-15T12:00:00+00:00",
            "https://www.ft.com/sitemaps/2024-03.xml",
            "2024-03-28T16:00:00+00:00",
        ),
    )
    connection.commit()
    destination = tmp_path / "ft-syndication-manifest.jsonl.gz"
    manifest = export_sitemap_manifest(
        connection,
        publisher="ft",
        destination=destination,
        from_year=2024,
        to_year=2024,
    )

    assert query_result == {
        "processed": 1,
        "occurrences": 1,
        "errors": [],
    }
    assert document_result == {
        "attempted": 1,
        "accepted": 1,
        "rejected": 0,
        "errors": [],
    }
    assert resolution_result == {
        "attempted": 1,
        "resolved": 1,
        "notFound": 0,
        "errors": [],
    }
    assert ft_syndication_summary(connection) == {
        "queriesByStatus": {"complete": 1},
        "occurrencesByStatus": {"accepted": 1},
        "resolutionsByStatus": {"resolved": 1},
        "articlesByYear": {"2024": 1},
        "articles": 1,
        "shouldContinue": False,
    }
    assert manifest["articles"] == 1
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    assert row["canonicalUrl"] == (
        "https://www.ft.com/content/"
        "a604bc55-26a5-42ca-a707-e6537abe0c1d"
    )
    assert row["publishedAt"] == "2024-03-28T00:00:00+00:00"
    assert row["candidates"][0] == {
        "provider": "other",
        "snapshotUrl": (
            "https://www.irishtimes.com/business/2024/03/28/"
            "amazon-invests-in-ai-start-up/"
        ),
        "expectedHeadline": (
            "Amazon writes its largest venture cheque yet "
            "for AI start-up Anthropic"
        ),
    }


def test_ft_catalog_work_is_balanced_across_years():
    connection = sqlite3.connect(":memory:")
    initialize_ft_syndication_schema(
        connection,
        from_year=2016,
        to_year=2018,
    )
    now = datetime.now(timezone.utc).isoformat()
    for year in range(2016, 2019):
        for rank in range(3):
            connection.execute(
                """
                INSERT INTO ft_syndication_occurrences(
                    year, shard_index, rank, updated_at
                ) VALUES (?, 0, ?, ?)
                """,
                (year, rank, now),
            )
            connection.execute(
                """
                INSERT INTO ft_syndication_unresolved(
                    partner_url,
                    published_at,
                    expected_headline,
                    source_year,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"https://partner.example/{year}/{rank}",
                    f"{year}-01-{rank + 1:02d}T00:00:00+00:00",
                    f"Financial Times licensed article {year} number {rank}",
                    year,
                    now,
                ),
            )
    connection.commit()

    document_rows = _next_document_rows(connection, maximum=6)
    assert [row[0] for row in document_rows] == [
        2018,
        2017,
        2016,
        2018,
        2017,
        2016,
    ]
    resolution_rows = _next_resolution_rows(connection, maximum=6)
    assert [int(row[3]) for row in resolution_rows] == [
        2018,
        2017,
        2016,
        2018,
        2017,
        2016,
    ]


def test_ft_original_resolution_rejects_partial_title_match():
    expected_headline = (
        "Amazon writes its largest venture cheque yet "
        "for AI start-up Anthropic"
    )
    canonical_url = (
        "https://www.ft.com/content/"
        "a604bc55-26a5-42ca-a707-e6537abe0c1d"
    )

    class PartialTitleClient:
        def get(self, url, params, headers=None):
            assert url == "https://search.yahoo.com/search"
            assert params["p"].endswith("site:ft.com")
            assert headers and headers["User-Agent"].startswith(
                "Mozilla/5.0"
            )
            return StubFtSyndicationResponse(
                html_value=f"""
                <html><body><ol id="web"><li>
                  <div class="compTitle"><a href="{canonical_url}">
                    <h3>Amazon writes largest venture cheque for Anthropic</h3>
                  </a></div>
                </li></ol></body></html>
                """
            )

    result = resolve_ft_original_url(
        expected_headline,
        spec=archive_source_spec("ft"),
        http_client=PartialTitleClient(),
    )

    assert result is None


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


def test_nyt_partner_catalog_adds_exact_canonical_direct_candidate(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.nytimes.com/2026/04/15/us/"
        "floods-michigan-cheboygan-dams-evacuation.html"
    )
    syndicated_url = (
        "https://www.hawaiitribune-herald.com/2026/04/16/"
        "nation-world-news/dam-failure-could-imperil-thousands/"
    )
    connection = sqlite3.connect(":memory:")
    initialize_sitemap_schema(
        connection,
        source=sitemap_source("nyt"),
        from_year=2026,
        to_year=2026,
        sitemap_index=INDEX_XML,
    )
    initialize_nyt_syndication_schema(
        connection,
        from_year=2026,
        to_year=2026,
    )
    query = next_nyt_syndication_query(connection)
    assert query is not None
    year, page, request_url = query
    content = json.dumps(
        [
            {
                "date": "2026-04-16T00:05:00",
                "date_gmt": "2026-04-16T10:05:00",
                "link": syndicated_url,
                "title": {
                    "rendered": (
                        "Dam failure could imperil thousands "
                        "in Northern Michigan"
                    )
                },
                "content": {
                    "rendered": (
                        "<p>Full licensed article body.</p>"
                        "<ins>This article originally appeared in "
                        f'<a href="{canonical_url}">'
                        "The New York Times</a>.</ins>"
                    )
                },
            },
            {
                "date": "2026-04-16T00:05:00",
                "link": "https://example.com/unrelated",
                "title": {"rendered": "Unrelated article"},
                "content": {"rendered": "<p>No canonical source link.</p>"},
            },
        ]
    ).encode()
    result = record_nyt_syndication_page(
        connection,
        year=year,
        page=page,
        request_url=request_url,
        content=content,
        total_pages=2,
    )

    assert result == {
        "seen": 2,
        "accepted": 1,
        "unresolved": 0,
        "totalPages": 2,
    }
    next_query = next_nyt_syndication_query(connection)
    assert next_query is not None
    assert next_query[0:2] == (2026, 2)
    assert nyt_syndication_summary(connection) == {
        "queriesByStatus": {"complete": 1, "pending": 1},
        "articles": 1,
        "resolutionByStatus": {},
        "resolutionNeeded": 0,
        "shouldContinue": True,
    }

    destination = tmp_path / "nyt-partner-manifest.jsonl.gz"
    summary = export_sitemap_manifest(
        connection,
        publisher="nyt",
        destination=destination,
        from_year=2026,
        to_year=2026,
    )
    assert summary["articles"] == 1
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    item = manifest_item_from_row(row, publisher="nyt")
    assert item.canonical_url == canonical_url
    assert item.published_at == "2026-04-15T00:00:00+00:00"
    assert item.candidates[0].provider.value == "other"
    assert item.candidates[0].snapshot_url == syndicated_url
    assert (
        item.candidates[0].expected_headline
        == "Dam failure could imperil thousands in Northern Michigan"
    )


def test_nyt_partner_catalog_resolves_legacy_copy_by_title_and_date():
    canonical_url = (
        "https://www.nytimes.com/2024/01/01/upshot/"
        "2024-election-trump-biden.html"
    )
    syndicated_url = (
        "https://www.hawaiitribune-herald.com/2024/01/02/"
        "nation-world-news/looking-ahead-to-five-things/"
    )
    headline = "Looking ahead to 5 things that will shape the 2024 election"
    connection = sqlite3.connect(":memory:")
    initialize_nyt_syndication_schema(
        connection,
        from_year=2024,
        to_year=2024,
    )
    query = next_nyt_syndication_query(connection)
    assert query is not None
    year, page, request_url = query
    result = record_nyt_syndication_page(
        connection,
        year=year,
        page=page,
        request_url=request_url,
        content=json.dumps(
            [
                {
                    "date": "2024-01-02T00:05:00",
                    "date_gmt": "2024-01-02T10:05:00",
                    "link": syndicated_url,
                    "title": {"rendered": headline},
                    "content": {
                        "rendered": (
                            "<p>Legacy full body without a source link.</p>"
                        )
                    },
                }
            ]
        ).encode(),
        total_pages=1,
    )
    assert result["accepted"] == 0
    assert result["unresolved"] == 1
    resolution = next_nyt_syndication_resolution(connection)
    assert resolution is not None
    search_html = f"""
    <html><body><ol id="web"><li><div class="compTitle">
      <a href="{canonical_url}"><h3>
        Looking Ahead to 5 Things That Will Shape the 2024 Election
      </h3></a>
    </div></li></ol></body></html>
    """.encode()
    resolved = resolve_nyt_syndication_search(
        search_html,
        headline=headline,
        partner_published_at="2024-01-02T10:05:00",
    )
    assert resolved == (
        canonical_url,
        "2024-01-01T00:00:00+00:00",
    )
    record_nyt_syndication_resolution(
        connection,
        syndicated_url=syndicated_url,
        partner_published_at="2024-01-02T10:05:00",
        headline=headline,
        source_endpoint="https://example.com/wp-json/wp/v2/posts",
        resolved=resolved,
    )
    assert next_nyt_syndication_resolution(connection) is None
    assert nyt_syndication_summary(connection) == {
        "queriesByStatus": {"complete": 1},
        "articles": 1,
        "resolutionByStatus": {"complete": 1},
        "resolutionNeeded": 0,
        "shouldContinue": False,
    }


def test_ap_historical_sitemap_candidates_include_live_fallback():
    canonical_url = (
        "https://apnews.com/article/"
        "historical-story-0123456789abcdef0123456789abcdef"
    )

    result = sitemap_wayback_candidates(
        "ap",
        canonical_url,
        published_at="2016-01-15T12:00:00+00:00",
    )

    assert len(result) == 4
    assert result[0] == {
        "provider": "live-origin",
        "snapshotUrl": canonical_url,
    }
    assert all(
        candidate["provider"] == "wayback"
        for candidate in result[1:]
    )
