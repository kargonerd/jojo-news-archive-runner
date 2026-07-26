from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3

from jojo_olds_api.news_models import (
    CaptureCandidate,
    CaptureProvider,
    RawCapture,
)
from jojo_olds_api.raw_archive_capture import (
    BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS,
    FT_SYNDICATION_MINIMUM_BODY_CHARACTERS,
    ManifestItem,
    NYT_SYNDICATION_MINIMUM_BODY_CHARACTERS,
    NYT_SYNDICATION_SEARCH_ENDPOINT,
    REUTERS_SYNDICATION_SEARCH_ENDPOINT,
    WAYBACK_TIMEMAP_ENDPOINT,
    WSJ_SYNDICATION_MINIMUM_BODY_CHARACTERS,
    bloomberg_syndication_search_url,
    capture_item,
    capture_summary,
    completed_capture_rejection_reason,
    initialize_capture_schema,
    load_capture_manifest,
    mark_capture_downloading,
    nyt_headline_wordpress_search_url,
    nyt_syndication_search_url,
    nyt_syndication_title_search_url,
    nyt_trusted_wordpress_search_url,
    pending_captures,
    record_capture_result,
    reuters_syndication_search_url,
    reset_completed_capture_for_retry,
    resolved_capture_candidate,
    score_raw_capture,
    store_raw_html,
)


ARTICLE = b"""
<!doctype html>
<html>
  <head>
    <script type="application/ld+json">
      {"@type":"NewsArticle","headline":"Captured headline"}
    </script>
  </head>
  <body><article><p>Captured article body.</p></article></body>
</html>
""" + (b" " * 2_048)


class StubArchiveClient:
    def __init__(self, responses: dict[str, tuple[int, dict[str, str], bytes, str]]):
        self.responses = responses
        self.requests: list[str] = []

    def fetch(self, url: str, *, maximum_bytes: int):
        self.requests.append(url)
        response = self.responses[url]
        if len(response[2]) > maximum_bytes:
            raise ValueError("too large")
        return response


def candidate(url: str, timestamp: str) -> CaptureCandidate:
    return CaptureCandidate(
        provider=CaptureProvider.WAYBACK,
        snapshot_url=url,
        captured_at=datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        ),
        mime_type="text/html",
        status_code=200,
    )


def wsj_syndication_html(
    *,
    headline: str,
    attribution: str,
) -> bytes:
    paragraphs = "".join(
        (
            "<p>Licensed financial reporting paragraph "
            f"{index} contains substantive market analysis, interviews "
            "with investors, company disclosures, historical comparisons "
            "and enough detail to represent the complete news article "
            "rather than a headline, preview, or subscription prompt.</p>"
        )
        for index in range(1, 6)
    )
    return f"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "{headline}",
        "datePublished": "2024-06-03T12:34:56Z",
        "author": {{"name": "{attribution}"}}
      }}
      </script>
    </head><body><article>
      <p>By Market Reporter, {attribution}</p>
      {paragraphs}
    </article></body></html>
    """.encode() + (b" " * 2_048)


def ft_syndication_html(
    *,
    headline: str,
    include_copyright: bool,
) -> bytes:
    paragraphs = "".join(
        (
            "<p>Licensed business reporting paragraph "
            f"{index} contains substantive details about the investment, "
            "executive comments, market conditions, financial disclosures "
            "and the competitive outlook. This is complete article text "
            "rather than a preview, search result, or subscription shell.</p>"
        )
        for index in range(1, 6)
    )
    copyright_text = (
        "<p>Copyright The Financial Times Limited 2024</p>"
        if include_copyright
        else ""
    )
    return f"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "{headline}",
        "datePublished": "2024-03-28T15:45:53Z",
        "author": {{"name": "Financial Times reporters"}}
      }}
      </script>
    </head><body><article>
      {paragraphs}
      {copyright_text}
    </article></body></html>
    """.encode() + (b" " * 2_048)


def test_ft_manifest_partner_copy_requires_strict_validation(
    tmp_path: Path,
):
    headline = (
        "Amazon writes its largest venture cheque yet "
        "for AI start-up Anthropic"
    )
    canonical_url = (
        "https://www.ft.com/content/"
        "a604bc55-26a5-42ca-a707-e6537abe0c1d"
    )
    partner_url = (
        "https://www.irishtimes.com/business/2024/03/28/"
        "amazon-invests-in-ai-start-up/"
    )
    item = ManifestItem(
        publisher="ft",
        canonical_url=canonical_url,
        published_at="2024-03-28T00:00:00+00:00",
        section="business",
        candidates=(
            CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
                expected_headline=headline,
            ),
        ),
    )
    client = StubArchiveClient(
        {
            partner_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                ft_syndication_html(
                    headline=headline,
                    include_copyright=True,
                ),
                partner_url,
            )
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    capture = result["capture"]
    assert capture.selected_candidate.provider == CaptureProvider.OTHER
    assert capture.quality_signals["ftSyndicationValidated"] is True
    assert (
        capture.quality_signals["syndicationFtCopyrightAttributed"]
        is True
    )
    assert (
        capture.quality_signals["syndicationBodyCharacters"]
        >= FT_SYNDICATION_MINIMUM_BODY_CHARACTERS
    )
    assert capture.quality_signals["syndicationHeadlineOverlap"] == 1.0
    assert capture.quality_signals["syndicationPartnerHostValidated"] is True


def test_ft_manifest_partner_copy_rejects_missing_copyright(
    tmp_path: Path,
):
    headline = (
        "Amazon writes its largest venture cheque yet "
        "for AI start-up Anthropic"
    )
    partner_url = (
        "https://www.irishtimes.com/business/2024/03/28/"
        "independent-related-article/"
    )
    item = ManifestItem(
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "a604bc55-26a5-42ca-a707-e6537abe0c1d"
        ),
        published_at="2024-03-28T00:00:00+00:00",
        section="business",
        candidates=(
            CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
                expected_headline=headline,
            ),
        ),
    )
    client = StubArchiveClient(
        {
            partner_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                ft_syndication_html(
                    headline=headline,
                    include_copyright=False,
                ),
                partner_url,
            )
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "error"
    assert "missing-ft-copyright" in result["error"]
    assert not (tmp_path / "objects").exists()


def test_wsj_manifest_partner_copy_requires_strict_validation(
    tmp_path: Path,
):
    headline = "Investors Prepare for a Volatile Summer in Global Markets"
    canonical_url = (
        "https://www.wsj.com/finance/stocks/"
        "investors-prepare-for-a-volatile-summer-in-global-markets-a1b2c3d4"
    )
    partner_url = (
        "https://www.tovima.com/wsj/"
        "a-complete-licensed-wsj-copy/"
    )
    item = ManifestItem(
        publisher="wsj",
        canonical_url=canonical_url,
        published_at="2024-06-03T12:34:56+00:00",
        section="finance",
        candidates=(
            CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
                expected_headline=headline,
            ),
        ),
    )
    client = StubArchiveClient(
        {
            partner_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                wsj_syndication_html(
                    headline=headline,
                    attribution="The Wall Street Journal",
                ),
                partner_url,
            )
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    capture = result["capture"]
    assert capture.selected_candidate.provider == CaptureProvider.OTHER
    assert capture.quality_signals["wsjSyndicationValidated"] is True
    assert capture.quality_signals["syndicationWsjAttributed"] is True
    assert (
        capture.quality_signals["syndicationBodyCharacters"]
        >= WSJ_SYNDICATION_MINIMUM_BODY_CHARACTERS
    )
    assert capture.quality_signals["syndicationHeadlineOverlap"] == 1.0
    assert capture.quality_signals["syndicationPartnerHostValidated"] is True


def test_wsj_manifest_partner_copy_rejects_missing_attribution(
    tmp_path: Path,
):
    headline = "Investors Prepare for a Volatile Summer in Global Markets"
    partner_url = "https://www.tovima.com/wsj/unattributed-copy/"
    item = ManifestItem(
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/finance/stocks/"
            "investors-prepare-for-a-volatile-summer-in-global-markets-"
            "a1b2c3d4"
        ),
        published_at="2024-06-03T12:34:56+00:00",
        section="finance",
        candidates=(
            CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
                expected_headline=headline,
            ),
        ),
    )
    client = StubArchiveClient(
        {
            partner_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                wsj_syndication_html(
                    headline=headline,
                    attribution="Independent Market Desk",
                ),
                partner_url,
            )
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "error"
    assert "missing-wsj-attribution" in result["error"]
    assert not (tmp_path / "objects").exists()


def test_wsj_manifest_partner_copy_rejects_unapproved_host(
    tmp_path: Path,
):
    headline = "Investors Prepare for a Volatile Summer in Global Markets"
    partner_url = "https://example.com/wsj/copied-page/"
    item = ManifestItem(
        publisher="wsj",
        canonical_url=(
            "https://www.wsj.com/finance/stocks/"
            "investors-prepare-for-a-volatile-summer-in-global-markets-"
            "a1b2c3d4"
        ),
        published_at="2024-06-03T12:34:56+00:00",
        section="finance",
        candidates=(
            CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=partner_url,
                expected_headline=headline,
            ),
        ),
    )
    client = StubArchiveClient(
        {
            partner_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                wsj_syndication_html(
                    headline=headline,
                    attribution="The Wall Street Journal",
                ),
                partner_url,
            )
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "error"
    assert "unexpected-partner-url" in result["error"]
    assert not (tmp_path / "objects").exists()


def test_legacy_manifest_loads_into_generic_capture_state(tmp_path: Path):
    manifest = tmp_path / "manifest.jsonl.gz"
    row = {
        "url": "https://www.bloomberg.com/news/articles/2020-01-01/example",
        "catalog_date": "2020-01-01T00:00:00+00:00",
        "section": "news",
        "wayback_timestamp": "20200102125527",
        "wayback_snapshot_url": (
            "https://web.archive.org/web/20200102125527id_/"
            "https://www.bloomberg.com/news/articles/2020-01-01/example"
        ),
        "wayback_digest": "EXAMPLE",
        "wayback_mimetype": "text/html",
    }
    with gzip.open(manifest, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")

    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="bloomberg",
        authorization_reference="authorization:test",
    )
    result = load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="bloomberg",
    )
    selected = pending_captures(
        connection,
        retry_errors=False,
        maximum=None,
        maximum_record_attempts=3,
    )

    assert result == {"manifestRows": 1, "inserted": 1}
    assert len(selected) == 1
    assert selected[0].publisher == "bloomberg"
    assert selected[0].candidates[0].provider == CaptureProvider.WAYBACK
    assert (
        selected[0].candidates[0].captured_at
        == datetime(2020, 1, 2, 12, 55, 27, tzinfo=timezone.utc)
    )


