from __future__ import annotations

from dataclasses import replace
import gzip
import json
from pathlib import Path
import sqlite3

import httpx

from jojo_olds_api.archive_sources import (
    archive_source_spec,
    article_url_publication_year,
    normalize_article_url,
)
from jojo_olds_api.raw_archive_capture import manifest_item_from_row
from jojo_olds_api.wsj_syndication_catalog import (
    initialize_wsj_syndication_schema,
    process_wsj_syndication_catalog,
    process_wsj_syndication_resolutions,
    resolve_wsj_original_url,
    wsj_syndication_count_for_year,
)
from jojo_olds_api.wayback_manifest import (
    CDXCapture,
    CDXPage,
    candidate_rank,
    discovery_summary,
    export_capture_manifest,
    extract_wsj_legacy_published_at,
    infer_published_at,
    initialize_discovery_schema,
    initialize_wsj_legacy_date_schema,
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


def test_extract_wsj_legacy_published_at_from_at_vars():
    assert extract_wsj_legacy_published_at(
        """<script>AT_VARS = {publicationDate:'2003-01-27'};</script>"""
    ) == "2003-01-27T00:00:00+00:00"


def test_extract_wsj_legacy_published_at_from_json_ld():
    assert extract_wsj_legacy_published_at(
        """<script>{"datePublished":"2014-06-03T12:34:56Z"}</script>"""
    ) == "2014-06-03T12:34:56+00:00"


def test_discovery_schema_accepts_additive_wayback_patterns():
    connection = sqlite3.connect(":memory:")
    current = archive_source_spec("npr")
    original = replace(
        current,
        wayback_patterns=("www.npr.org/{year}/*",),
    )
    initialize_discovery_schema(
        connection,
        spec=original,
        from_year=2010,
        to_year=2010,
        collapse="urlkey",
    )
    connection.execute(
        "UPDATE discovery_queries SET status='complete'"
    )

    initialize_discovery_schema(
        connection,
        spec=current,
        from_year=2010,
        to_year=2010,
        collapse="urlkey",
    )

    assert connection.execute(
        "SELECT pattern, status FROM discovery_queries ORDER BY rowid"
    ).fetchall() == [
        ("www.npr.org/2010/*", "complete"),
        ("npr.org/2010/*", "pending"),
    ]


def test_discovery_schema_rejects_replaced_wayback_patterns():
    connection = sqlite3.connect(":memory:")
    current = archive_source_spec("npr")
    original = replace(
        current,
        wayback_patterns=("legacy.npr.org/{year}/*",),
    )
    initialize_discovery_schema(
        connection,
        spec=original,
        from_year=2010,
        to_year=2010,
        collapse="urlkey",
    )

    try:
        initialize_discovery_schema(
            connection,
            spec=current,
            from_year=2010,
            to_year=2010,
            collapse="urlkey",
        )
    except ValueError as exc:
        assert "different publisher, date window, or spec" in str(exc)
    else:
        raise AssertionError("replaced patterns must invalidate discovery state")


def test_discovery_schema_rejects_additive_patterns_from_another_scope():
    connection = sqlite3.connect(":memory:")
    current = archive_source_spec("npr")
    original = replace(
        current,
        wayback_patterns=("www.npr.org/{year}/*",),
    )
    initialize_discovery_schema(
        connection,
        spec=original,
        from_year=2010,
        to_year=2010,
        collapse="urlkey",
    )

    try:
        initialize_discovery_schema(
            connection,
            spec=replace(current, publisher="not-npr"),
            from_year=2010,
            to_year=2010,
            collapse="urlkey",
        )
    except ValueError as exc:
        assert "different publisher, date window, or spec" in str(exc)
    else:
        raise AssertionError("different publisher must invalidate discovery state")


def test_wsj_legacy_no_date_candidate_is_removed_from_year_pool():
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=archive_source_spec("wsj"),
        from_year=2010,
        to_year=2015,
        collapse="urlkey",
    )
    canonical_url = "https://www.wsj.com/articles/SB100014240527487"
    connection.execute(
        """
        INSERT INTO candidates(
            canonical_url, published_at, timestamp, original_url,
            digest, mimetype, status_code, byte_count, rank_score
        ) VALUES (?, ?, ?, ?, '', 'text/html', 200, 1234, 0)
        """,
        (
            canonical_url,
            "2012-04-03T12:00:00+00:00",
            "20120403120000",
            canonical_url,
        ),
    )
    initialize_wsj_legacy_date_schema(connection)
    connection.execute(
        """
        UPDATE wsj_legacy_date_hydration
        SET status='no-date'
        WHERE canonical_url=?
        """,
        (canonical_url,),
    )

    initialize_wsj_legacy_date_schema(connection)

    assert connection.execute(
        "SELECT COUNT(*) FROM candidates WHERE canonical_url=?",
        (canonical_url,),
    ).fetchone()[0] == 0


