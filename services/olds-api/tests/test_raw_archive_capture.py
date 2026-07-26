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
    CAPTURE_POLICY_VERSIONS,
    FT_SYNDICATION_MINIMUM_BODY_CHARACTERS,
    ManifestItem,
    NYT_SYNDICATION_MINIMUM_BODY_CHARACTERS,
    NYT_SYNDICATION_SEARCH_ENDPOINT,
    REUTERS_SYNDICATION_SEARCH_ENDPOINT,
    WAYBACK_TIMEMAP_ENDPOINT,
    WSJ_SYNDICATION_MINIMUM_BODY_CHARACTERS,
    arquivo_pt_cdx_url,
    bloomberg_syndication_search_url,
    capture_item,
    capture_summary,
    completed_capture_rejection_reason,
    discover_arquivo_pt_candidates,
    discover_ft_syndication_candidates,
    discover_reuters_syndication_candidates,
    ft_google_news_headline_search_url,
    ft_google_news_partner_search_url,
    ft_syndication_broad_title_search_url,
    ft_syndication_partner_site_search_url,
    ft_syndication_search_url,
    ft_syndication_title_search_url,
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
    reuters_syndication_title_search_url,
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


def test_capture_policy_upgrade_retries_errors_once(tmp_path: Path):
    canonical_url = "https://www.ft.com/content/policy-upgrade"
    snapshot_url = (
        "https://web.archive.org/web/20260102000000id_/"
        + canonical_url
    )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "publisher": "ft",
                "canonicalUrl": canonical_url,
                "publishedAt": "2026-01-01T00:00:00Z",
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
    connection.execute(
        """
        UPDATE archive_metadata
        SET value='ft-capture/old'
        WHERE key='capture_policy_version'
        """
    )
    connection.execute(
        """
        UPDATE captures
        SET status='error',
            attempts=3,
            last_error='old policy exhausted'
        """
    )
    connection.commit()

    initialize_capture_schema(
        connection,
        publisher="ft",
        authorization_reference="authorization:test",
    )
    first = connection.execute(
        "SELECT status, attempts, last_error FROM captures"
    ).fetchone()
    stored_version = connection.execute(
        """
        SELECT value
        FROM archive_metadata
        WHERE key='capture_policy_version'
        """
    ).fetchone()[0]
    connection.execute(
        """
        UPDATE captures
        SET status='error',
            attempts=2,
            last_error='current policy failure'
        """
    )
    connection.commit()

    initialize_capture_schema(
        connection,
        publisher="ft",
        authorization_reference="authorization:test",
    )
    second = connection.execute(
        "SELECT status, attempts, last_error FROM captures"
    ).fetchone()

    assert first == ("pending", 0, None)
    assert stored_version == CAPTURE_POLICY_VERSIONS["ft"]
    assert second == ("error", 2, "current policy failure")


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


def test_arquivo_pt_candidates_are_exact_deduplicated_and_ranked():
    canonical_url = "https://www.ft.com/content/example"
    item = ManifestItem(
        publisher="ft",
        canonical_url=canonical_url,
        published_at="2020-01-01T00:00:00Z",
        section=None,
        candidates=(),
    )
    query_url = arquivo_pt_cdx_url(item)
    rows = [
        {
            "url": canonical_url,
            "timestamp": "20200201000000",
            "mime": "text/html",
            "status": "200",
            "digest": "FAR",
            "length": "30000",
        },
        {
            "url": canonical_url,
            "timestamp": "20200102000000",
            "mime": "text/html",
            "status": "200",
            "digest": "NEAR",
            "length": "25000",
        },
        {
            "url": canonical_url,
            "timestamp": "20200103000000",
            "mime": "text/html",
            "status": "200",
            "digest": "NEAR",
            "length": "26000",
        },
        {
            "url": "https://www.ft.com/content/different",
            "timestamp": "20200101000000",
            "mime": "text/html",
            "status": "200",
            "digest": "WRONG",
            "length": "27000",
        },
    ]
    payload = "\n".join(json.dumps(row) for row in rows).encode()
    client = StubArchiveClient(
        {
            query_url: (
                200,
                {"content-type": "text/x-ndjson"},
                payload,
                query_url,
            )
        }
    )

    candidates = discover_arquivo_pt_candidates(
        item,
        archive_client=client,
    )

    assert client.requests == [query_url]
    assert [candidate.digest for candidate in candidates] == ["NEAR", "FAR"]
    assert all(
        candidate.provider == CaptureProvider.ARQUIVO_PT
        for candidate in candidates
    )
    assert candidates[0].snapshot_url == (
        "https://arquivo.pt/noFrame/replay/20200102000000/"
        + canonical_url
    )