def test_manifest_refresh_retries_errors_when_candidates_change(
    tmp_path: Path,
):
    canonical_url = "https://apnews.com/article/manifest-refresh"
    wayback_url = (
        "https://web.archive.org/web/20260102000000id_/"
        f"{canonical_url}"
    )
    manifest = tmp_path / "manifest.jsonl"

    def write_manifest(candidates: list[dict[str, object]]) -> None:
        manifest.write_text(
            json.dumps(
                {
                    "publisher": "ap",
                    "canonicalUrl": canonical_url,
                    "publishedAt": "2026-01-01T00:00:00Z",
                    "candidates": candidates,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    write_manifest(
        [
            {
                "provider": "wayback",
                "snapshotUrl": wayback_url,
            }
        ]
    )
    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="ap",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="ap",
    )
    connection.execute(
        """
        UPDATE captures
        SET status='error',
            attempts=3,
            last_error='old candidates exhausted'
        """
    )
    connection.commit()

    write_manifest(
        [
            {
                "provider": "wayback",
                "snapshotUrl": wayback_url,
            },
            {
                "provider": "live-origin",
                "snapshotUrl": canonical_url,
            },
        ]
    )
    result = load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="ap",
    )
    row = connection.execute(
        """
        SELECT status, attempts, last_error, candidates_json
        FROM captures
        """
    ).fetchone()

    assert result == {"manifestRows": 1, "inserted": 1}
    assert row[0:3] == ("pending", 0, None)
    assert [
        candidate["provider"]
        for candidate in json.loads(row[3])
    ] == ["wayback", "live-origin"]


def test_capture_tries_fallback_and_stores_only_usable_raw_html(tmp_path: Path):
    first_url = "https://web.archive.org/web/20200101000000id_/https://example.com/a"
    second_url = "https://web.archive.org/web/20200102000000id_/https://example.com/a"
    error_page = (
        b"<html>Wayback Machine doesn't have that page archived.</html>"
    )
    client = StubArchiveClient(
        {
            first_url: (200, {"content-type": "text/html"}, error_page, first_url),
            second_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                ARTICLE,
                second_url,
            ),
        }
    )
    item = ManifestItem(
        publisher="ap",
        canonical_url="https://apnews.com/article/example",
        published_at="2020-01-01T00:00:00Z",
        section="world",
        candidates=(
            candidate(first_url, "20200101000000"),
            candidate(second_url, "20200102000000"),
        ),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [first_url, second_url]
    capture = result["capture"]
    assert capture.selected_candidate.snapshot_url == second_url
    assert capture.raw_html.byte_count == len(ARTICLE)
    assert capture.quality_signals["archiveErrorPage"] is False
    with gzip.open(tmp_path / capture.raw_html.path, "rb") as handle:
        assert handle.read() == ARTICLE
    record = json.loads((tmp_path / result["recordPath"]).read_text("utf-8"))
    assert record["formatVersion"] == "jojo-raw-capture/1"
    assert record["canonicalUrl"] == item.canonical_url
    assert "plainText" not in record
    assert "bodyHtml" not in record


def test_capture_keeps_strong_article_instead_of_first_html_shell(
    tmp_path: Path,
):
    shell_url = (
        "https://web.archive.org/web/20200101000000id_/"
        "https://www.ft.com/content/example"
    )
    article_url = (
        "https://web.archive.org/web/20200102000000id_/"
        "https://amp.ft.com/content/example"
    )
    shell = (
        b"<!doctype html><html><body><p>Subscribe to read.</p></body></html>"
        + (b" " * 2_048)
    )
    client = StubArchiveClient(
        {
            shell_url: (
                200,
                {"content-type": "text/html"},
                shell,
                shell_url,
            ),
            article_url: (
                200,
                {"content-type": "text/html"},
                ARTICLE,
                article_url,
            ),
        }
    )
    item = ManifestItem(
        publisher="ft",
        canonical_url="https://www.ft.com/content/example",
        published_at="2020-01-01T00:00:00Z",
        section=None,
        candidates=(
            candidate(shell_url, "20200101000000"),
            candidate(article_url, "20200102000000"),
        ),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [
        (
            "https://web.archive.org/web/timemap/json?"
            "url=https%3A%2F%2Fwww.ft.com%2Fcontent%2Fexample"
        ),
        shell_url,
        article_url,
    ]
    capture = result["capture"]
    assert capture.selected_candidate.snapshot_url == article_url
    assert capture.quality_score == 100
    assert capture.raw_html.byte_count == len(ARTICLE)
    assert len(list((tmp_path / "objects").rglob("*.html.gz"))) == 1


def test_ft_capture_uses_exact_wayback_before_common_crawl(tmp_path: Path):
    canonical_url = "https://www.ft.com/content/example"
    timemap_url = (
        "https://web.archive.org/web/timemap/json?"
        "url=https%3A%2F%2Fwww.ft.com%2Fcontent%2Fexample"
    )
    exact_url = (
        "https://web.archive.org/web/20200101120000id_/" + canonical_url
    )
    guessed_url = (
        "https://web.archive.org/web/20200102000000id_/" + canonical_url
    )
    timemap = json.dumps(
        [
            [
                "urlkey",
                "timestamp",
                "original",
                "mimetype",
                "statuscode",
                "digest",
            ],
            [
                "com,ft)/content/example",
                "20200101120000",
                canonical_url,
                "text/html",
                "200",
                "EXACT",
            ],
        ]
    ).encode()
    client = StubArchiveClient(
        {
            timemap_url: (
                200,
                {"content-type": "application/json"},
                timemap,
                timemap_url,
            ),
            exact_url: (
                200,
                {"content-type": "text/html"},
                ARTICLE,
                exact_url,
            ),
        }
    )
    item = ManifestItem(
        publisher="ft",
        canonical_url=canonical_url,
        published_at="2020-01-01T00:00:00Z",
        section=None,
        candidates=(candidate(guessed_url, "20200102000000"),),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [timemap_url, exact_url]
    assert (
        result["capture"].selected_candidate.provider
        == CaptureProvider.WAYBACK
    )
    assert result["capture"].selected_candidate.snapshot_url == exact_url


def test_ft_capture_checks_later_exact_wayback_versions_before_common_crawl(
    tmp_path: Path,
):
    canonical_url = "https://www.ft.com/content/example"
    timemap_url = (
        "https://web.archive.org/web/timemap/json?"
        "url=https%3A%2F%2Fwww.ft.com%2Fcontent%2Fexample"
    )
    exact_urls = [
        (
            f"https://web.archive.org/web/2020010{day}120000id_/"
            f"{canonical_url}"
        )
        for day in range(1, 9)
    ]
    timemap = json.dumps(
        [
            [
                "urlkey",
                "timestamp",
                "original",
                "mimetype",
                "statuscode",
                "digest",
            ],
            *[
                [
                    "com,ft)/content/example",
                    f"2020010{day}120000",
                    canonical_url,
                    "text/html",
                    "200",
                    f"EXACT-{day}",
                ]
                for day in range(1, 9)
            ],
        ]
    ).encode()
    subscription_shell = (
        b"<!doctype html><html><body>Subscribe to read</body></html>"
        + (b" " * 2_048)
    )
    responses = {
        timemap_url: (
            200,
            {"content-type": "application/json"},
            timemap,
            timemap_url,
        ),
        **{
            url: (
                200,
                {"content-type": "text/html"},
                subscription_shell if index < 7 else ARTICLE,
                url,
            )
            for index, url in enumerate(exact_urls)
        },
    }
    client = StubArchiveClient(responses)
    item = ManifestItem(
        publisher="ft",
        canonical_url=canonical_url,
        published_at="2020-01-01T00:00:00Z",
        section=None,
        candidates=(),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [timemap_url, *exact_urls]
    assert result["capture"].selected_candidate.snapshot_url == exact_urls[-1]
    assert result["capture"].quality_score == 100


def test_bloomberg_capture_falls_back_to_exact_timemap_snapshot(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2024-01-09/"
        "white-house-example"
    )
    guessed_url = (
        "https://web.archive.org/web/20240110000000id_/" + canonical_url
    )
    exact_url = (
        "https://web.archive.org/web/20240109091704id_/" + canonical_url
    )
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.bloomberg.com%2Fnews%2Farticles%2F"
        "2024-01-09%2Fwhite-house-example"
    )
    shell = (
        b"<html><head><title>Bloomberg - Are you a robot?</title></head>"
        b"<body>We've detected unusual activity.</body></html>"
        + (b" " * 2_048)
    )
    timemap = json.dumps(
        [
            [
                "urlkey",
                "timestamp",
                "original",
                "mimetype",
                "statuscode",
                "digest",
                "length",
            ],
            [
                "com,bloomberg)/news/articles/example",
                "20240109091704",
                canonical_url,
                "text/html",
                "200",
                "EXACT-DIGEST",
                str(len(ARTICLE)),
            ],
            [
                "com,bloomberg)/news/articles/example",
                "20240110000000",
                "https://www.bloomberg.com/tosv2.html",
                "text/html",
                "200",
                "SHELL-DIGEST",
                "12345",
            ],
        ]
    ).encode()
    client = StubArchiveClient(
        {
            guessed_url: (
                200,
                {"content-type": "text/html"},
                shell,
                (
                    "https://web.archive.org/web/20240110000000id_/"
                    "https://www.bloomberg.com/tosv2.html"
                ),
            ),
            timemap_url: (
                200,
                {"content-type": "application/json"},
                timemap,
                timemap_url,
            ),
            exact_url: (
                200,
                {"content-type": "text/html"},
                ARTICLE,
                exact_url,
            ),
        }
    )
    item = ManifestItem(
        publisher="bloomberg",
        canonical_url=canonical_url,
        published_at="2024-01-09T09:00:00Z",
        section="politics",
        candidates=(candidate(guessed_url, "20240110000000"),),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [guessed_url, timemap_url, exact_url]
    capture = result["capture"]
    assert capture.selected_candidate.snapshot_url == exact_url
    assert capture.selected_candidate.digest == "EXACT-DIGEST"
    assert [value.snapshot_url for value in capture.candidates_considered] == [
        guessed_url,
        exact_url,
    ]


def test_reuters_capture_falls_back_to_exact_timemap_snapshot(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.reuters.com/business/energy/"
        "reactor-uses-fuel-2023-12-21"
    )
    guessed_url = (
        "https://web.archive.org/web/20231222000000id_/" + canonical_url
    )
    exact_url = (
        "https://web.archive.org/web/20231221121500id_/" + canonical_url
    )
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.reuters.com%2Fbusiness%2Fenergy%2F"
        "reactor-uses-fuel-2023-12-21"
    )
    timemap = json.dumps(
        [
            [
                "urlkey",
                "timestamp",
                "original",
                "mimetype",
                "statuscode",
                "digest",
                "length",
            ],
            [
                "com,reuters)/business/energy/reactor-uses-fuel",
                "20231221121500",
                canonical_url,
                "text/html",
                "200",
                "REUTERS-EXACT",
                str(len(ARTICLE)),
            ],
        ]
    ).encode()
    missing = b"<html>Wayback Machine doesn't have that page archived.</html>"
    search_url = (
        REUTERS_SYNDICATION_SEARCH_ENDPOINT
        + "?p=reactor+uses+fuel+Reuters"
    )
    client = StubArchiveClient(
        {
            guessed_url: (
                404,
                {"content-type": "text/html"},
                missing,
                guessed_url,
            ),
            search_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                b"<html><body><ol id='web'></ol></body></html>",
                search_url,
            ),
            timemap_url: (
                200,
                {"content-type": "application/json"},
                timemap,
                timemap_url,
            ),
            exact_url: (
                200,
                {"content-type": "text/html"},
                ARTICLE,
                exact_url,
            ),
        }
    )
    item = ManifestItem(
        publisher="reuters",
        canonical_url=canonical_url,
        published_at="2023-12-21T00:00:00Z",
        section="energy",
        candidates=(candidate(guessed_url, "20231222000000"),),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [
        guessed_url,
        search_url,
        timemap_url,
        exact_url,
    ]
    assert result["capture"].selected_candidate.snapshot_url == exact_url
    assert result["capture"].selected_candidate.digest == "REUTERS-EXACT"


def test_wsj_capture_falls_back_to_exact_timemap_snapshot(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.wsj.com/articles/"
        "markets-rally-on-economic-news-11673533499"
    )
    guessed_url = (
        "https://web.archive.org/web/20160102000000id_/" + canonical_url
    )
    exact_url = (
        "https://web.archive.org/web/20160101121500id_/" + canonical_url
    )
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.wsj.com%2Farticles%2F"
        "markets-rally-on-economic-news-11673533499"
    )
    timemap = json.dumps(
        [
            [
                "urlkey",
                "timestamp",
                "original",
                "mimetype",
                "statuscode",
                "digest",
                "length",
            ],
            [
                "com,wsj)/articles/markets-rally",
                "20160101121500",
                canonical_url,
                "text/html",
                "200",
                "WSJ-EXACT",
                str(len(ARTICLE)),
            ],
        ]
    ).encode()
    client = StubArchiveClient(
        {
            guessed_url: (
                404,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            timemap_url: (
                200,
                {"content-type": "application/json"},
                timemap,
                timemap_url,
            ),
            exact_url: (
                200,
                {"content-type": "text/html"},
                ARTICLE,
                exact_url,
            ),
        }
    )
    item = ManifestItem(
        publisher="wsj",
        canonical_url=canonical_url,
        published_at="2016-01-01T12:00:00Z",
        section=None,
        candidates=(candidate(guessed_url, "20160102000000"),),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [guessed_url, timemap_url, exact_url]
    assert result["capture"].selected_candidate.snapshot_url == exact_url
    assert result["capture"].selected_candidate.digest == "WSJ-EXACT"

def test_nyt_capture_uses_exact_timemap_snapshot(tmp_path: Path):
    canonical_url = (
        "https://www.nytimes.com/2025/11/24/briefing/"
        "negotiating-peace-in-ukraine.html"
    )
    guessed_url = (
        "https://web.archive.org/web/20251125000000id_/" + canonical_url
    )
    exact_url = (
        "https://web.archive.org/web/20251124122247id_/" + canonical_url
    )
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.nytimes.com%2F2025%2F11%2F24%2Fbriefing%2F"
        "negotiating-peace-in-ukraine.html"
    )
    timemap = json.dumps(
        [
            [
                "urlkey",
                "timestamp",
                "original",
                "mimetype",
                "statuscode",
                "digest",
                "length",
            ],
            [
                "com,nytimes)/2025/11/24/briefing/example.html",
                "20251124122247",
                canonical_url,
                "text/html",
                "200",
                "NYT-EXACT",
                str(len(ARTICLE)),
            ],
        ]
    ).encode()
    client = StubArchiveClient(
        {
            guessed_url: (
                404,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            timemap_url: (
                200,
                {"content-type": "application/json"},
                timemap,
                timemap_url,
            ),
            exact_url: (
                200,
                {"content-type": "text/html"},
                ARTICLE,
                exact_url,
            ),
        }
    )
    item = ManifestItem(
        publisher="nyt",
        canonical_url=canonical_url,
        published_at="2025-11-24T12:00:00Z",
        section=None,
        candidates=(candidate(guessed_url, "20251125000000"),),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [guessed_url, timemap_url, exact_url]
    assert result["capture"].selected_candidate.digest == "NYT-EXACT"


def test_nyt_capture_falls_back_to_validated_local_newspaper_copy(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.nytimes.com/2026/04/15/us/"
        "floods-michigan-cheboygan-dams-evacuation.html"
    )
    guessed_url = (
        "https://web.archive.org/web/20260416000000id_/" + canonical_url
    )
    syndicated_url = (
        "https://www.hawaiitribune-herald.com/2026/04/16/"
        "nation-world-news/dam-failure-could-imperil-thousands/"
    )
    item = ManifestItem(
        publisher="nyt",
        canonical_url=canonical_url,
        published_at="2026-04-15T16:00:00Z",
        section="us",
        candidates=(candidate(guessed_url, "20260416000000"),),
    )
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.nytimes.com%2F2026%2F04%2F15%2Fus%2F"
        "floods-michigan-cheboygan-dams-evacuation.html"
    )
    trusted_search_url = nyt_trusted_wordpress_search_url(item)
    expected_headline = (
        "Dam Failure Could Imperil Thousands in Northern Michigan"
    )
    trusted_search_json = json.dumps(
        [
            {
                "date": "2026-04-16T00:05:00",
                "date_gmt": "2026-04-16T10:05:00",
                "link": syndicated_url,
                "title": {"rendered": expected_headline},
                "content": {
                    "rendered": (
                        "<p>Licensed copy.</p>"
                        f'<p>This article originally appeared in '
                        f'<a href="{canonical_url}">The New York Times</a>.</p>'
                    )
                },
            }
        ]
    ).encode()
    paragraphs = "".join(
        (
            "<p>Emergency reporting paragraph "
            f"{index} contains substantive details about evacuations, "
            "rising rivers, public safety warnings, emergency crews and "
            "the structural condition of multiple dams in Michigan. "
            "Officials described the affected communities and the response "
            "under way while residents prepared to leave their homes.</p>"
        )
        for index in range(1, 9)
    )
    syndicated_html = f"""
    <!doctype html><html><head>
      <meta property="og:title"
            content="Dam Failure Could Imperil Thousands in Northern Michigan">
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "Dam Failure Could Imperil Thousands in Northern Michigan",
        "datePublished": "2026-04-16T00:05:00Z",
        "author": {{"name": "New York Times"}}
      }}
      </script>
    </head><body><main><div class="post-content">
      {paragraphs}
      <p>This article originally appeared in
        <a href="{canonical_url}">The New York Times</a>.
      </p>
    </div></main></body></html>
    """.encode() + (b" " * 2_048)
    empty_timemap = json.dumps(
        [["urlkey", "timestamp", "original", "mimetype", "statuscode"]]
    ).encode()
    client = StubArchiveClient(
        {
            guessed_url: (
                403,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            timemap_url: (
                200,
                {"content-type": "application/json"},
                empty_timemap,
                timemap_url,
            ),
            trusted_search_url: (
                200,
                {"content-type": "application/json; charset=utf-8"},
                trusted_search_json,
                trusted_search_url,
            ),
            syndicated_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                syndicated_html,
                syndicated_url,
            ),
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [
        guessed_url,
        timemap_url,
        trusted_search_url,
        syndicated_url,
    ]
    capture = result["capture"]
    assert capture.selected_candidate.provider == CaptureProvider.OTHER
    assert capture.final_url == syndicated_url
    assert capture.quality_signals["nytSyndicationValidated"] is True
    assert capture.quality_signals["syndicationNytAttributed"] is True
    assert (
        capture.quality_signals["syndicationCanonicalArticleLinked"]
        is True
    )
    assert (
        capture.quality_signals["syndicationBodyCharacters"]
        >= NYT_SYNDICATION_MINIMUM_BODY_CHARACTERS
    )
    assert capture.quality_signals["syndicationHeadlineOverlap"] == 1.0


def test_nyt_manifest_direct_copy_requires_exact_canonical_provenance(
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
    item = ManifestItem(
        publisher="nyt",
        canonical_url=canonical_url,
        published_at="2026-04-15T00:00:00Z",
        section="us",
        candidates=(
            CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=syndicated_url,
            ),
        ),
    )
    paragraphs = "".join(
        (
            "<p>Licensed reporting paragraph "
            f"{index} contains full details about emergency workers, "
            "evacuations, damaged roads, rising rivers and the structural "
            "condition of dams across northern Michigan. Officials described "
            "the response while residents prepared to leave their homes.</p>"
        )
        for index in range(1, 9)
    )
    content = f"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "Dam failure could imperil thousands in Northern Michigan",
        "datePublished": "2026-04-16T00:05:00Z",
        "author": {{"name": "New York Times"}}
      }}
      </script>
    </head><body><div class="post-content">
      {paragraphs}
      <p>This article originally appeared in
        <a href="{canonical_url}">The New York Times</a>.
      </p>
    </div></body></html>
    """.encode() + (b" " * 2_048)
    client = StubArchiveClient(
        {
            syndicated_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                content,
                syndicated_url,
            )
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [syndicated_url]
    capture = result["capture"]
    assert capture.quality_signals["nytSyndicationValidated"] is True
    assert (
        capture.quality_signals["syndicationCanonicalArticleLinked"]
        is True
    )


def test_nyt_capture_uses_strict_headline_wordpress_copy(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.nytimes.com/2026/04/19/arts/design/"
        "milan-design-week-revivals.html"
    )
    guessed_url = (
        "https://web.archive.org/web/20260420000000id_/" + canonical_url
    )
    item = ManifestItem(
        publisher="nyt",
        canonical_url=canonical_url,
        published_at="2026-04-19T00:00:00Z",
        section="arts",
        candidates=(candidate(guessed_url, "20260420000000"),),
    )
    headline = "Vintage Designs Take on New Lives at Milan Design Week"
    syndicated_url = (
        "https://dnyuz.com/2026/04/19/"
        "vintage-designs-take-on-new-lives-at-milan-design-week/"
    )
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.nytimes.com%2F2026%2F04%2F19%2Farts%2F"
        "design%2Fmilan-design-week-revivals.html"
    )
    trusted_search_url = nyt_trusted_wordpress_search_url(item)
    canonical_search_url = nyt_syndication_search_url(item)
    headline_wordpress_url = nyt_headline_wordpress_search_url(headline)
    canonical_search_html = f"""
    <html><body><ol id="web"><li><div class="compTitle">
      <a href="{canonical_url}"><h3>{headline} - The New York Times</h3></a>
    </div></li></ol></body></html>
    """.encode()
    headline_search_json = json.dumps(
        [
            {
                "date": "2026-04-19T10:37:50",
                "link": syndicated_url,
                "title": {"rendered": headline},
                "content": {
                    "rendered": (
                        "<p>Full licensed article body.</p>"
                        "<p>The post appeared first on "
                        '<a href="https://www.nytimes.com/">'
                        "New York Times</a>.</p>"
                    )
                },
            }
        ]
    ).encode()
    paragraphs = "".join(
        (
            "<p>Design reporting paragraph "
            f"{index} describes the makers, materials, exhibitions, "
            "historic furniture and contemporary production in enough "
            "detail to constitute a complete licensed article body. "
            "The reporting includes interviews, locations and context.</p>"
        )
        for index in range(1, 9)
    )
    syndicated_html = f"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "{headline}",
        "datePublished": "2026-04-19T10:37:50Z",
        "author": {{"name": "New York Times"}}
      }}
      </script>
    </head><body><main><div class="post-content">
      {paragraphs}
      <p>The post appeared first on
        <a href="https://www.nytimes.com/">New York Times</a>.
      </p>
    </div></main></body></html>
    """.encode() + (b" " * 2_048)
    empty_timemap = json.dumps(
        [["urlkey", "timestamp", "original", "mimetype", "statuscode"]]
    ).encode()
    client = StubArchiveClient(
        {
            guessed_url: (
                403,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            timemap_url: (
                200,
                {"content-type": "application/json"},
                empty_timemap,
                timemap_url,
            ),
            trusted_search_url: (
                200,
                {"content-type": "application/json"},
                b"[]",
                trusted_search_url,
            ),
            canonical_search_url: (
                200,
                {"content-type": "text/html"},
                canonical_search_html,
                canonical_search_url,
            ),
            headline_wordpress_url: (
                200,
                {"content-type": "application/json"},
                headline_search_json,
                headline_wordpress_url,
            ),
            syndicated_url: (
                200,
                {"content-type": "text/html"},
                syndicated_html,
                syndicated_url,
            ),
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [
        guessed_url,
        timemap_url,
        trusted_search_url,
        canonical_search_url,
        headline_wordpress_url,
        syndicated_url,
    ]
    capture = result["capture"]
    assert capture.selected_candidate.snapshot_url == syndicated_url
    assert capture.quality_signals["nytSyndicationValidated"] is True
    assert (
        capture.quality_signals["syndicationCanonicalArticleLinked"]
        is False
    )


def test_nyt_syndication_rejects_unattributed_same_topic_article(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.nytimes.com/2026/04/15/us/"
        "floods-michigan-cheboygan-dams-evacuation.html"
    )
    guessed_url = (
        "https://web.archive.org/web/20260416000000id_/" + canonical_url
    )
    related_url = "https://example.com/michigan-dam-emergency"
    item = ManifestItem(
        publisher="nyt",
        canonical_url=canonical_url,
        published_at="2026-04-15T16:00:00Z",
        section="us",
        candidates=(candidate(guessed_url, "20260416000000"),),
    )
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.nytimes.com%2F2026%2F04%2F15%2Fus%2F"
        "floods-michigan-cheboygan-dams-evacuation.html"
    )
    headline = "Dam Failure Could Imperil Thousands in Northern Michigan"
    search_url = nyt_syndication_search_url(item)
    title_search_url = nyt_syndication_title_search_url(headline)
    trusted_search_url = nyt_trusted_wordpress_search_url(item)
    search_html = f"""
    <html><body><ol id="web"><li><div class="compTitle">
      <a href="{canonical_url}"><h3>
        {headline} - The New York Times
      </h3></a>
    </div></li></ol></body></html>
    """.encode()
    title_search_html = f"""
    <html><body><ol id="web"><li><div class="compTitle">
      <a href="{related_url}"><h3>{headline}</h3></a>
    </div></li></ol></body></html>
    """.encode()
    paragraphs = "".join(
        (
            "<p>Independent reporting paragraph "
            f"{index} discusses the same emergency and contains enough "
            "substantive material to exceed the body threshold, but this "
            "copy has no source-service attribution and must be rejected. "
            "It includes descriptions of warnings, emergency crews, roads, "
            "weather conditions and residents leaving nearby homes.</p>"
        )
        for index in range(1, 9)
    )
    related_html = f"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "{headline}",
        "datePublished": "2026-04-16T00:05:00Z",
        "author": {{"name": "Independent Local Reporter"}}
      }}
      </script>
    </head><body><div class="post-content">
      {paragraphs}
    </div></body></html>
    """.encode() + (b" " * 2_048)
    empty_timemap = json.dumps(
        [["urlkey", "timestamp", "original", "mimetype", "statuscode"]]
    ).encode()
    client = StubArchiveClient(
        {
            guessed_url: (
                403,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            timemap_url: (
                200,
                {"content-type": "application/json"},
                empty_timemap,
                timemap_url,
            ),
            trusted_search_url: (
                200,
                {"content-type": "application/json"},
                b"[]",
                trusted_search_url,
            ),
            search_url: (
                200,
                {"content-type": "text/html"},
                search_html,
                search_url,
            ),
            title_search_url: (
                200,
                {"content-type": "text/html"},
                title_search_html,
                title_search_url,
            ),
            related_url: (
                200,
                {"content-type": "text/html"},
                related_html,
                related_url,
            ),
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "error"
    assert "missing-nyt-attribution" in result["error"]
    assert not (tmp_path / "objects").exists()


def test_unsupported_publisher_does_not_query_wayback_timemap(
    tmp_path: Path,
):
    canonical_url = "https://apnews.com/article/example"
    guessed_url = (
        "https://web.archive.org/web/20240110000000id_/" + canonical_url
    )
    client = StubArchiveClient(
        {
            guessed_url: (
                404,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
        }
    )
    item = ManifestItem(
        publisher="ap",
        canonical_url=canonical_url,
        published_at="2024-01-09T09:00:00Z",
        section=None,
        candidates=(candidate(guessed_url, "20240110000000"),),
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "error"
    assert client.requests == [guessed_url]


def test_reuters_capture_falls_back_to_validated_syndicated_html(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.reuters.com/business/autos-transportation/"
        "boeing-justice-department-seek-judges-approval-"
        "deal-opposed-by-crash-victims-2025-07-03"
    )
    guessed_url = (
        "https://web.archive.org/web/20250704000000id_/"
        + canonical_url
    )
    syndicated_url = (
        "https://www.yahoo.com/news/"
        "boeing-justice-department-seek-judges-035416509.html"
    )
    item = ManifestItem(
        publisher="reuters",
        canonical_url=canonical_url,
        published_at="2025-07-03T00:00:00Z",
        section="business",
        candidates=(candidate(guessed_url, "20250704000000"),),
    )
    search_url = reuters_syndication_search_url(item)
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.reuters.com%2Fbusiness%2F"
        "autos-transportation%2Fboeing-justice-department-seek-"
        "judges-approval-deal-opposed-by-crash-victims-2025-07-03"
    )
    assert search_url.startswith(REUTERS_SYNDICATION_SEARCH_ENDPOINT)
    search_html = f"""
    <html><body><ol id="web"><li><h3>
      <a href="{syndicated_url}">Syndicated copy</a>
    </h3></li></ol></body></html>
    """.encode()
    syndicated_html = b"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "Boeing and Justice Department seek judge's approval for deal opposed by crash victims' families",
        "datePublished": "2025-07-03T03:54:16Z",
        "author": {"name": "Reuters"}
      }
      </script>
    </head><body><article>
      <p>By David Shepardson</p>
      <p>(Reuters) - Boeing and the Justice Department asked a U.S. judge
      to approve an agreement concerning the 737 MAX case. This paragraph
      contains enough substantive reporting to identify the syndicated wire
      article and distinguish it from a search result, abstract, or shell.</p>
      <p>The agreement includes compensation for victims' families and other
      obligations. The report continues with court arguments, procedural
      history, financial terms, and responses from the parties so that the
      captured body is long enough for full-article validation.</p>
      <p>Additional reporting explains the earlier plea agreement, the two
      crashes, regulatory findings, and the positions taken by relatives.
      This is retained as the raw HTML supplied by the syndication host.</p>
    </article></body></html>
    """ + (b" " * 2_048)
    client = StubArchiveClient(
        {
            guessed_url: (
                401,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            timemap_url: (
                200,
                {"content-type": "application/json"},
                json.dumps(
                    [[
                        "urlkey",
                        "timestamp",
                        "original",
                        "mimetype",
                        "statuscode",
                    ]]
                ).encode(),
                timemap_url,
            ),
            search_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                search_html,
                search_url,
            ),
            syndicated_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                syndicated_html,
                syndicated_url,
            ),
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests == [
        guessed_url,
        search_url,
        syndicated_url,
    ]
    capture = result["capture"]
    assert capture.selected_candidate.provider == CaptureProvider.OTHER
    assert capture.selected_candidate.snapshot_url == syndicated_url
    assert capture.final_url == syndicated_url
    assert capture.quality_signals["reutersSyndicationValidated"] is True
    assert capture.quality_signals["syndicationReutersAttributed"] is True
    assert capture.quality_signals["syndicationBodyCharacters"] >= 400


def test_reuters_syndication_rejects_unattributed_related_article(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.reuters.com/world/example-related-story-2025-07-03"
    )
    guessed_url = (
        "https://web.archive.org/web/20250704000000id_/"
        + canonical_url
    )
    related_url = "https://example.com/example-related-story"
    item = ManifestItem(
        publisher="reuters",
        canonical_url=canonical_url,
        published_at="2025-07-03T00:00:00Z",
        section="world",
        candidates=(candidate(guessed_url, "20250704000000"),),
    )
    search_url = reuters_syndication_search_url(item)
    search_html = f"""
    <html><body><ol id="web"><li><h3>
      <a href="{related_url}">Related article</a>
    </h3></li></ol></body></html>
    """.encode()
    related_html = b"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "Example related story",
        "datePublished": "2025-07-03T03:00:00Z",
        "author": {"name": "Another Publisher"}
      }
      </script>
    </head><body><article>
      <p>This independently written report discusses a similar subject but
      does not carry the wire service byline or attribution required by the
      archive. It has enough text to pass a naive length-only article check.</p>
      <p>More unrelated reporting is included here to ensure the rejection is
      caused by provenance validation rather than by a short-body threshold.
      The candidate must never be stored as if it were the original wire.</p>
      <p>A final paragraph adds context and length while still omitting any
      reference to the source whose canonical URL is being archived.</p>
    </article></body></html>
    """ + (b" " * 2_048)
    client = StubArchiveClient(
        {
            guessed_url: (
                401,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            search_url: (
                200,
                {"content-type": "text/html"},
                search_html,
                search_url,
            ),
            related_url: (
                200,
                {"content-type": "text/html"},
                related_html,
                related_url,
            ),
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "error"
    assert "missing-reuters-attribution" in result["error"]
    assert not (tmp_path / "objects").exists()


def test_bloomberg_capture_falls_back_to_validated_syndicated_html(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2024-06-03/"
        "tories-fail-to-dent-labour-polling-lead-in-early-uk-campaign"
    )
    guessed_url = (
        "https://web.archive.org/web/20240604000000id_/"
        + canonical_url
    )
    syndicated_url = (
        "https://www.yahoo.com/news/"
        "tories-fail-to-dent-labour-polling-lead-040000123.html"
    )
    item = ManifestItem(
        publisher="bloomberg",
        canonical_url=canonical_url,
        published_at="2024-06-03T00:00:00Z",
        section="politics",
        candidates=(candidate(guessed_url, "20240604000000"),),
    )
    search_url = bloomberg_syndication_search_url(item)
    search_html = f"""
    <html><body><ol id="web"><li><h3>
      <a href="{syndicated_url}">Licensed syndicated copy</a>
    </h3></li></ol></body></html>
    """.encode()
    syndicated_html = b"""
    <!doctype html><html><head>
      <meta property="og:site_name" content="Yahoo News">
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "Tories Fail to Dent Labour Polling Lead in Early UK Campaign",
        "datePublished": "2024-06-03T04:00:00Z",
        "author": {"name": "Bloomberg News"}
      }
      </script>
    </head><body><article>
      <div class="article-content">
        <p>Bloomberg News reports that the governing party failed to narrow
        the opposition's polling lead during the opening stage of the UK
        election campaign. The survey provides a detailed national snapshot
        and identifies the issues shaping voter decisions.</p>
        <p>The report explains changes among several demographic groups,
        compares the findings with earlier polling, and includes responses
        from campaign officials. This supplies substantive reporting rather
        than a headline, search snippet, or related-story card.</p>
        <p>Additional analysis covers the parties' economic messages,
        leadership ratings, constituency strategy, and the timetable before
        voting day. The complete licensed copy is retained as raw HTML for
        reproducible parser validation.</p>
      </div>
    </article></body></html>
    """ + (b" " * 2_048)
    client = StubArchiveClient(
        {
            guessed_url: (
                401,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            search_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                search_html,
                search_url,
            ),
            syndicated_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                syndicated_html,
                syndicated_url,
            ),
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "complete"
    assert client.requests[0] == guessed_url
    assert client.requests[1].startswith(WAYBACK_TIMEMAP_ENDPOINT)
    assert client.requests[-2:] == [search_url, syndicated_url]
    capture = result["capture"]
    assert capture.selected_candidate.provider == CaptureProvider.OTHER
    assert capture.final_url == syndicated_url
    assert capture.quality_signals["bloombergSyndicationValidated"] is True
    assert capture.quality_signals["syndicationBloombergAttributed"] is True
    assert (
        capture.quality_signals["syndicationBodyCharacters"]
        >= BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS
    )


def test_bloomberg_syndication_rejects_unattributed_related_article(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2024-06-03/"
        "tories-fail-to-dent-labour-polling-lead-in-early-uk-campaign"
    )
    guessed_url = (
        "https://web.archive.org/web/20240604000000id_/"
        + canonical_url
    )
    related_url = "https://example.com/tories-fail-to-dent-labour-lead"
    item = ManifestItem(
        publisher="bloomberg",
        canonical_url=canonical_url,
        published_at="2024-06-03T00:00:00Z",
        section="politics",
        candidates=(candidate(guessed_url, "20240604000000"),),
    )
    search_url = bloomberg_syndication_search_url(item)
    search_html = f"""
    <html><body><ol id="web"><li><h3>
      <a href="{related_url}">Related article</a>
    </h3></li></ol></body></html>
    """.encode()
    related_html = b"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "Tories Fail to Dent Labour Polling Lead in Early UK Campaign",
        "datePublished": "2024-06-03T04:00:00Z",
        "author": {"name": "Another Publisher"}
      }
      </script>
    </head><body><article>
      <p>An independently written article may have the same headline and
      publication date, but it lacks the required source attribution. The
      validator must not silently treat it as the canonical publisher copy.</p>
      <p>More unrelated reporting is included to exceed the minimum body
      length and prove that provenance, rather than a naive length check,
      causes this candidate to be rejected by the archive.</p>
      <p>A final paragraph adds enough detail for a complete parser result
      while deliberately omitting any reference to the canonical publisher
      or its news service.</p>
    </article></body></html>
    """ + (b" " * 2_048)
    client = StubArchiveClient(
        {
            guessed_url: (
                401,
                {"content-type": "text/html"},
                b"",
                guessed_url,
            ),
            search_url: (
                200,
                {"content-type": "text/html"},
                search_html,
                search_url,
            ),
            related_url: (
                200,
                {"content-type": "text/html"},
                related_html,
                related_url,
            ),
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )

    assert result["status"] == "error"
    assert "missing-bloomberg-attribution" in result["error"]
    assert not (tmp_path / "objects").exists()


def test_capture_state_records_result_and_summary(tmp_path: Path):
    url = "https://web.archive.org/web/20200102000000id_/https://example.com/a"
    item = ManifestItem(
        publisher="ft",
        canonical_url="https://www.ft.com/content/example",
        published_at="2020-01-01T00:00:00Z",
        section=None,
        candidates=(candidate(url, "20200102000000"),),
    )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "publisher": "ft",
                "canonical_url": item.canonical_url,
                "published_at": item.published_at,
                "candidates": [
                    value.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for value in item.candidates
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="ft",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="ft",
    )
    selected = pending_captures(
        connection,
        retry_errors=False,
        maximum=1,
        maximum_record_attempts=3,
    )[0]
    mark_capture_downloading(connection, selected)
    result = capture_item(
        selected,
        archive_client=StubArchiveClient(
            {url: (200, {"content-type": "text/html"}, ARTICLE, url)}
        ),
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
    )
    record_capture_result(connection, result)

    row = connection.execute(
        "SELECT status, attempts, raw_sha256, record_path FROM captures"
    ).fetchone()
    summary = capture_summary(connection, output_dir=tmp_path)

    assert row[0:2] == ("complete", 1)
    assert len(row[2]) == 64
    assert row[3].endswith(".json")
    assert summary["capturesByStatus"] == {"complete": 1}
    assert summary["rawHtmlBytes"] == len(ARTICLE)
    assert summary["objectsOnDisk"] == 1
    assert summary["recordsOnDisk"] == 1


def test_content_addressed_storage_is_deterministic(tmp_path: Path):
    first = store_raw_html(tmp_path, ARTICLE)
    second = store_raw_html(tmp_path, ARTICLE)

    assert first == second
    assert first.content_encoding == "gzip"
    assert len(list((tmp_path / "objects").rglob("*.html.gz"))) == 1


def test_raw_quality_rejects_archive_error_page():
    score, signals = score_raw_capture(
        b"<html>Wayback Machine doesn't have that page archived.</html>",
        http_status=200,
        content_type="text/html",
    )

    assert score < 100
    assert signals["looksLikeHtml"] is True
    assert signals["archiveErrorPage"] is True


def test_raw_quality_rejects_authentication_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Log In - The New York Times</title></head>
        <body><p>Log in to continue.</p></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
        final_url="https://myaccount.nytimes.com/auth/login?URI=article",
    )

    assert score < 85
    assert signals["authenticationShell"] is True


def test_raw_quality_rejects_access_challenge_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Bloomberg - Are you a robot?</title></head>
        <body><p>We've detected unusual activity from your network.</p></body>
        </html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["accessChallengeShell"] is True


def test_raw_quality_rejects_email_login_redirect_with_empty_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>The New York Times</title></head>
        <body></body></html>
        """ + (b" " * 20_000),
        http_status=200,
        content_type="text/html",
        final_url=(
            "https://myaccount.nytimes.com/auth/enter-email"
            "?response_type=cookie"
        ),
    )

    assert score < 85
    assert signals["authenticationShell"] is True


def test_raw_quality_rejects_nyt_lire_shell_without_login_title():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>The New York Times</title>
        <meta name="sourceApp" content="nyt-lire"></head>
        <body><div id="myAccountAuth" class="full-page"></div>
        <script src="/lire_ui/js/unified-lire.bundle.js"></script></body>
        </html>
        """ + (b" " * 20_000),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["authenticationShell"] is True


def test_raw_quality_rejects_client_challenge_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Client Challenge</title></head>
        <body><p>JavaScript is disabled in your browser. A required part of
        this site couldn't load.</p></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["accessChallengeShell"] is True


def test_raw_quality_rejects_ft_zephr_barrier_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Article headline</title></head>
        <body><div id="barrier-page">
        <h1>Article headline</h1>
        <span>Subscribe to unlock this article</span>
        </div>
        <script>
        window.Zephr.outcomes['paywall'] = {
          featureLabel: 'Paywall'
        };
        </script></body></html>
        """ + (b" " * 220_000),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["subscriptionShell"] is True


def test_raw_quality_rejects_ft_legacy_subscription_landing_pages():
    for title in (
        "Become an FT subscriber to read | Financial Times",
        "Subscribe to a slice of the FT | Financial Times",
    ):
        score, signals = score_raw_capture(
            (
                f"""
                <html><head><title>{title}</title>
                <script type="application/ld+json">
                {{"@type":"NewsArticle","headline":"Navigation teaser"}}
                </script></head>
                <body><main>Choose a subscription to continue.</main></body>
                </html>
                """
            ).encode()
            + (b" " * 90_000),
            http_status=200,
            content_type="text/html",
        )

        assert score < 85
        assert signals["hasArticleMarker"] is True
        assert signals["hasStrongBodyMarker"] is False
        assert signals["subscriptionShell"] is True


def test_raw_quality_rejects_ft_professional_access_error_redirect():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Monetary Policy Radar | Financial Times</title>
        </head><body></body></html>
        """ + (b" " * 90_000),
        http_status=200,
        content_type="text/html",
        final_url=(
            "https://web.archive.org/web/20260223182234id_/"
            "https://professional-monetary-policy-radar.ft.com/"
            "access-error/example"
        ),
    )

    assert score < 85
    assert signals["accessChallengeShell"] is True


def test_raw_quality_rejects_truncated_ft_article_body():
    score, signals = score_raw_capture(
        b"""
        <html><head><script type="application/ld+json">
        {"@type":"NewsArticle","headline":"Archived FT article",
         "articleBody":"Only a short archived teaser survives here."}
        </script></head><body><article>
        <div class="article__content-body">
          <p>Only a short archived teaser survives here.</p>
        </div></article></body></html>
        """ + (b" " * 90_000),
        http_status=200,
        content_type="text/html",
        final_url=(
            "https://web.archive.org/web/20180901000000id_/"
            "https://www.ft.com/content/example"
        ),
    )

    assert score < 85
    assert signals["ftTruncatedArticleShell"] is True
    assert signals["ftBodyCharacters"] < 100


def test_raw_quality_keeps_image_led_ft_data_story():
    images = b"".join(
        b"<figure><img src='https://www.ft.com/image.png'></figure>"
        for _ in range(3)
    )
    score, signals = score_raw_capture(
        (
            b"<html><head><script type='application/ld+json'>"
            b'{"@type":"NewsArticle","headline":"FT data story"}'
            b"</script></head><body><article>"
            b"<div class='article__content-body'>"
            + images
            + b"</div></article></body></html>"
            + (b" " * 90_000)
        ),
        http_status=200,
        content_type="text/html",
        final_url=(
            "https://web.archive.org/web/20181001000000id_/"
            "https://www.ft.com/content/image-led"
        ),
    )

    assert score >= 85
    assert signals["ftTruncatedArticleShell"] is False
    assert signals["ftBodyImages"] == 3


def test_raw_quality_rejects_javascript_redirect_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Archived interactive</title></head>
        <body><article><div class="interactive-graphic"><script>
        var destUrl = "https://www.nytimes.com/guides/privacy";
        window.location = fullUrl;
        </script></div></article></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["redirectShell"] is True


def test_stored_challenge_shell_is_requeued_by_current_policy(
    tmp_path: Path,
):
    canonical_url = "https://www.ft.com/content/example"
    snapshot_url = (
        "https://web.archive.org/web/20240101000000id_/" + canonical_url
    )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "publisher": "ft",
                "canonicalUrl": canonical_url,
                "publishedAt": "2024-01-01T00:00:00Z",
                "candidates": [
                    {
                        "provider": "wayback",
                        "snapshotUrl": snapshot_url,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    connection = sqlite3.connect(":memory:")
    initialize_capture_schema(
        connection,
        publisher="ft",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        connection,
        manifest_path=manifest,
        publisher="ft",
    )
    shell = (
        b"<html><head><title>Client Challenge</title></head>"
        b"<body>JavaScript is disabled in your browser.</body></html>"
    )
    blob = store_raw_html(tmp_path, shell)
    capture = RawCapture(
        article_id="ft:" + ("a" * 64),
        publisher="ft",
        canonical_url=canonical_url,
        published_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        selected_candidate=CaptureCandidate(
            provider=CaptureProvider.WAYBACK,
            snapshot_url=snapshot_url,
        ),
        candidates_considered=[],
        retrieved_at=datetime.now(timezone.utc),
        final_url=snapshot_url,
        http_status=200,
        content_type="text/html",
        quality_score=85,
        raw_html=blob,
    )
    record_capture_result(
        connection,
        {
            "canonicalUrl": canonical_url,
            "status": "complete",
            "capture": capture,
            "recordPath": "records/example.json",
            "error": None,
        },
    )

    reason = completed_capture_rejection_reason(
        capture,
        archive_root=tmp_path,
    )
    reset_completed_capture_for_retry(
        connection,
        canonical_url=canonical_url,
        reason=str(reason),
    )
    row = connection.execute(
        "SELECT status, attempts, last_error FROM captures"
    ).fetchone()

    assert reason == "access-challenge-shell"
    assert row[0:2] == ("pending", 0)
    assert "access-challenge-shell" in row[2]


def test_raw_quality_rejects_subscription_shell_without_article_body():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Subscribe to read | Financial Times</title></head>
        <body><article><p>Discover all the plans currently available in your
        country</p></article></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["subscriptionShell"] is True


def test_raw_quality_rejects_wsj_continue_reading_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>A WSJ article</title></head>
        <body><article><p>A short preview sentence.</p>
        <div>Continue reading your article with a
        <strong>WSJ subscription</strong></div>
        <a>Already a subscriber?</a></article></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["subscriptionShell"] is True


def test_raw_quality_rejects_wsj_structured_snippet_view():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>A WSJ article</title>
        <script type="application/json">
        {"isSnippetView":true,"articleBodySchema":[{"@type":"ImageObject"}]}
        </script></head>
        <body><article data-testid="article-body">
        <p>The first two paragraphs are visible but the rest is omitted.</p>
        </article></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["hasStrongBodyMarker"] is True
    assert signals["subscriptionShell"] is True


def test_raw_quality_rejects_bloomberg_login_to_keep_reading_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>A Bloomberg article preview</title></head>
        <body><article><p>Several preview paragraphs are shown here.</p>
        <h2>Already a subscriber?</h2>
        <p>Log in to keep reading or access research tools and resources.</p>
        </article></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["subscriptionShell"] is True


def test_raw_quality_keeps_subscription_page_with_structured_article_body():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Subscribe to read | Financial Times</title>
        <script type="application/ld+json">
        {"@type":"NewsArticle","articleBody":"Full archived article body."}
        </script></head><body></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score >= 85
    assert signals["subscriptionShell"] is False


def test_raw_quality_keeps_article_body_with_subscription_footer():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Archived Financial Times article</title></head>
        <body><div class="article__content-body"><p>Full article text.</p></div>
        <footer>During your trial you will have complete digital access to
        FT.com.</footer></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score >= 85
    assert signals["subscriptionShell"] is False


def test_wayback_candidate_records_actual_redirected_snapshot():
    requested = candidate(
        (
            "https://web.archive.org/web/20200115000000id_/"
            "https://example.com/article"
        ),
        "20200115000000",
    )
    resolved = resolved_capture_candidate(
        requested,
        final_url=(
            "https://web.archive.org/web/20200114235538id_/"
            "https://example.com/article"
        ),
        http_status=200,
        content_type="text/html",
        byte_count=1234,
    )

    assert resolved.snapshot_url.startswith(
        "https://web.archive.org/web/20200114235538id_/"
    )
    assert resolved.captured_at == datetime(
        2020,
        1,
        14,
        23,
        55,
        38,
        tzinfo=timezone.utc,
    )
    assert resolved.byte_count == 1234