class StubWsjSyndicationResponse:
    def __init__(
        self,
        *,
        json_value: object | None = None,
        html_value: str = "",
        status_code: int = 200,
    ):
        self._json_value = json_value
        self.content = html_value.encode()
        self.text = html_value
        self.status_code = status_code

    def json(self):
        return self._json_value

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class StubWsjSyndicationClient:
    def __init__(self):
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url, params, headers=None):
        self.requests.append((url, params))
        if url.endswith("/wp-json/wp/v2/posts"):
            return StubWsjSyndicationResponse(
                json_value=[
                    {
                        "id": 123,
                        "date_gmt": "2024-06-03T12:34:56",
                        "link": (
                            "https://www.tovima.com/wsj/"
                            "a-complete-licensed-wsj-copy/"
                        ),
                        "title": {
                            "rendered": (
                                "Investors Prepare for a Volatile "
                                "Summer in Global Markets"
                            )
                        },
                    },
                    {
                        "id": 456,
                        "date_gmt": "2024-06-04T12:34:56",
                        "link": "https://www.tovima.com/world/not-wsj/",
                        "title": {"rendered": "This row must be rejected"},
                    },
                ]
            )
        assert url == "https://search.yahoo.com/search"
        assert 'site:wsj.com' in params["p"]
        assert headers and headers["User-Agent"].startswith("Mozilla/5.0")
        canonical_url = (
            "https://www.wsj.com/finance/stocks/"
            "investors-prepare-for-a-volatile-summer-in-global-markets-"
            "a1b2c3d4"
        )
        return StubWsjSyndicationResponse(
            html_value=f"""
            <html><body><ol id="web"><li>
              <div class="compTitle"><a href="{canonical_url}">
                <h3>Investors Prepare for a Volatile Summer in Global
                Markets - The Wall Street Journal</h3>
              </a></div>
            </li></ol></body></html>
            """,
        )


def test_wsj_syndication_catalog_resolves_and_exports_partner_copy(
    tmp_path: Path,
):
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2024,
        to_year=2024,
        collapse="urlkey",
    )
    initialize_wsj_syndication_schema(connection)
    client = StubWsjSyndicationClient()

    catalog = process_wsj_syndication_catalog(
        connection,
        http_client=client,
        from_year=2024,
        to_year=2024,
        maximum_pages=1,
    )
    resolutions = process_wsj_syndication_resolutions(
        connection,
        spec=spec,
        http_client=client,
        maximum=10,
    )
    destination = tmp_path / "wsj-syndication-manifest.jsonl.gz"
    summary = export_capture_manifest(
        connection,
        spec=spec,
        destination=destination,
        from_year=2024,
        to_year=2024,
    )

    assert catalog == {
        "status": "complete",
        "pages": 1,
        "seen": 2,
        "accepted": 1,
    }
    assert resolutions == {
        "attempted": 1,
        "resolved": 1,
        "notFound": 0,
        "errors": [],
    }
    assert wsj_syndication_count_for_year(connection, 2024) == 1
    assert summary["articles"] == 1
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        row = json.loads(handle.readline())
    assert row["canonicalUrl"] == (
        "https://www.wsj.com/finance/stocks/"
        "investors-prepare-for-a-volatile-summer-in-global-markets-a1b2c3d4"
    )
    assert row["publishedAt"] == "2024-06-03T12:34:56+00:00"
    assert row["candidates"][0] == {
        "provider": "other",
        "snapshotUrl": (
            "https://www.tovima.com/wsj/"
            "a-complete-licensed-wsj-copy/"
        ),
        "expectedHeadline": (
            "Investors Prepare for a Volatile Summer in Global Markets"
        ),
    }