def test_ft_capture_falls_back_to_validated_arquivo_pt_replay(
    tmp_path: Path,
):
    canonical_url = "https://www.ft.com/content/example"
    item = ManifestItem(
        publisher="ft",
        canonical_url=canonical_url,
        published_at="2020-01-01T00:00:00Z",
        section=None,
        candidates=(),
    )
    timemap_url = (
        "https://web.archive.org/web/timemap/json?"
        "url=https%3A%2F%2Fwww.ft.com%2Fcontent%2Fexample"
    )
    timemap = json.dumps(
        [["urlkey", "timestamp", "original", "mimetype", "statuscode"]]
    ).encode()
    query_url = arquivo_pt_cdx_url(item)
    replay_url = (
        "https://arquivo.pt/noFrame/replay/20210208222255/"
        + canonical_url
    )
    index = json.dumps(
        {
            "url": canonical_url,
            "timestamp": "20210208222255",
            "mime": "text/html",
            "status": "200",
            "digest": "ARQUIVO",
            "length": "28508",
        }
    ).encode()
    client = StubArchiveClient(
        {
            timemap_url: (
                200,
                {"content-type": "application/json"},
                timemap,
                timemap_url,
            ),
            query_url: (
                200,
                {"content-type": "text/x-ndjson"},
                index,
                query_url,
            ),
            replay_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                ARTICLE,
                replay_url,
            ),
        }
    )

    result = capture_item(
        item,
        archive_client=client,
        output_dir=tmp_path,
        maximum_html_bytes=1_000_000,
        enable_arquivo_pt_fallback=True,
    )

    assert result["status"] == "complete"
    assert client.requests == [timemap_url, query_url, replay_url]
    capture = result["capture"]
    assert capture.selected_candidate.provider == CaptureProvider.ARQUIVO_PT
    assert capture.final_url == replay_url
    assert capture.quality_signals["arquivoPtReplayValidated"] is True


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


def test_reuters_syndication_uses_exact_title_second_search():
    canonical_url = (
        "https://www.reuters.com/business/autos-transportation/"
        "auto-industry-rocked-by-trumps-25-tariffs-us-imports-"
        "2025-03-27"
    )
    item = ManifestItem(
        publisher="reuters",
        canonical_url=canonical_url,
        published_at="2025-03-27T00:00:00Z",
        section="business",
        candidates=(),
    )
    expected_headline = (
        "Auto industry rocked by Trump's 25% tariffs on US imports"
    )
    initial_url = reuters_syndication_search_url(item)
    title_url = reuters_syndication_title_search_url(expected_headline)
    radio_url = (
        "https://www.933thedrive.com/2025/03/27/"
        "auto-industry-rocked-by-trumps-25-tariffs-on-us-imports/"
    )
    initial_html = f"""
    <html><body><ol id="web">
      <li><h3><a href="{canonical_url}">
        {expected_headline} - Reuters
      </a></h3></li>
      <li><h3><a href="https://example.com/different-story">
        A different story
      </a></h3></li>
    </ol></body></html>
    """.encode()
    title_html = f"""
    <html><body><ol id="web">
      <li><h3><a href="{radio_url}">
        {expected_headline} | Reuters
      </a></h3></li>
      <li><h3><a href="https://example.net/unrelated">
        Unrelated market report
      </a></h3></li>
    </ol></body></html>
    """.encode()
    client = StubArchiveClient(
        {
            initial_url: (
                200,
                {"content-type": "text/html"},
                initial_html,
                initial_url,
            ),
            title_url: (
                200,
                {"content-type": "text/html"},
                title_html,
                title_url,
            ),
        }
    )

    candidates = discover_reuters_syndication_candidates(
        item,
        archive_client=client,
    )

    assert client.requests == [initial_url, title_url]
    assert [candidate.snapshot_url for candidate in candidates] == [
        radio_url
    ]
    assert candidates[0].provider == CaptureProvider.OTHER
    assert candidates[0].expected_headline == expected_headline