def test_wsj_syndication_retries_not_found_after_resolver_upgrade():
    connection = sqlite3.connect(":memory:")
    initialize_wsj_syndication_schema(connection)
    connection.execute(
        """
        INSERT INTO wsj_syndication_articles(
            partner_url,
            published_at,
            expected_headline,
            resolution_status,
            resolution_attempts,
            updated_at
        ) VALUES (?, ?, ?, 'not-found', 3, ?)
        """,
        (
            "https://www.tovima.com/wsj/retry-this-copy/",
            "2024-06-03T12:34:56+00:00",
            "A Complete Wall Street Journal Headline",
            "2024-06-03T12:34:56+00:00",
        ),
    )
    connection.execute(
        """
        UPDATE wsj_syndication_metadata
        SET value='legacy-resolver'
        WHERE key='resolver_version'
        """
    )
    connection.commit()

    initialize_wsj_syndication_schema(connection)

    assert connection.execute(
        """
        SELECT resolution_status, resolution_attempts, last_error
        FROM wsj_syndication_articles
        """
    ).fetchone() == ("pending", 0, None)


def test_wsj_syndication_uses_google_news_when_yahoo_errors(
    monkeypatch,
):
    headline = (
        "Trump Makes a Call and U.S. Soccer Gets a Star Back—and "
        "the World Cup Is Raging"
    )
    canonical_url = (
        "https://www.wsj.com/sports/soccer/"
        "balogun-red-card-fifa-trump-infantino-abd58604"
    )

    class GoogleFallbackClient:
        def get(self, url, params, headers=None):
            if url == "https://search.yahoo.com/search":
                return httpx.Response(
                    500,
                    request=httpx.Request("GET", url),
                )
            assert url == "https://news.google.com/rss/search"
            assert params["q"] == f"{headline} site:wsj.com"
            return StubWsjSyndicationResponse(
                html_value="""
                <rss><channel><item>
                  <title>
                    Trump Makes a Call and U.S. Soccer Gets a Star Back—and
                    the World Cup Is Raging - The Wall Street Journal
                  </title>
                  <link>
                    https://news.google.com/rss/articles/ENCODED-ID
                  </link>
                  <pubDate>Mon, 06 Jul 2026 07:00:00 GMT</pubDate>
                </item></channel></rss>
                """
            )

    monkeypatch.setattr(
        "jojo_olds_api.wayback_manifest._decode_google_news_url",
        lambda http_client, url: canonical_url,
    )

    result = resolve_wsj_original_url(
        headline,
        expected_published_at="2026-07-06T23:00:16+00:00",
        spec=archive_source_spec("wsj"),
        http_client=GoogleFallbackClient(),
    )

    assert result == canonical_url


def test_wsj_syndication_rejects_stale_google_news_match(
    monkeypatch,
):
    headline = (
        "Trump Makes a Call and U.S. Soccer Gets a Star Back—and "
        "the World Cup Is Raging"
    )
    decoder_called = False

    class StaleGoogleClient:
        def get(self, url, params, headers=None):
            if url == "https://search.yahoo.com/search":
                return StubWsjSyndicationResponse(
                    html_value="<html><ol id='web'></ol></html>"
                )
            return StubWsjSyndicationResponse(
                html_value="""
                <rss><channel><item>
                  <title>
                    Trump Makes a Call and U.S. Soccer Gets a Star Back—and
                    the World Cup Is Raging - The Wall Street Journal
                  </title>
                  <link>
                    https://news.google.com/rss/articles/ENCODED-ID
                  </link>
                  <pubDate>Mon, 01 Jun 2026 07:00:00 GMT</pubDate>
                </item></channel></rss>
                """
            )

    def decode_google_news_url(http_client, url):
        nonlocal decoder_called
        decoder_called = True
        return (
            "https://www.wsj.com/sports/soccer/"
            "balogun-red-card-fifa-trump-infantino-abd58604"
        )

    monkeypatch.setattr(
        "jojo_olds_api.wayback_manifest._decode_google_news_url",
        decode_google_news_url,
    )

    result = resolve_wsj_original_url(
        headline,
        expected_published_at="2026-07-06T23:00:16+00:00",
        spec=archive_source_spec("wsj"),
        http_client=StaleGoogleClient(),
    )

    assert result is None
    assert decoder_called is False


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
    assert normalize_article_url(
        wsj,
        (
            "https://www.wsj.com/articles/"
            "B3-BY423_health_PREVIEW_20181003165352.jpg"
        ),
    ) is None
    assert normalize_article_url(
        wsj,
        (
            "https://www.wsj.com/articles/"
            "dish-on-this-wednesday-crossword-july-26-48a57b4"
        ),
    ) is None

    reuters = archive_source_spec("reuters")
    assert normalize_article_url(
        reuters,
        "https://www.reuters.com/article/comments/idUS123",
    ) is None
    assert normalize_article_url(
        reuters,
        "https://www.reuters.com/article/slideshow/idUS123",
    ) is None
    assert normalize_article_url(
        reuters,
        "https://www.reuters.com/article/idUSKBN12345620150101",
    ) == (
        "https://www.reuters.com/article/idUSKBN12345620150101"
    )
    assert normalize_article_url(
        reuters,
        "https://www.reuters.com/article/idUS123%3C/body%3E",
    ) is None
    assert normalize_article_url(
        reuters,
        "https://www.reuters.com/article/idUS12320090101%7C",
    ) is None
    assert article_url_publication_year(
        reuters,
        "https://www.reuters.com/article/idUSTRES57D23Q20090816",
    ) == 2009
    wsj = archive_source_spec("wsj")
    assert article_url_publication_year(
        wsj,
        "https://www.wsj.com/articles/"
        "afghans-mourn-for-bombing-victims-1416846693",
    ) == 2014
    assert article_url_publication_year(
        wsj,
        "https://www.wsj.com/articles/"
        "accenture-looks-to-boost-ai-capabilities-through-"
        "mergers-11592818200",
    ) == 2020
    assert article_url_publication_year(
        wsj,
        "https://www.wsj.com/articles/"
        "abbott-beats-forecasts-on-strong-covid-19-testing-"
        "business-151594900170",
    ) == 2020

    ft = archive_source_spec("ft")
    assert normalize_article_url(
        ft,
        "https://ft.com/content/12345678-1234-1234-1234-123456789abc?share=1",
    ) == (
        "https://www.ft.com/content/"
        "12345678-1234-1234-1234-123456789abc"
    )

    assert normalize_article_url(
        archive_source_spec("axios"),
        "https://www.axios.com/2020/01/02/example?utm_source=test",
    ) == "https://www.axios.com/2020/01/02/example"
    assert normalize_article_url(
        archive_source_spec("npr"),
        "https://www.npr.org/2018/02/03/123456789/example",
    ) == "https://www.npr.org/2018/02/03/123456789/example"
    assert normalize_article_url(
        archive_source_spec("npr"),
        "https://www.npr.org/2017/05/29/530555477/example%0A",
    ) == "https://www.npr.org/2017/05/29/530555477/example"
    assert normalize_article_url(
        archive_source_spec("npr"),
        "https://www.npr.org/2018/04/03/598239092/example=",
    ) == "https://www.npr.org/2018/04/03/598239092/example"
    assert normalize_article_url(
        archive_source_spec("npr"),
        "https://www.npr.org/2010/11/02/130682288/election-2010-florida-results",
    ) is None
    assert normalize_article_url(
        archive_source_spec("nikkei"),
        "https://www.nikkei.com/article/DGXZQOCD00001/",
    ) == "https://www.nikkei.com/article/DGXZQOCD00001"
    assert normalize_article_url(
        archive_source_spec("zaobao"),
        "https://www.zaobao.com.sg/news/singapore/story20240102-1234567",
    ) == "https://www.zaobao.com.sg/news/singapore/story20240102-1234567"
    assert normalize_article_url(
        archive_source_spec("aljazeera"),
        "https://www.aljazeera.com/news/2020/1/2/example",
    ) == "https://www.aljazeera.com/news/2020/1/2/example"
    assert normalize_article_url(
        archive_source_spec("scmp"),
        "https://www.scmp.com/article/721725/corrections-clarifications",
    ) == "https://www.scmp.com/article/721725/corrections-clarifications"
    assert normalize_article_url(
        archive_source_spec("caixin"),
        "https://magazine.caixin.com/2010/cw385/?utm=1",
    ) == "https://magazine.caixin.com/2010/cw385"


def test_date_inference_and_candidate_ranking_prefers_after_publication():
    published = infer_published_at(
        "https://www.nytimes.com/2020/01/02/world/example.html"
    )
    assert published == "2020-01-02T00:00:00+00:00"
    after = candidate_rank("20200102010000", published_at=published)
    before = candidate_rank("20200101010000", published_at=published)
    assert after < before
    assert infer_published_at(
        "https://www.reuters.com/article/"
        "01cyberaton-brief-idUSFWN0U201D20141218"
    ) == "2014-12-18T00:00:00+00:00"
    assert infer_published_at(
        "https://www.wsj.com/article/"
        "0,,BT-CO-20130516-704945,00.html"
    ) == "2013-05-16T00:00:00+00:00"
    assert infer_published_at(
        "https://www.wsj.com/articles/"
        "a-19th-century-island-home-in-south-carolina-1472740999"
    ) == "2016-09-01T00:00:00+00:00"
    assert infer_published_at(
        "https://www.wsj.com/articles/"
        "accenture-looks-to-boost-ai-capabilities-through-"
        "mergers-11592818200"
    ) == "2020-06-22T00:00:00+00:00"
    assert infer_published_at(
        "https://www.wsj.com/articles/"
        "abbott-beats-forecasts-on-strong-covid-19-testing-"
        "business-151594900170"
    ) == "2020-07-16T00:00:00+00:00"