def test_reuters_syndication_keeps_initial_results_when_title_search_fails():
    canonical_url = (
        "https://www.reuters.com/world/example-reuters-story-2025-03-27"
    )
    partner_url = "https://example.com/example-reuters-story"
    expected_headline = "Example Reuters story has a sufficiently long title"
    item = ManifestItem(
        publisher="reuters",
        canonical_url=canonical_url,
        published_at="2025-03-27T00:00:00Z",
        section="world",
        candidates=(),
    )
    initial_url = reuters_syndication_search_url(item)
    title_url = reuters_syndication_title_search_url(expected_headline)
    initial_html = f"""
    <html><body><ol id="web">
      <li><h3><a href="{canonical_url}">
        {expected_headline} - Reuters
      </a></h3></li>
      <li><h3><a href="{partner_url}">
        {expected_headline}
      </a></h3></li>
    </ol></body></html>
    """.encode()
    client = StubArchiveClient(
        {
            initial_url: (
                200,
                {"content-type": "text/html"},
                initial_html,
                initial_url,
            ),
            title_url: (
                503,
                {"content-type": "text/html"},
                b"",
                title_url,
            ),
        }
    )

    candidates = discover_reuters_syndication_candidates(
        item,
        archive_client=client,
    )

    assert client.requests == [initial_url, title_url]
    assert [candidate.snapshot_url for candidate in candidates] == [
        partner_url
    ]


def test_ft_capture_uses_paywall_metadata_to_find_validated_partner(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.ft.com/content/"
        "d8f6d8af-8235-43ae-a946-6d51da973ca4"
    )
    snapshot_url = (
        "https://web.archive.org/web/20260308005629id_/"
        + canonical_url
    )
    expected_headline = (
        "Rachel Reeves sticks to stability in face of Iran war "
        "and restive Labour MPs"
    )
    partner_url = (
        "https://example.com/2026/03/03/"
        "rachel-reeves-sticks-to-stability/"
    )
    item = ManifestItem(
        publisher="ft",
        canonical_url=canonical_url,
        published_at="2026-03-03T16:04:04.200Z",
        section="uk",
        candidates=(
            CaptureCandidate(
                provider=CaptureProvider.WAYBACK,
                snapshot_url=snapshot_url,
            ),
        ),
    )
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?url=" + (
        "https%3A%2F%2Fwww.ft.com%2Fcontent%2F"
        "d8f6d8af-8235-43ae-a946-6d51da973ca4"
    )
    title_search_url = ft_syndication_title_search_url(
        expected_headline
    )
    paywall_html = f"""
    <!doctype html><html><head><title>Subscribe to read</title>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "{expected_headline}",
        "datePublished": "2026-03-03T16:04:04.200Z",
        "isAccessibleForFree": "False"
      }}
      </script>
    </head><body>
      <h1>{expected_headline}</h1>
      <div id="barrier-page">Subscribe to unlock this article</div>
    </body></html>
    """.encode() + (b" " * 90_000)
    search_html = f"""
    <html><body><ol id="web">
      <li><h3><a href="{canonical_url}">
        {expected_headline} - Financial Times
      </a></h3></li>
      <li><h3><a href="{partner_url}">
        {expected_headline}
      </a></h3></li>
    </ol></body></html>
    """.encode()
    partner_html = f"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {{
        "@type": "NewsArticle",
        "headline": "{expected_headline}",
        "datePublished": "2026-03-03T17:04:04Z",
        "author": {{"name": "Financial Times"}}
      }}
      </script>
    </head><body><article>
      <p>Copyright The Financial Times Limited 2026</p>
      <p>The chancellor used a deliberately short statement to project
      economic credibility while conflict abroad unsettled investors and
      members of her own party. The report describes the fiscal forecasts,
      the reaction in parliament, and the political calculation behind the
      speech in enough detail to identify a complete licensed copy.</p>
      <p>Officials argued that stable public finances would help households
      and businesses absorb the energy shock. Economists discussed gilt
      yields, inflation, borrowing, and the assumptions made by the fiscal
      watchdog, while Labour MPs pressed ministers for a clearer account of
      the choices that could follow if the conflict continued.</p>
      <p>The final section records responses from opposition politicians,
      investors, and government advisers. It also explains how the spring
      statement fits the wider budget timetable and why the chancellor
      chose reassurance over announcing another package of measures.</p>
    </article></body></html>
    """.encode() + (b" " * 2_048)
    client = StubArchiveClient(
        {
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
            snapshot_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                paywall_html,
                snapshot_url,
            ),
            title_search_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                search_html,
                title_search_url,
            ),
            partner_url: (
                200,
                {"content-type": "text/html; charset=utf-8"},
                partner_html,
                partner_url,
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
        timemap_url,
        snapshot_url,
        title_search_url,
        partner_url,
    ]
    capture = result["capture"]
    assert capture.selected_candidate.snapshot_url == partner_url
    assert capture.quality_signals["ftSyndicationValidated"] is True
    assert capture.quality_signals["syndicationHeadlineOverlap"] == 1.0
    assert capture.quality_signals["syndicationFtCopyrightAttributed"] is True
    assert capture.quality_signals["syndicationBodyCharacters"] >= 400


def test_ft_syndication_recovers_missing_headline_from_google_news():
    canonical_url = (
        "https://www.ft.com/content/"
        "02a3b935-62dc-44a1-9957-6867e8ee1890"
    )
    expected_headline = (
        "Israel attacks Beirut days after Trump's showdown with Netanyahu"
    )
    partner_url = (
        "https://example.com/world/"
        "israel-attacks-beirut-after-trump-showdown"
    )
    item = ManifestItem(
        publisher="ft",
        canonical_url=canonical_url,
        published_at="2026-06-07T15:02:53.730Z",
        section="world",
        candidates=(),
    )
    canonical_search_url = ft_syndication_search_url(item)
    google_news_url = ft_google_news_headline_search_url(item)
    title_search_url = ft_syndication_title_search_url(
        expected_headline
    )
    broad_search_url = ft_syndication_broad_title_search_url(
        expected_headline
    )
    google_news_xml = f"""
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>{expected_headline} - Financial Times</title>
        <pubDate>Sun, 07 Jun 2026 07:00:00 GMT</pubDate>
        <source url="https://www.ft.com">Financial Times</source>
      </item>
      <item>
        <title>A different report with a nearby publication date</title>
        <pubDate>Sun, 07 Jun 2026 08:00:00 GMT</pubDate>
        <source url="https://example.net">Another Publisher</source>
      </item>
    </channel></rss>
    """.encode()
    broad_search_html = f"""
    <html><body><ol id="web">
      <li><h3><a href="{canonical_url}">
        {expected_headline} - Financial Times
      </a></h3></li>
      <li><h3><a href="{partner_url}">
        {expected_headline}
      </a></h3></li>
    </ol></body></html>
    """.encode()
    client = StubArchiveClient(
        {
            canonical_search_url: (
                200,
                {"content-type": "text/html"},
                b"<html><ol id='web'></ol></html>",
                canonical_search_url,
            ),
            google_news_url: (
                200,
                {"content-type": "application/rss+xml"},
                google_news_xml,
                google_news_url,
            ),
            title_search_url: (
                200,
                {"content-type": "text/html"},
                b"<html><ol id='web'></ol></html>",
                title_search_url,
            ),
            broad_search_url: (
                200,
                {"content-type": "text/html"},
                broad_search_html,
                broad_search_url,
            ),
        }
    )

    candidates = discover_ft_syndication_candidates(
        item,
        archive_client=client,
    )

    assert client.requests == [
        canonical_search_url,
        google_news_url,
        title_search_url,
        broad_search_url,
    ]
    assert [candidate.snapshot_url for candidate in candidates] == [
        partner_url
    ]
    assert candidates[0].expected_headline == expected_headline


def test_ft_syndication_refines_google_news_partner_host_with_yahoo():
    expected_headline = (
        "AI is forecast to put 200,000 European banking jobs "
        "at risk by 2030"
    )
    partner_url = (
        "https://www.irishtimes.com/business/2026/01/01/"
        "ai-is-forecast-to-put-200000-european-banking-jobs-"
        "at-risk-by-2030/"
    )
    item = ManifestItem(
        publisher="ft",
        canonical_url=(
            "https://www.ft.com/content/"
            "71e12f85-1edb-4156-8cb5-3fe8aef36d93"
        ),
        published_at="2026-01-01T08:00:00Z",
        section="banking",
        candidates=(),
    )
    title_search_url = ft_syndication_title_search_url(
        expected_headline
    )
    broad_search_url = ft_syndication_broad_title_search_url(
        expected_headline
    )
    google_news_url = ft_google_news_partner_search_url(
        expected_headline
    )
    partner_search_url = ft_syndication_partner_site_search_url(
        expected_headline,
        "www.irishtimes.com",
    )
    google_news_xml = f"""
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>
          AI is forecast to ‘put 200,000 European banking jobs at risk’
          by 2030 - The Irish Times
        </title>
        <pubDate>Thu, 01 Jan 2026 08:00:00 GMT</pubDate>
        <source url="https://www.irishtimes.com">The Irish Times</source>
      </item>
      <item>
        <title>{expected_headline} - Unrelated Archive</title>
        <pubDate>Thu, 01 Jan 2015 08:00:00 GMT</pubDate>
        <source url="https://unrelated.example">Unrelated Archive</source>
      </item>
    </channel></rss>
    """.encode()
    partner_search_html = f"""
    <html><body><ol id="web"><li><h3>
      <a href="{partner_url}">
        AI is forecast to put 200,000 European banking jobs at risk
        by 2030 - The Irish Times
      </a>
    </h3></li></ol></body></html>
    """.encode()
    empty_search = b"<html><ol id='web'></ol></html>"
    client = StubArchiveClient(
        {
            title_search_url: (
                200,
                {"content-type": "text/html"},
                empty_search,
                title_search_url,
            ),
            broad_search_url: (
                200,
                {"content-type": "text/html"},
                empty_search,
                broad_search_url,
            ),
            google_news_url: (
                200,
                {"content-type": "application/rss+xml"},
                google_news_xml,
                google_news_url,
            ),
            partner_search_url: (
                200,
                {"content-type": "text/html"},
                partner_search_html,
                partner_search_url,
            ),
        }
    )

    candidates = discover_ft_syndication_candidates(
        item,
        archive_client=client,
        expected_headline=expected_headline,
    )

    assert client.requests == [
        title_search_url,
        broad_search_url,
        google_news_url,
        partner_search_url,
    ]
    assert [candidate.snapshot_url for candidate in candidates] == [
        partner_url
    ]
    assert candidates[0].expected_headline == expected_headline


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


def test_bloomberg_syndication_rejects_short_paywall_preview(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2020-12-14/"
        "bank-m-a-deep-freeze-thaws-with-wave-of-billion-dollar-deals"
    )
    guessed_url = (
        "https://web.archive.org/web/20201215000000id_/" + canonical_url
    )
    partner_url = (
        "https://www.afr.com/companies/financial-services/"
        "bank-m-and-a-deep-freeze-thaws-with-billion-dollar-deal-wave-"
        "20201215-p56nn6"
    )
    item = ManifestItem(
        publisher="bloomberg",
        canonical_url=canonical_url,
        published_at="2020-12-14T00:00:00Z",
        section="business",
        candidates=(candidate(guessed_url, "20201215000000"),),
    )
    search_url = bloomberg_syndication_search_url(item)
    search_html = f"""
    <html><body><ol id="web"><li><h3>
      <a href="{partner_url}">Licensed Bloomberg copy</a>
    </h3></li></ol></body></html>
    """.encode()
    partner_html = b"""
    <!doctype html><html><head>
      <script type="application/ld+json">
      {
        "@type": "NewsArticle",
        "headline": "Bank M&A Deep Freeze Thaws With Wave of Billion-Dollar Deals",
        "datePublished": "2020-12-14T12:00:00Z",
        "author": {"name": "Bloomberg"}
      }
      </script>
    </head><body><article><div class="article-content">
      <p>Huntington Bancshares announced a multibillion-dollar regional bank
      deal, and industry executives said more mergers and acquisitions were
      likely after the market's pandemic freeze.</p>
      <p>Bloomberg reported that low interest rates, tight margins, executive
      succession and pressure to grow were driving the renewed deal wave.
      These two paragraphs are only the publicly visible preview.</p>
      <p>Subscribe to gift this article.</p>
      <p>Already a subscriber? Login</p>
    </div></article></body></html>
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
            partner_url: (
                200,
                {"content-type": "text/html"},
                partner_html,
                partner_url,
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
    assert "suspected-paywall-shell" in result["error"]
    assert not (tmp_path / "objects").exists()


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


def test_raw_quality_rejects_bloomberg_terms_violation_redirect():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Terms of Service Violation</title></head>
        <body><p>Your usage has been flagged as a violation of our terms
        of service. Please confirm that you are not a robot.</p></body>
        </html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
        final_url=(
            "https://web.archive.org/web/20180919041634id_/"
            "https://www.bloomberg.com/tosv2.html?url=encoded"
        ),
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
        "Try FT for free | Financial Times",
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


def test_raw_quality_rejects_ft_legacy_barrier_variants():
    modern_score, modern_signals = score_raw_capture(
        b"""
        <html><body>
          <div class="barrier-grid__article-title">
            Archived headline without article body
          </div>
        </body></html>
        """ + (b" " * 220_000),
        http_status=200,
        content_type="text/html",
        final_url=(
            "https://web.archive.org/web/20161009061251id_/"
            "https://www.ft.com/content/example"
        ),
    )
    legacy_score, legacy_signals = score_raw_capture(
        b"""
        <html><head><title>Archived FT headline - FT.com</title></head>
        <body><nav>World Companies Markets Subscribe Sign in</nav></body>
        </html>
        """ + (b" " * 32_000),
        http_status=200,
        content_type="text/html",
        final_url=(
            "https://web.archive.org/web/20160622180631id_/"
            "http://www.ft.com/cms/s/example,Authorised=false.html"
            "?classification=conditional_standard&iab=barrier-app"
        ),
    )

    assert modern_score < 85
    assert modern_signals["subscriptionShell"] is True
    assert legacy_score < 85
    assert legacy_signals["subscriptionShell"] is True
    assert legacy_signals["ftLegacyBarrierUrl"] is True


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


def test_stored_bloomberg_partner_paywall_preview_is_requeued(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.bloomberg.com/news/articles/2020-12-14/"
        "bank-m-a-deep-freeze-thaws-with-wave-of-billion-dollar-deals"
    )
    partner_url = (
        "https://www.afr.com/companies/financial-services/"
        "bank-m-and-a-deep-freeze-thaws-with-billion-dollar-deal-wave-"
        "20201215-p56nn6"
    )
    shell = b"""
    <!doctype html><html><head>
      <meta property="og:title"
            content="Bank M&A Deep Freeze Thaws With Wave of Billion-Dollar Deals">
      <meta property="article:published_time"
            content="2020-12-14T12:00:00Z">
    </head><body><article><div class="article-content">
      <p>Huntington Bancshares announced a multibillion-dollar regional bank
      deal, and industry executives said more mergers and acquisitions were
      likely after the market's pandemic freeze.</p>
      <p>Bloomberg reported that low interest rates, tight margins, executive
      succession and pressure to grow were driving the renewed deal wave.
      These two paragraphs are only the publicly visible preview.</p>
      <p>Subscribe to gift this article.</p>
      <p>Already a subscriber? Login</p>
    </div></article></body></html>
    """ + (b" " * 2_048)
    blob = store_raw_html(tmp_path, shell)
    capture = RawCapture(
        article_id="bloomberg:" + ("b" * 64),
        publisher="bloomberg",
        canonical_url=canonical_url,
        published_at=datetime(2020, 12, 14, tzinfo=timezone.utc),
        selected_candidate=CaptureCandidate(
            provider=CaptureProvider.OTHER,
            snapshot_url=partner_url,
        ),
        candidates_considered=[],
        retrieved_at=datetime.now(timezone.utc),
        final_url=partner_url,
        http_status=200,
        content_type="text/html",
        quality_score=100,
        raw_html=blob,
    )

    reason = completed_capture_rejection_reason(
        capture,
        archive_root=tmp_path,
    )

    assert reason == "bloomberg-syndication-paywall-shell"


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


def test_raw_quality_rejects_wsj_full_story_subscription_preview():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>A WSJ gallery</title></head>
        <body><article>
          <h1>A Duplex Penthouse in the Heart of Singapore</h1>
          <p>A single image caption survives in this archived preview.</p>
          <div>To Read the Full Story, Subscribe or Sign In</div>
        </article></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["subscriptionShell"] is True


def test_raw_quality_rejects_wsj_empty_article_jsonld_shell():
    score, signals = score_raw_capture(
        b"""
        <html><head><script type="application/ld+json">
        {
          "@type": "NewsArticle",
          "headline": "",
          "datePublished": "",
          "url": "https://www.wsj.com/articles/"
        }
        </script></head><body><article>
          <h2>Most Popular Articles</h2>
        </article></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
    )

    assert score < 85
    assert signals["subscriptionShell"] is True
    assert signals["wsjEmptyArticleShell"] is True


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


def test_stored_wsj_subscription_shell_keeps_complete_article(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.wsj.com/articles/"
        "a-complete-archived-article-1482289646"
    )
    shell = b"""
    <!doctype html><html><head>
      <meta property="og:title"
            content="A Complete Archived Article">
      <script type="application/json">
        {"isSnippetView":true}
      </script>
    </head><body>
      <article data-testid="article-body">
        <p>The archived page contains the complete first paragraph with
        enough reporting detail to establish that this is article text.</p>
        <p>A second substantive paragraph continues the report and provides
        additional facts, context and quotations from the original story.</p>
      </article>
      <div>Continue reading your article with a WSJ subscription.</div>
      <div>Already a subscriber?</div>
    </body></html>
    """ + (b" " * 2_048)
    blob = store_raw_html(tmp_path, shell)
    capture = RawCapture(
        article_id="wsj:" + ("c" * 64),
        publisher="wsj",
        canonical_url=canonical_url,
        published_at=datetime(2016, 12, 21, tzinfo=timezone.utc),
        selected_candidate=CaptureCandidate(
            provider=CaptureProvider.WAYBACK,
            snapshot_url=(
                "https://web.archive.org/web/20161222000000id_/"
                + canonical_url
            ),
        ),
        candidates_considered=[],
        retrieved_at=datetime.now(timezone.utc),
        final_url=canonical_url,
        http_status=200,
        content_type="text/html",
        quality_score=40,
        raw_html=blob,
    )

    _, signals = score_raw_capture(
        shell,
        http_status=200,
        content_type="text/html",
        final_url=canonical_url,
    )
    reason = completed_capture_rejection_reason(
        capture,
        archive_root=tmp_path,
    )

    assert signals["subscriptionShell"] is True
    assert reason is None


def test_stored_wsj_subscription_shell_still_rejects_short_preview(
    tmp_path: Path,
):
    canonical_url = (
        "https://www.wsj.com/articles/"
        "a-short-archived-preview-1482289646"
    )
    shell = b"""
    <html><head>
      <meta property="og:title" content="A Short Archived Preview">
      <script type="application/json">{"isSnippetView":true}</script>
    </head><body><article>
      <p>A short preview sentence.</p>
    </article></body></html>
    """ + (b" " * 2_048)
    blob = store_raw_html(tmp_path, shell)
    capture = RawCapture(
        article_id="wsj:" + ("d" * 64),
        publisher="wsj",
        canonical_url=canonical_url,
        published_at=datetime(2016, 12, 21, tzinfo=timezone.utc),
        selected_candidate=CaptureCandidate(
            provider=CaptureProvider.WAYBACK,
            snapshot_url=(
                "https://web.archive.org/web/20161222000000id_/"
                + canonical_url
            ),
        ),
        candidates_considered=[],
        retrieved_at=datetime.now(timezone.utc),
        final_url=canonical_url,
        http_status=200,
        content_type="text/html",
        quality_score=40,
        raw_html=blob,
    )

    reason = completed_capture_rejection_reason(
        capture,
        archive_root=tmp_path,
    )

    assert reason == "subscription-shell"


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


def test_raw_quality_keeps_legacy_ft_amp_article_body():
    score, signals = score_raw_capture(
        b"""
        <html><head><title>Archived Financial Times article</title></head>
        <body><article>
          <div class="article-body" itemprop="articleBody">
            <p>France's highest court revived a corruption probe into the
            former president after rejecting his argument about taped phone
            conversations with his lawyer and a senior prosecutor.</p>
            <p>The ruling means the investigation will resume and may lead
            to a trial while the politician prepares to seek his party's
            nomination in the next presidential election.</p>
          </div>
        </article></body></html>
        """ + (b" " * 2_048),
        http_status=200,
        content_type="text/html",
        final_url=(
            "https://web.archive.org/web/20160624005500id_/"
            "https://amp.ft.com/content/"
            "12333d72-f038-11e5-9f20-c3a047354386"
        ),
    )

    assert score >= 85
    assert signals["ftBodyCharacters"] >= 250
    assert signals["ftTruncatedArticleShell"] is False


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