def test_reuters_discovery_reclassifies_legacy_ids_by_publication_date():
    spec = archive_source_spec("reuters")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2010,
        to_year=2015,
    )
    url = (
        "https://www.reuters.com/article/"
        "example-idUSL1N0AB12320120607"
    )
    connection.execute(
        """
        INSERT INTO candidates(
            canonical_url, published_at, timestamp, original_url,
            digest, mimetype, status_code, byte_count, rank_score
        ) VALUES (?, '2015-01-01T00:00:00+00:00', '20150102000000',
                  ?, 'digest', 'text/html', 200, 10000, 1)
        """,
        (url, url),
    )

    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2010,
        to_year=2015,
    )

    assert connection.execute(
        "SELECT published_at FROM candidates WHERE canonical_url=?",
        (url,),
    ).fetchone() == ("2012-06-07T00:00:00+00:00",)


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


def test_wsj_export_rejects_legacy_asset_rows(tmp_path: Path):
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2023,
        to_year=2023,
    )
    article_url = (
        "https://www.wsj.com/articles/"
        "markets-rally-on-new-economic-data-a1b2c3d4"
    )
    asset_url = (
        "https://www.wsj.com/articles/"
        "B3-BY423_health_PREVIEW_20181003165352.jpg"
    )
    connection.executemany(
        """
        INSERT INTO candidates(
            canonical_url, published_at, timestamp, original_url,
            digest, mimetype, status_code, byte_count, rank_score
        ) VALUES (?, '2023-06-01T00:00:00+00:00', '20230602000000',
                  ?, 'digest', 'text/html', 200, 10000, 1)
        """,
        ((article_url, article_url), (asset_url, asset_url)),
    )

    destination = tmp_path / "manifest.jsonl.gz"
    summary = export_capture_manifest(
        connection,
        spec=spec,
        destination=destination,
        from_year=2023,
        to_year=2023,
        capture_minimum_per_year=1,
    )

    assert wsj_catalog_count_for_year(connection, 2023) == 1
    assert summary["articles"] == 1
    assert summary["yearCounts"] == {"2023": 1}
    with gzip.open(destination, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert [row["canonicalUrl"] for row in rows] == [article_url]


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
    result = record_discovery_page(
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


def test_discovery_initialization_prunes_out_of_window_candidates():
    spec = archive_source_spec("ft")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2020,
        to_year=2020,
        collapse="urlkey",
    )
    pattern, _ = next_discovery_query(connection)
    result = record_discovery_page(
        connection,
        spec=spec,
        pattern=pattern,
        page=CDXPage(
            captures=(
                CDXCapture(
                    timestamp="20130315120000",
                    original=(
                        "https://www.ft.com/content/"
                        "31fb47f2-9782-11e6-a1dc-bdf38d484582"
                    ),
                    mimetype="text/html",
                    status_code=200,
                    digest="OLD",
                    length=50_000,
                ),
            ),
            resume_key=None,
        ),
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM candidates"
    ).fetchone()[0] == 0
    assert result == {
        "seen": 1,
        "accepted": 0,
        "hasMore": False,
    }
    connection.execute(
        """
        INSERT INTO candidates(
            canonical_url,
            published_at,
            timestamp,
            original_url,
            digest,
            mimetype,
            status_code,
            byte_count,
            rank_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                "https://www.ft.com/content/"
                "31fb47f2-9782-11e6-a1dc-bdf38d484582"
            ),
            "2013-03-15T12:00:00+00:00",
            "20130315120000",
            (
                "https://www.ft.com/content/"
                "31fb47f2-9782-11e6-a1dc-bdf38d484582"
            ),
            "OLD-MANUAL",
            "text/html",
            200,
            50_000,
            0,
        ),
    )
    connection.commit()

    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2020,
        to_year=2020,
        collapse="urlkey",
    )

    assert connection.execute(
        "SELECT COUNT(*) FROM candidates"
    ).fetchone()[0] == 0


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


def test_pre_2014_wsj_discovery_prioritizes_legacy_article_urls():
    spec = archive_source_spec("wsj")
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=spec,
        from_year=2010,
        to_year=2015,
        collapse="urlkey",
    )

    pattern, _ = next_discovery_query(connection)

    assert pattern == "online.wsj.com/article/*"


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
