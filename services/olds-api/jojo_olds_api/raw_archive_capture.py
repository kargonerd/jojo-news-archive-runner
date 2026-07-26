from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
from html import escape
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from threading import Lock
from typing import Callable, Iterable
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
from .bloomberg_archive_download import ArchiveClient
from .common_crawl import (
    discover_common_crawl_candidates,
    fetch_common_crawl_candidate,
)
from .ft_syndication_catalog import (
    INFINI_DATASET,
    INFINI_DATASET_ROWS_ENDPOINT,
)
from .ghostarchive import (
    discover_ghostarchive_candidates,
    fetch_ghostarchive_candidate,
    is_ghostarchive_candidate_url,
)
from .news_models import (
    ArticleStatus,
    BlobReference,
    CaptureCandidate,
    CaptureProvider,
    CaptureRepresentation,
    RawCapture,
)


SCHEMA_VERSION = "jojo-raw-capture-state/1"
CAPTURE_POLICY_VERSIONS = {
    "ap": "ap-capture/0.5.0",
    "bloomberg": "bloomberg-capture/0.10.1",
    "ft": "ft-capture/0.13.0",
    "nyt": "nyt-capture/0.8.0",
    "reuters": "reuters-capture/0.7.0",
    "wsj": "wsj-capture/0.8.2",
}
ACCEPTED_HTTP_STATUSES = {200, 206}
WAYBACK_TIMEMAP_ENDPOINT = "https://web.archive.org/web/timemap/json"
WAYBACK_TIMEMAP_MAXIMUM_BYTES = 2_000_000
WAYBACK_TIMEMAP_MAXIMUM_CANDIDATES = 8
WAYBACK_TIMEMAP_FALLBACK_PUBLISHERS = {
    "bloomberg",
    "nyt",
    "reuters",
    "wsj",
}
REUTERS_SYNDICATION_SEARCH_ENDPOINT = "https://search.yahoo.com/search"
REUTERS_SYNDICATION_SEARCH_MAXIMUM_BYTES = 2_000_000
REUTERS_SYNDICATION_MAXIMUM_CANDIDATES = 8
REUTERS_SYNDICATION_MINIMUM_BODY_CHARACTERS = 400
BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS = 400
WSJ_SYNDICATION_MINIMUM_BODY_CHARACTERS = 400
FT_SYNDICATION_MINIMUM_BODY_CHARACTERS = 400
FT_CAPTURE_MINIMUM_BODY_CHARACTERS = 100
FT_IMAGE_LED_MINIMUM_IMAGES = 3
FT_GOOGLE_NEWS_RSS_ENDPOINT = "https://news.google.com/rss/search"
FT_GOOGLE_NEWS_MAXIMUM_PARTNER_SOURCES = 3
FT_GOOGLE_NEWS_MAXIMUM_DATE_DELTA_DAYS = 2
FT_SYNDICATION_MAXIMUM_DATE_DELTA_DAYS = 2
FT_ADVISORSTREAM_MAXIMUM_DATE_DELTA_DAYS = 14
FT_KNOWN_PARTNER_SITEMAPS = (
    (
        "https://www.davidruler.com",
        "https://www.davidruler.com/sitemap.xml",
    ),
)
NYT_SYNDICATION_SEARCH_ENDPOINT = REUTERS_SYNDICATION_SEARCH_ENDPOINT
NYT_SYNDICATION_SEARCH_MAXIMUM_BYTES = 2_000_000
NYT_SYNDICATION_MAXIMUM_CANDIDATES = 8
NYT_SYNDICATION_MINIMUM_BODY_CHARACTERS = 1_000
NYT_TRUSTED_WORDPRESS_ENDPOINTS = (
    "https://www.hawaiitribune-herald.com/wp-json/wp/v2/posts",
)
NYT_HEADLINE_WORDPRESS_ENDPOINTS = (
    "https://dnyuz.com/wp-json/wp/v2/posts",
)
COMMON_CRAWL_FALLBACK_PUBLISHERS = {"ft"}
ARQUIVO_PT_FALLBACK_PUBLISHERS = {"ft"}
ARQUIVO_PT_CDX_ENDPOINT = "https://arquivo.pt/wayback/cdx"
ARQUIVO_PT_REPLAY_ENDPOINT = "https://arquivo.pt/noFrame/replay"
ARQUIVO_PT_INDEX_MAXIMUM_BYTES = 2_000_000
ARQUIVO_PT_MAXIMUM_CANDIDATES = 3
REUTERS_SYNDICATION_STOP_WORDS = {
    "a",
    "after",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "s",
    "the",
    "to",
    "with",
}
_HTML_MARKERS = (
    b"<!doctype html",
    b"<html",
    b"<article",
    b"application/ld+json",
)
_ARCHIVE_ERROR_MARKERS = (
    b"wayback machine doesn't have that page archived",
    b"this url has been excluded from the wayback machine",
    b"cannot be crawled or displayed due to robots.txt",
)
_AUTH_SHELL_MARKERS = (
    b"<title>log in - ",
    b"<title>sign in - ",
    b"/auth/login?",
    b"sign in to continue",
    b"log in to continue",
    b'id="myaccountauth"',
    b'sourceapp" content="nyt-lire"',
    b"/lire_ui/",
)
_ACCESS_CHALLENGE_MARKERS = (
    b"are you a robot?",
    b"we've detected unusual activity",
    b"verify you are human",
    b"checking if the site connection is secure",
    b"<title>client challenge</title>",
    b"javascript is disabled in your browser",
    b"a required part of this site couldn",
    b"terms of service violation",
)
_REDIRECT_SHELL_MARKERS = (
    b"window.location = fullurl",
    b"window.location=fullurl",
)
_SUBSCRIPTION_SHELL_MARKERS = (
    b"<title>subscribe to read",
    b"<title>become an ft subscriber to read",
    b"<title>subscribe to a slice of the ft",
    b"<title>try ft for free",
    b'id="barrier-page"',
    b"barrier-grid__article-title",
    b"subscribe to unlock this article",
    b"window.zephr.outcomes['paywall']",
    b"join over 300,000 finance professionals",
    b"discover all the plans currently available in your country",
    b"during your trial you will have complete digital access to ft.com",
    b"to read the full story, subscribe or sign in",
    b'class="wsj-snippet-login"',
)
_PARSED_PAYWALL_PHRASES = (
    "subscribe to read",
    "subscribe to continue",
    "sign in to continue",
    "already a subscriber",
    "unlock this article",
)
_PARSED_PAYWALL_MAXIMUM_BODY_CHARACTERS = 1_000
_ARTICLE_BODY_MARKERS = (
    b"article__content-body",
    b'id="article-body"',
    b"data-trackable=\"article-body\"",
    b"data-testid=\"article-body\"",
    b"story-body",
)
_WAYBACK_FINAL_RE = re.compile(
    r"https?://web\.archive\.org/web/(\d{14})(?:id_|im_|js_|cs_)?/",
    re.IGNORECASE,
)
_ft_known_partner_urls: dict[str, str] | None = None
_ft_known_partner_urls_lock = Lock()


@dataclass(frozen=True)
class ManifestItem:
    publisher: str
    canonical_url: str
    published_at: str | None
    section: str | None
    candidates: tuple[CaptureCandidate, ...]

    @property
    def article_id(self) -> str:
        digest = hashlib.sha256(self.canonical_url.encode("utf-8")).hexdigest()
        return f"{self.publisher}:{digest}"


@dataclass(frozen=True)
class NytSyndicationDiscovery:
    expected_headline: str | None
    candidates: tuple[CaptureCandidate, ...]


def initialize_capture_schema(
    connection: sqlite3.Connection,
    *,
    publisher: str,
    authorization_reference: str,
) -> None:
    if not authorization_reference.strip():
        raise ValueError("authorization_reference must not be empty")
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS archive_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS captures (
            canonical_url TEXT PRIMARY KEY,
            article_id TEXT NOT NULL,
            publisher TEXT NOT NULL,
            published_at TEXT,
            section TEXT,
            candidates_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            selected_candidate_json TEXT,
            final_url TEXT,
            http_status INTEGER,
            content_type TEXT,
            quality_score INTEGER,
            quality_signals_json TEXT,
            raw_path TEXT,
            raw_sha256 TEXT,
            raw_bytes INTEGER,
            stored_bytes INTEGER,
            record_path TEXT,
            last_error TEXT,
            retrieved_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_captures_status
            ON captures(status);
        CREATE INDEX IF NOT EXISTS idx_captures_published_at
            ON captures(published_at);
        """
    )
    capture_policy_version = CAPTURE_POLICY_VERSIONS.get(
        publisher,
        f"{publisher}-capture/1",
    )
    previous_policy = connection.execute(
        """
        SELECT value
        FROM archive_metadata
        WHERE key='capture_policy_version'
        """
    ).fetchone()
    policy_changed = (
        previous_policy is None
        or str(previous_policy[0]) != capture_policy_version
    )
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "publisher": publisher,
        "authorization_reference": authorization_reference,
        "capture_policy_version": capture_policy_version,
    }
    connection.executemany(
        """
        INSERT INTO archive_metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        metadata.items(),
    )
    if policy_changed:
        connection.execute(
            """
            UPDATE captures
            SET status='pending',
                attempts=0,
                last_error=NULL,
                updated_at=?
            WHERE status='error'
            """,
            (_now_iso(),),
        )
    connection.execute(
        """
        UPDATE captures
        SET status='pending',
            last_error='interrupted before completion',
            updated_at=?
        WHERE status='downloading'
        """,
        (_now_iso(),),
    )
    connection.commit()


def load_capture_manifest(
    connection: sqlite3.Connection,
    *,
    manifest_path: Path,
    publisher: str,
) -> dict[str, int]:
    inserted = 0
    seen = 0
    batch: list[tuple[object, ...]] = []
    for row in _read_jsonl(manifest_path):
        item = manifest_item_from_row(row, publisher=publisher)
        seen += 1
        if not item.candidates:
            continue
        batch.append(
            (
                item.canonical_url,
                item.article_id,
                item.publisher,
                item.published_at,
                item.section,
                json.dumps(
                    [
                        candidate.model_dump(
                            mode="json",
                            by_alias=True,
                            exclude_none=True,
                        )
                        for candidate in item.candidates
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                _now_iso(),
            )
        )
        if len(batch) >= 1_000:
            inserted += _insert_manifest_batch(connection, batch)
            batch.clear()
    if batch:
        inserted += _insert_manifest_batch(connection, batch)
    connection.commit()
    return {"manifestRows": seen, "inserted": inserted}


def manifest_item_from_row(row: dict, *, publisher: str) -> ManifestItem:
    row_publisher = str(row.get("publisher") or publisher).strip().lower()
    if row_publisher != publisher:
        raise ValueError(
            f"manifest publisher {row_publisher!r} does not match {publisher!r}"
        )
    canonical_url = str(
        row.get("canonical_url")
        or row.get("canonicalUrl")
        or row.get("url")
        or ""
    ).strip()
    if not canonical_url.startswith(("http://", "https://")):
        raise ValueError(f"manifest row has invalid canonical URL: {canonical_url!r}")

    raw_candidates = row.get("candidates")
    candidates: list[CaptureCandidate] = []
    if isinstance(raw_candidates, list):
        for candidate in raw_candidates:
            candidates.append(CaptureCandidate.model_validate(candidate))
    else:
        snapshot_url = str(row.get("wayback_snapshot_url") or "").strip()
        timestamp = str(row.get("wayback_timestamp") or "").strip()
        if snapshot_url:
            candidates.append(
                CaptureCandidate(
                    provider=CaptureProvider.WAYBACK,
                    snapshot_url=snapshot_url,
                    captured_at=_wayback_datetime(timestamp),
                    digest=_optional_string(row.get("wayback_digest")),
                    mime_type=_optional_string(row.get("wayback_mimetype")),
                    status_code=_optional_int(row.get("wayback_status_code")) or 200,
                )
            )
    published_at = _optional_string(
        row.get("published_at")
        or row.get("publishedAt")
        or row.get("catalog_date")
    )
    return ManifestItem(
        publisher=publisher,
        canonical_url=canonical_url,
        published_at=published_at,
        section=_optional_string(row.get("section")),
        candidates=tuple(candidates),
    )


def pending_captures(
    connection: sqlite3.Connection,
    *,
    retry_errors: bool,
    maximum: int | None,
    maximum_record_attempts: int,
    prioritize_parser_validation: bool = False,
) -> list[ManifestItem]:
    if maximum_record_attempts < 1:
        raise ValueError("maximum_record_attempts must be positive")
    priority_urls: list[str] = []
    if prioritize_parser_validation:
        from .parser_validation import pending_parser_validation_urls

        priority_urls = pending_parser_validation_urls(
            connection,
            maximum=maximum,
            maximum_record_attempts=maximum_record_attempts,
        )
    priority_rows: list[tuple] = []
    if priority_urls:
        placeholders = ",".join("?" for _ in priority_urls)
        rows_by_url = {
            row[1]: row
            for row in connection.execute(
                f"""
                SELECT
                    publisher,
                    canonical_url,
                    published_at,
                    section,
                    candidates_json
                FROM captures
                WHERE canonical_url IN ({placeholders})
                """,
                priority_urls,
            ).fetchall()
        }
        priority_rows = [
            rows_by_url[url] for url in priority_urls if url in rows_by_url
        ]

    remaining = (
        None
        if maximum is None
        else max(0, maximum - len(priority_rows))
    )
    if remaining == 0:
        rows = priority_rows
        return [_manifest_item_from_capture_row(row) for row in rows]

    statuses = ("pending", "error") if retry_errors else ("pending",)
    placeholders = ",".join("?" for _ in statuses)
    query = f"""
        SELECT publisher, canonical_url, published_at, section, candidates_json
        FROM captures
        WHERE status IN ({placeholders})
          AND (status='pending' OR attempts < ?)
        ORDER BY COALESCE(published_at, ''), canonical_url
    """
    parameters: list[object] = [*statuses, maximum_record_attempts]
    if priority_urls:
        excluded = ",".join("?" for _ in priority_urls)
        query = query.replace(
            "ORDER BY COALESCE(published_at, ''), canonical_url",
            f"""
              AND canonical_url NOT IN ({excluded})
            ORDER BY COALESCE(published_at, ''), canonical_url
            """,
        )
        parameters.extend(priority_urls)
    if remaining is not None:
        query += " LIMIT ?"
        parameters.append(remaining)
    rows = priority_rows + connection.execute(query, parameters).fetchall()
    return [_manifest_item_from_capture_row(row) for row in rows]


def _manifest_item_from_capture_row(row: tuple) -> ManifestItem:
    return ManifestItem(
        publisher=row[0],
        canonical_url=row[1],
        published_at=row[2],
        section=row[3],
        candidates=tuple(
            CaptureCandidate.model_validate(candidate)
            for candidate in json.loads(row[4])
        ),
    )


def mark_capture_downloading(
    connection: sqlite3.Connection,
    item: ManifestItem,
) -> None:
    connection.execute(
        """
        UPDATE captures
        SET status='downloading',
            attempts=attempts+1,
            last_error=NULL,
            updated_at=?
        WHERE canonical_url=?
        """,
        (_now_iso(), item.canonical_url),
    )
    connection.commit()


def capture_item(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    output_dir: Path,
    maximum_html_bytes: int,
    enable_common_crawl_fallback: bool = False,
    enable_arquivo_pt_fallback: bool = False,
    ft_syndication_lookup: Callable[
        [ManifestItem, str],
        tuple[CaptureCandidate, ...],
    ]
    | None = None,
) -> dict:
    failures: list[str] = []
    candidates_considered = list(item.candidates)
    best_response: tuple[
        CaptureCandidate,
        int,
        bytes,
        str,
        str,
        int,
        dict[str, object],
    ] | None = None
    ft_raw_partner_validated = False
    ft_infini_origin_validated = False
    ft_title_index_attempted = False
    ft_dynamic_syndication_attempted = False
    ft_ghostarchive_attempted = False
    ft_original_headline = next(
        (
            candidate.expected_headline
            for candidate in item.candidates
            if candidate.expected_headline
        ),
        None,
    )

    def observe_candidate_response(
        candidate: CaptureCandidate,
        content: bytes,
        final_url: str,
    ) -> None:
        nonlocal ft_original_headline
        if (
            item.publisher != "ft"
            or ft_original_headline
            or candidate.provider == CaptureProvider.OTHER
        ):
            return
        ft_original_headline = _extract_ft_original_headline(
            content,
            expected_published_at=item.published_at,
            final_url=final_url,
        )

    def consider_candidates(
        candidates: Iterable[CaptureCandidate],
    ) -> None:
        nonlocal best_response
        nonlocal ft_raw_partner_validated
        nonlocal ft_infini_origin_validated
        for candidate in candidates:
            if (
                candidate.provider == CaptureProvider.INFINI_NEWS
                and (
                    ft_raw_partner_validated
                    or ft_infini_origin_validated
                )
            ):
                continue
            response, failure = _fetch_usable_candidate(
                candidate,
                archive_client=archive_client,
                maximum_html_bytes=maximum_html_bytes,
                canonical_url=item.canonical_url,
                publisher=item.publisher,
                response_observer=observe_candidate_response,
            )
            if failure:
                failures.append(failure)
            if response is None:
                continue
            if (
                item.publisher == "nyt"
                and candidate.provider == CaptureProvider.OTHER
            ):
                validated, validation_signals = (
                    _validate_nyt_syndication_response(
                        item,
                        expected_headline=candidate.expected_headline,
                        content=response[2],
                        final_url=response[3],
                    )
                )
                if not validated:
                    failures.append(
                        "nyt-syndication:validation:"
                        + str(
                            validation_signals.get("reason") or "failed"
                        )
                    )
                    continue
                response = (
                    response[0],
                    response[1],
                    response[2],
                    response[3],
                    response[4],
                    response[5],
                    response[6] | validation_signals,
                )
            if (
                item.publisher == "wsj"
                and candidate.provider == CaptureProvider.OTHER
            ):
                validated, validation_signals = (
                    _validate_wsj_syndication_response(
                        item,
                        expected_headline=candidate.expected_headline,
                        content=response[2],
                        final_url=response[3],
                    )
                )
                if not validated:
                    failures.append(
                        "wsj-syndication:validation:"
                        + str(
                            validation_signals.get("reason") or "failed"
                        )
                    )
                    continue
                response = (
                    response[0],
                    response[1],
                    response[2],
                    response[3],
                    response[4],
                    response[5],
                    response[6] | validation_signals,
                )
            if (
                item.publisher == "ft"
                and candidate.provider in {
                    CaptureProvider.OTHER,
                    CaptureProvider.INFINI_NEWS,
                }
            ):
                direct_infini_origin = bool(
                    candidate.provider == CaptureProvider.INFINI_NEWS
                    and _is_ft_origin_url(candidate.source_url)
                )
                ghostarchive_origin = bool(
                    candidate.provider == CaptureProvider.OTHER
                    and is_ghostarchive_candidate_url(
                        candidate.snapshot_url
                    )
                )
                if ghostarchive_origin:
                    validated, validation_signals = (
                        _validate_ft_ghostarchive_response(
                            item,
                            expected_headline=candidate.expected_headline,
                            content=response[2],
                            final_url=response[3],
                        )
                    )
                elif direct_infini_origin:
                    validated, validation_signals = (
                        _validate_ft_infini_origin_response(
                            item,
                            expected_source_url=(
                                candidate.source_url or ""
                            ),
                            expected_headline=candidate.expected_headline,
                            content=response[2],
                            final_url=response[3],
                        )
                    )
                else:
                    validated, validation_signals = (
                        _validate_ft_syndication_response(
                            item,
                            expected_partner_url=(
                                candidate.source_url
                                or candidate.snapshot_url
                            ),
                            expected_headline=candidate.expected_headline,
                            content=response[2],
                            final_url=response[3],
                        )
                    )
                if not validated:
                    failures.append(
                        (
                            "ft-ghostarchive-origin"
                            if ghostarchive_origin
                            else (
                                "ft-infini-origin"
                                if direct_infini_origin
                                else "ft-syndication"
                            )
                        )
                        + ":validation:"
                        + str(
                            validation_signals.get("reason") or "failed"
                        )
                    )
                    continue
                response = (
                    response[0],
                    response[1],
                    response[2],
                    response[3],
                    response[4],
                    (
                        100
                        if direct_infini_origin or ghostarchive_origin
                        else response[5]
                    ),
                    response[6] | validation_signals,
                )
                if direct_infini_origin or ghostarchive_origin:
                    ft_infini_origin_validated = True
                elif candidate.provider == CaptureProvider.OTHER:
                    ft_raw_partner_validated = True
            if (
                item.publisher == "bloomberg"
                and candidate.provider == CaptureProvider.OTHER
                and _is_bnn_wayback_candidate(candidate.snapshot_url)
            ):
                validated, validation_signals = (
                    _validate_bloomberg_bnn_response(
                        item,
                        expected_headline=candidate.expected_headline,
                        content=response[2],
                        final_url=response[3],
                    )
                )
                if not validated:
                    failures.append(
                        "bloomberg-bnn:validation:"
                        + str(
                            validation_signals.get("reason") or "failed"
                        )
                    )
                    continue
                response = (
                    response[0],
                    response[1],
                    response[2],
                    response[3],
                    response[4],
                    response[5],
                    response[6] | validation_signals,
                )
            if (
                item.publisher == "bloomberg"
                and candidate.provider == CaptureProvider.OTHER
                and candidate.expected_headline
                and not _is_bnn_wayback_candidate(candidate.snapshot_url)
            ):
                validated, validation_signals = (
                    _validate_bloomberg_partner_archive_response(
                        item,
                        expected_headline=candidate.expected_headline,
                        content=response[2],
                        final_url=response[3],
                    )
                )
                if not validated:
                    failures.append(
                        "bloomberg-partner:validation:"
                        + str(
                            validation_signals.get("reason") or "failed"
                        )
                    )
                    continue
                response = (
                    response[0],
                    response[1],
                    response[2],
                    response[3],
                    response[4],
                    response[5],
                    response[6] | validation_signals,
                )
            if best_response is None or response[5] > best_response[5]:
                best_response = response
            if response[5] == 100:
                break

    def consider_ft_title_index() -> None:
        nonlocal ft_title_index_attempted
        if (
            ft_title_index_attempted
            or best_response is not None
            or item.publisher != "ft"
            or ft_syndication_lookup is None
            or not ft_original_headline
        ):
            return
        ft_title_index_attempted = True
        try:
            indexed_candidates = ft_syndication_lookup(
                item,
                ft_original_headline,
            )
        except Exception as exc:
            failures.append(f"ft-title-index:{type(exc).__name__}")
            indexed_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        indexed_candidates = tuple(
            candidate
            for candidate in indexed_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(indexed_candidates)
        consider_candidates(indexed_candidates)

    def consider_ft_dynamic_syndication() -> None:
        nonlocal ft_dynamic_syndication_attempted
        if (
            ft_dynamic_syndication_attempted
            or best_response is not None
            or item.publisher != "ft"
        ):
            return
        ft_dynamic_syndication_attempted = True
        try:
            fallback_candidates = discover_ft_syndication_candidates(
                item,
                archive_client=archive_client,
                expected_headline=ft_original_headline,
            )
        except Exception as exc:
            failures.append(f"ft-syndication:{type(exc).__name__}")
            fallback_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        fallback_candidates = tuple(
            candidate
            for candidate in fallback_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(fallback_candidates)
        consider_candidates(fallback_candidates)
        if best_response is not None or not fallback_candidates:
            return
        fallback_headline = next(
            (
                candidate.expected_headline
                for candidate in fallback_candidates
                if candidate.expected_headline
            ),
            ft_original_headline,
        )
        try:
            additional_candidates = discover_ft_syndication_candidates(
                item,
                archive_client=archive_client,
                expected_headline=fallback_headline,
                skip_title_search=True,
                exhaustive=True,
            )
        except Exception as exc:
            failures.append(
                f"ft-syndication-additional:{type(exc).__name__}"
            )
            additional_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        additional_candidates = tuple(
            candidate
            for candidate in additional_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(additional_candidates)
        consider_candidates(additional_candidates)

    def consider_ft_ghostarchive() -> None:
        nonlocal ft_ghostarchive_attempted
        expected_date = _parse_iso_datetime(item.published_at)
        if (
            ft_ghostarchive_attempted
            or best_response is not None
            or item.publisher != "ft"
            or expected_date is None
            or expected_date.year < 2022
        ):
            return
        ft_ghostarchive_attempted = True
        try:
            ghostarchive_candidates = discover_ghostarchive_candidates(
                item.canonical_url,
                archive_client=archive_client,
                expected_headline=ft_original_headline,
            )
        except Exception as exc:
            failures.append(f"ghostarchive-index:{type(exc).__name__}")
            ghostarchive_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        ghostarchive_candidates = tuple(
            candidate
            for candidate in ghostarchive_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(ghostarchive_candidates)
        consider_candidates(ghostarchive_candidates)

    direct_infini_candidates = tuple(
        candidate
        for candidate in item.candidates
        if (
            candidate.provider == CaptureProvider.INFINI_NEWS
            and _is_ft_origin_url(candidate.source_url)
        )
    )
    consider_candidates(direct_infini_candidates)
    consider_ft_ghostarchive()

    if item.publisher in COMMON_CRAWL_FALLBACK_PUBLISHERS:
        # Exact Wayback captures have historically produced far more usable FT
        # articles than Common Crawl WARC records. Try the nearest exact
        # snapshots first and avoid three index plus Range lookups when one is
        # already a maximum-quality article.
        timemap_candidates: tuple[CaptureCandidate, ...] = ()
        if not ft_infini_origin_validated:
            try:
                timemap_candidates = discover_wayback_timemap_candidates(
                    item,
                    archive_client=archive_client,
                    maximum_candidates=WAYBACK_TIMEMAP_MAXIMUM_CANDIDATES,
                )
            except Exception as exc:
                failures.append(f"wayback-timemap:{type(exc).__name__}")
                timemap_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        timemap_candidates = tuple(
            candidate
            for candidate in timemap_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(timemap_candidates)
        consider_candidates(timemap_candidates)

        # Publication-near Wayback URLs are cheap to resolve and can select a
        # useful later capture outside the bounded exact-capture shortlist.
        # Exhaust them before the slower Common Crawl index/WARC fallback.
        if (
            not ft_infini_origin_validated
            and (best_response is None or best_response[5] < 100)
        ):
            consider_candidates(item.candidates)

        consider_ft_title_index()
        consider_ft_dynamic_syndication()

        if (
            not ft_infini_origin_validated
            and enable_common_crawl_fallback
            and (best_response is None or best_response[5] < 100)
        ):
            try:
                common_crawl_candidates = discover_common_crawl_candidates(
                    item.canonical_url,
                    published_at=item.published_at,
                    archive_client=archive_client,
                )
            except Exception as exc:
                failures.append(f"commoncrawl-index:{type(exc).__name__}")
                common_crawl_candidates = ()
            existing_urls = {
                (
                    candidate.snapshot_url,
                    candidate.warc_offset,
                    candidate.warc_length,
                )
                for candidate in candidates_considered
            }
            common_crawl_candidates = tuple(
                candidate
                for candidate in common_crawl_candidates
                if (
                    candidate.snapshot_url,
                    candidate.warc_offset,
                    candidate.warc_length,
                )
                not in existing_urls
            )
            candidates_considered.extend(common_crawl_candidates)
            consider_candidates(common_crawl_candidates)
    else:
        consider_candidates(item.candidates)

    if best_response is None and item.publisher == "reuters":
        try:
            fallback_candidates = discover_reuters_syndication_candidates(
                item,
                archive_client=archive_client,
            )
        except Exception as exc:
            failures.append(f"reuters-syndication:{type(exc).__name__}")
            fallback_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        fallback_candidates = tuple(
            candidate
            for candidate in fallback_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(fallback_candidates)
        for candidate in fallback_candidates:
            response, failure = _fetch_usable_candidate(
                candidate,
                archive_client=archive_client,
                maximum_html_bytes=maximum_html_bytes,
                canonical_url=item.canonical_url,
                publisher=item.publisher,
            )
            if failure:
                failures.append(failure)
            if response is None:
                continue
            validated, validation_signals = (
                _validate_reuters_syndication_response(
                    item,
                    expected_headline=candidate.expected_headline,
                    content=response[2],
                    final_url=response[3],
                )
            )
            if not validated:
                failures.append(
                    "reuters-syndication:validation:"
                    + str(validation_signals.get("reason") or "failed")
                )
                continue
            response = (
                response[0],
                response[1],
                response[2],
                response[3],
                response[4],
                response[5],
                response[6] | validation_signals,
            )
            if best_response is None or response[5] > best_response[5]:
                best_response = response
            if response[5] == 100:
                break

    if (
        best_response is None
        and item.publisher in WAYBACK_TIMEMAP_FALLBACK_PUBLISHERS
    ):
        try:
            fallback_candidates = discover_wayback_timemap_candidates(
                item,
                archive_client=archive_client,
            )
        except Exception as exc:
            failures.append(f"wayback-timemap:{type(exc).__name__}")
            fallback_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        fallback_candidates = tuple(
            candidate
            for candidate in fallback_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(fallback_candidates)
        for candidate in fallback_candidates:
            response, failure = _fetch_usable_candidate(
                candidate,
                archive_client=archive_client,
                maximum_html_bytes=maximum_html_bytes,
                canonical_url=item.canonical_url,
                publisher=item.publisher,
            )
            if failure:
                failures.append(failure)
            if response is None:
                continue
            if best_response is None or response[5] > best_response[5]:
                best_response = response
            if response[5] == 100:
                break

    if best_response is None and item.publisher == "bloomberg":
        try:
            fallback_candidates = discover_bloomberg_syndication_candidates(
                item,
                archive_client=archive_client,
            )
        except Exception as exc:
            failures.append(f"bloomberg-syndication:{type(exc).__name__}")
            fallback_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        fallback_candidates = tuple(
            candidate
            for candidate in fallback_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(fallback_candidates)
        for candidate in fallback_candidates:
            response, failure = _fetch_usable_candidate(
                candidate,
                archive_client=archive_client,
                maximum_html_bytes=maximum_html_bytes,
                canonical_url=item.canonical_url,
                publisher=item.publisher,
            )
            if failure:
                failures.append(failure)
            if response is None:
                continue
            validated, validation_signals = (
                _validate_bloomberg_syndication_response(
                    item,
                    content=response[2],
                    final_url=response[3],
                )
            )
            if not validated:
                failures.append(
                    "bloomberg-syndication:validation:"
                    + str(validation_signals.get("reason") or "failed")
                )
                continue
            response = (
                response[0],
                response[1],
                response[2],
                response[3],
                response[4],
                response[5],
                response[6] | validation_signals,
            )
            if best_response is None or response[5] > best_response[5]:
                best_response = response
            if response[5] == 100:
                break

    if best_response is None and item.publisher == "nyt":
        try:
            discovery = discover_nyt_syndication(
                item,
                archive_client=archive_client,
            )
        except Exception as exc:
            failures.append(f"nyt-syndication:{type(exc).__name__}")
            discovery = NytSyndicationDiscovery(
                expected_headline=None,
                candidates=(),
            )
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        fallback_candidates = tuple(
            candidate
            for candidate in discovery.candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(fallback_candidates)
        for candidate in fallback_candidates:
            response, failure = _fetch_usable_candidate(
                candidate,
                archive_client=archive_client,
                maximum_html_bytes=maximum_html_bytes,
                canonical_url=item.canonical_url,
                publisher=item.publisher,
            )
            if failure:
                failures.append(failure)
            if response is None:
                continue
            validated, validation_signals = (
                _validate_nyt_syndication_response(
                    item,
                    expected_headline=discovery.expected_headline,
                    content=response[2],
                    final_url=response[3],
                )
            )
            if not validated:
                failures.append(
                    "nyt-syndication:validation:"
                    + str(validation_signals.get("reason") or "failed")
                )
                continue
            response = (
                response[0],
                response[1],
                response[2],
                response[3],
                response[4],
                response[5],
                response[6] | validation_signals,
            )
            if best_response is None or response[5] > best_response[5]:
                best_response = response
            if response[5] == 100:
                break

    consider_ft_title_index()
    consider_ft_dynamic_syndication()

    if (
        enable_arquivo_pt_fallback
        and item.publisher in ARQUIVO_PT_FALLBACK_PUBLISHERS
        and (best_response is None or best_response[5] < 100)
    ):
        try:
            arquivo_pt_candidates = discover_arquivo_pt_candidates(
                item,
                archive_client=archive_client,
            )
        except Exception as exc:
            failures.append(f"arquivo-pt-index:{type(exc).__name__}")
            arquivo_pt_candidates = ()
        existing_urls = {
            candidate.snapshot_url for candidate in candidates_considered
        }
        arquivo_pt_candidates = tuple(
            candidate
            for candidate in arquivo_pt_candidates
            if candidate.snapshot_url not in existing_urls
        )
        candidates_considered.extend(arquivo_pt_candidates)
        consider_candidates(arquivo_pt_candidates)

    if best_response is not None:
        (
            candidate,
            status_code,
            content,
            final_url,
            content_type,
            quality_score,
            signals,
        ) = best_response
        raw_reference = store_raw_html(output_dir, content)
        retrieved_at = datetime.now(timezone.utc)
        selected_candidate = resolved_capture_candidate(
            candidate,
            final_url=final_url,
            http_status=status_code,
            content_type=content_type,
            byte_count=len(content),
        )
        capture = RawCapture(
            article_id=item.article_id,
            publisher=item.publisher,
            canonical_url=item.canonical_url,
            published_at=item.published_at,
            section=item.section,
            selected_candidate=selected_candidate,
            candidates_considered=candidates_considered,
            retrieved_at=retrieved_at,
            final_url=final_url,
            http_status=status_code,
            content_type=content_type or "text/html",
            representation=(
                CaptureRepresentation.DERIVED_HTML
                if candidate.provider == CaptureProvider.INFINI_NEWS
                else CaptureRepresentation.RAW_HTML
            ),
            quality_score=quality_score,
            quality_signals=signals,
            raw_html=raw_reference,
        )
        record_path = store_capture_record(output_dir, capture)
        return {
            "canonicalUrl": item.canonical_url,
            "status": "complete",
            "capture": capture,
            "recordPath": record_path,
            "error": None,
        }
    return {
        "canonicalUrl": item.canonical_url,
        "status": "error",
        "capture": None,
        "recordPath": None,
        "error": "; ".join(failures[-8:]) or "no usable capture candidates",
    }


def _fetch_infini_news_candidate(
    candidate: CaptureCandidate,
    *,
    archive_client: ArchiveClient,
    maximum_html_bytes: int,
    canonical_url: str,
) -> tuple[int, dict[str, str], bytes, str]:
    parsed = urlsplit(candidate.snapshot_url)
    expected_endpoint = urlsplit(INFINI_DATASET_ROWS_ENDPOINT)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_endpoint.hostname
        or parsed.path != expected_endpoint.path
        or query.get("dataset") != [INFINI_DATASET]
        or query.get("split") != ["train"]
        or query.get("length") != ["1"]
        or len(query.get("config", [])) != 1
        or re.fullmatch(r"year_\d{4}", query["config"][0]) is None
        or len(query.get("offset", [])) != 1
        or re.fullmatch(r"\d+", query["offset"][0]) is None
        or not candidate.source_url
    ):
        raise ValueError("invalid Infini-News dataset row candidate")
    status_code, headers, payload, _ = _fetch_limited_archive(
        archive_client,
        candidate.snapshot_url,
        maximum_bytes=max(maximum_html_bytes, 2_000_000),
        attempts=2,
        timeout=45.0,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not payload:
        raise ValueError(f"Infini-News row returned HTTP {status_code}")
    if "json" not in content_type and not payload.lstrip().startswith(b"{"):
        raise ValueError("Infini-News row did not return JSON")
    decoded = json.loads(payload)
    rows = decoded.get("rows") if isinstance(decoded, dict) else None
    row_wrapper = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
    row = row_wrapper.get("row") if isinstance(row_wrapper, dict) else None
    if not isinstance(row, dict):
        raise ValueError("Infini-News row response is invalid")
    expected_index = int(query["offset"][0])
    if row_wrapper.get("row_idx") != expected_index:
        raise ValueError("Infini-News row index mismatch")
    expected_year = int(query["config"][0].removeprefix("year_"))
    if row.get("year") != expected_year:
        raise ValueError("Infini-News row year mismatch")
    source_url = str(row.get("url") or "").strip()
    if not _same_article_url(source_url, candidate.source_url):
        raise ValueError("Infini-News source URL mismatch")
    expected_headline = candidate.expected_headline or ""
    headline = str(row.get("title") or "").strip()
    if (
        len(_significant_tokens(expected_headline)) < 4
        or _headline_text_overlap(expected_headline, headline) < 0.8
    ):
        raise ValueError("Infini-News headline mismatch")
    text = str(row.get("text") or "").strip()
    minimum_body_characters = (
        1_000
        if _is_ft_origin_url(candidate.source_url)
        else FT_SYNDICATION_MINIMUM_BODY_CHARACTERS
    )
    if len(text) < minimum_body_characters:
        raise ValueError("Infini-News document body is too short")
    warc_filename = str(row.get("warc_filename") or "").strip()
    if not candidate.warc_filename:
        raise ValueError("Infini-News candidate WARC provenance is missing")
    if warc_filename != candidate.warc_filename:
        raise ValueError("Infini-News WARC provenance mismatch")
    if (
        not warc_filename.startswith("CC-NEWS-")
        or not warc_filename.endswith(".warc.gz")
    ):
        raise ValueError("Infini-News WARC provenance is missing")
    derived_html = _infini_news_derived_html(
        row,
        canonical_url=canonical_url,
        source_url=source_url,
        headline=headline,
        text=text,
    )
    if len(derived_html) > maximum_html_bytes:
        raise ValueError("Infini-News derived HTML exceeds the capture limit")
    return (
        200,
        {"content-type": "text/html; charset=utf-8"},
        derived_html,
        source_url,
    )


def _infini_news_derived_html(
    row: dict[str, object],
    *,
    canonical_url: str,
    source_url: str,
    headline: str,
    text: str,
) -> bytes:
    published_at = str(
        row.get("publish_date") or row.get("date") or ""
    ).strip()
    author = str(row.get("author") or "").strip()
    description = str(row.get("description") or "").strip()
    structured = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "url": canonical_url,
        "mainEntityOfPage": canonical_url,
        "isBasedOn": source_url,
        "headline": headline,
        **({"datePublished": published_at} if published_at else {}),
        **({"author": {"@type": "Person", "name": author}} if author else {}),
        **({"description": description} if description else {}),
    }
    structured_json = json.dumps(
        structured,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    body_html = "".join(f"<p>{escape(line)}</p>" for line in paragraphs)
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{escape(headline)}</title>"
        f"<link rel=\"canonical\" href=\"{escape(canonical_url, quote=True)}\">"
        f"<link rel=\"alternate\" href=\"{escape(source_url, quote=True)}\">"
        f"<script type=\"application/ld+json\">{structured_json}</script>"
        "</head><body>"
        f"<article data-jojo-representation=\"derived-infini-news\">"
        f"<h1>{escape(headline)}</h1>{body_html}</article>"
        "</body></html>"
    )
    return html.encode("utf-8")


def _fetch_usable_candidate(
    candidate: CaptureCandidate,
    *,
    archive_client: ArchiveClient,
    maximum_html_bytes: int,
    canonical_url: str,
    publisher: str,
    response_observer: Callable[
        [CaptureCandidate, bytes, str],
        None,
    ]
    | None = None,
) -> tuple[
    tuple[
        CaptureCandidate,
        int,
        bytes,
        str,
        str,
        int,
        dict[str, object],
    ]
    | None,
    str | None,
]:
    transport_signals: dict[str, object] = {}
    try:
        if candidate.provider == CaptureProvider.COMMON_CRAWL:
            status_code, headers, content, final_url = (
                fetch_common_crawl_candidate(
                    candidate,
                    archive_client=archive_client,
                    maximum_html_bytes=maximum_html_bytes,
                )
            )
        elif candidate.provider == CaptureProvider.INFINI_NEWS:
            if publisher != "ft":
                raise ValueError("Infini-News derived capture is FT-only")
            status_code, headers, content, final_url = (
                _fetch_infini_news_candidate(
                    candidate,
                    archive_client=archive_client,
                    maximum_html_bytes=maximum_html_bytes,
                    canonical_url=canonical_url,
                )
            )
        elif (
            candidate.provider == CaptureProvider.OTHER
            and is_ghostarchive_candidate_url(candidate.snapshot_url)
        ):
            (
                status_code,
                headers,
                content,
                final_url,
                transport_signals,
            ) = fetch_ghostarchive_candidate(
                candidate,
                canonical_url=canonical_url,
                archive_client=archive_client,
                maximum_html_bytes=maximum_html_bytes,
            )
        elif (
            candidate.provider == CaptureProvider.WAYBACK
            and (urlsplit(canonical_url).hostname or "").casefold().endswith(
                "ft.com"
            )
        ):
            status_code, headers, content, final_url = _fetch_limited_archive(
                archive_client,
                candidate.snapshot_url,
                maximum_bytes=maximum_html_bytes,
                attempts=2,
                timeout=30.0,
            )
        else:
            status_code, headers, content, final_url = archive_client.fetch(
                candidate.snapshot_url,
                maximum_bytes=maximum_html_bytes,
            )
    except Exception as exc:
        return None, f"{candidate.provider.value}:{type(exc).__name__}"
    if (
        candidate.provider == CaptureProvider.COMMON_CRAWL
        and not _same_article_url(final_url, canonical_url)
    ):
        return None, "commoncrawl:target-mismatch"
    if (
        candidate.provider == CaptureProvider.ARQUIVO_PT
        and not _arquivo_pt_replay_matches(final_url, canonical_url)
    ):
        return None, "arquivo-pt:target-mismatch"
    content_type = headers.get("content-type", "").split(";", 1)[0].strip()
    quality_score, signals = score_raw_capture(
        content,
        http_status=status_code,
        content_type=content_type,
        final_url=final_url,
    )
    signals = signals | transport_signals
    if response_observer is not None:
        response_observer(candidate, content, final_url)
    structured_subscription_article = bool(
        signals["subscriptionShell"]
        and _structured_subscription_article_usable(
            content,
            publisher=publisher,
            canonical_url=canonical_url,
        )
    )
    if structured_subscription_article:
        quality_score = min(100, quality_score + 60)
        signals = signals | {
            "structuredSubscriptionArticle": True,
        }
    if (
        status_code not in ACCEPTED_HTTP_STATUSES
        or not content
        or not signals["looksLikeHtml"]
        or signals["archiveErrorPage"]
        or signals["authenticationShell"]
        or signals["accessChallengeShell"]
        or (
            signals["subscriptionShell"]
            and not structured_subscription_article
        )
        or signals["ftTruncatedArticleShell"]
        or signals["redirectShell"]
    ):
        return (
            None,
            f"{candidate.provider.value}:http-{status_code}:score-{quality_score}",
        )
    if candidate.provider == CaptureProvider.COMMON_CRAWL:
        signals = signals | {
            "commonCrawlWarcValidated": True,
            "commonCrawlWarcFilename": candidate.warc_filename,
            "commonCrawlWarcOffset": candidate.warc_offset,
            "commonCrawlWarcLength": candidate.warc_length,
        }
    elif candidate.provider == CaptureProvider.ARQUIVO_PT:
        signals = signals | {
            "arquivoPtReplayValidated": True,
            "arquivoPtCapturedAt": (
                candidate.captured_at.isoformat()
                if candidate.captured_at is not None
                else None
            ),
        }
    elif candidate.provider == CaptureProvider.INFINI_NEWS:
        signals = signals | {
            "infiniNewsValidated": True,
            "infiniNewsDerivedHtml": True,
            "infiniNewsDatasetRowUrl": candidate.snapshot_url,
            "infiniNewsSourceUrl": candidate.source_url,
            "infiniNewsWarcFilename": candidate.warc_filename,
            "infiniNewsDerivedHtmlSha256": hashlib.sha256(content).hexdigest(),
        }
    return (
        (
            candidate,
            status_code,
            content,
            final_url,
            content_type,
            quality_score,
            signals,
        ),
        None,
    )


def arquivo_pt_cdx_url(item: ManifestItem) -> str:
    return ARQUIVO_PT_CDX_ENDPOINT + "?" + urlencode(
        [
            ("url", item.canonical_url),
            ("output", "json"),
            ("filter", "=status:200"),
            ("filter", "=mime:text/html"),
            ("collapse", "digest"),
        ]
    )


def discover_arquivo_pt_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    maximum_candidates: int = ARQUIVO_PT_MAXIMUM_CANDIDATES,
) -> tuple[CaptureCandidate, ...]:
    if maximum_candidates < 1:
        raise ValueError("maximum_candidates must be positive")
    query_url = arquivo_pt_cdx_url(item)
    status_code, headers, content, _ = _fetch_limited_archive(
        archive_client,
        query_url,
        maximum_bytes=ARQUIVO_PT_INDEX_MAXIMUM_BYTES,
        attempts=2,
        timeout=30.0,
    )
    if status_code == 404 or not content:
        return ()
    if status_code != 200:
        raise ValueError(f"Arquivo.pt CDX returned HTTP {status_code}")
    content_type = headers.get("content-type", "").casefold()
    if "json" not in content_type and not content.lstrip().startswith(b"{"):
        raise ValueError("Arquivo.pt CDX did not return NDJSON")

    candidates: list[CaptureCandidate] = []
    seen: set[str] = set()
    for line in content.splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        original = str(row.get("url") or row.get("original") or "").strip()
        timestamp = str(row.get("timestamp") or "").strip()
        mime_type = str(
            row.get("mime") or row.get("mimetype") or ""
        ).strip()
        archived_status = str(
            row.get("status") or row.get("statuscode") or ""
        ).strip()
        if (
            not _same_article_url(original, item.canonical_url)
            or not re.fullmatch(r"\d{14}", timestamp)
            or archived_status != "200"
            or mime_type.casefold() != "text/html"
        ):
            continue
        digest = _optional_string(row.get("digest"))
        deduplication_key = digest or f"{timestamp}:{original}"
        if deduplication_key in seen:
            continue
        seen.add(deduplication_key)
        candidates.append(
            CaptureCandidate(
                provider=CaptureProvider.ARQUIVO_PT,
                snapshot_url=(
                    f"{ARQUIVO_PT_REPLAY_ENDPOINT}/{timestamp}/{original}"
                ),
                captured_at=_wayback_datetime(timestamp),
                digest=digest,
                mime_type=mime_type,
                status_code=200,
                byte_count=_optional_int(row.get("length")),
            )
        )
    candidates.sort(
        key=lambda candidate: _timemap_candidate_sort_key(
            candidate,
            published_at=item.published_at,
        )
    )
    return tuple(candidates[:maximum_candidates])


def discover_wayback_timemap_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    maximum_candidates: int = WAYBACK_TIMEMAP_MAXIMUM_CANDIDATES,
) -> tuple[CaptureCandidate, ...]:
    if maximum_candidates < 1:
        raise ValueError("maximum_candidates must be positive")
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?" + urlencode(
        {"url": item.canonical_url}
    )
    status_code, headers, content, _ = _fetch_limited_archive(
        archive_client,
        timemap_url,
        maximum_bytes=WAYBACK_TIMEMAP_MAXIMUM_BYTES,
        attempts=2,
        timeout=35.0,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not content:
        raise ValueError(f"Wayback timemap returned HTTP {status_code}")
    if "json" not in content_type and not content.lstrip().startswith(b"["):
        raise ValueError("Wayback timemap did not return JSON")
    payload = json.loads(content)
    if not isinstance(payload, list) or not payload:
        return ()
    header = payload[0]
    if not isinstance(header, list):
        raise ValueError("Wayback timemap header is invalid")
    columns = {str(value).casefold(): index for index, value in enumerate(header)}
    required = {"timestamp", "original", "mimetype", "statuscode"}
    if not required.issubset(columns):
        raise ValueError("Wayback timemap is missing required columns")

    candidates: list[CaptureCandidate] = []
    seen: set[str] = set()
    for row in payload[1:]:
        if not isinstance(row, list):
            continue
        timestamp = _timemap_value(row, columns, "timestamp")
        original = _timemap_value(row, columns, "original")
        mime_type = _timemap_value(row, columns, "mimetype")
        status = _optional_int(_timemap_value(row, columns, "statuscode"))
        if (
            _wayback_datetime(timestamp) is None
            or status != 200
            or mime_type.casefold() != "text/html"
            or not _same_article_url(
                original,
                item.canonical_url,
            )
        ):
            continue
        digest = _optional_string(_timemap_value(row, columns, "digest"))
        deduplication_key = digest or f"{timestamp}:{original}"
        if deduplication_key in seen:
            continue
        seen.add(deduplication_key)
        candidates.append(
            CaptureCandidate(
                provider=CaptureProvider.WAYBACK,
                snapshot_url=(
                    f"https://web.archive.org/web/{timestamp}id_/{original}"
                ),
                captured_at=_wayback_datetime(timestamp),
                digest=digest,
                mime_type=mime_type,
                status_code=status,
                byte_count=_optional_int(
                    _timemap_value(row, columns, "length")
                ),
            )
        )

    candidates.sort(
        key=lambda candidate: _timemap_candidate_sort_key(
            candidate,
            published_at=item.published_at,
        )
    )
    return tuple(candidates[:maximum_candidates])


def reuters_syndication_search_url(item: ManifestItem) -> str:
    return _syndication_search_url(item, publisher_label="Reuters")


def reuters_syndication_title_search_url(expected_headline: str) -> str:
    return REUTERS_SYNDICATION_SEARCH_ENDPOINT + "?" + urlencode(
        {"p": f'"{expected_headline}" Reuters'}
    )


def bloomberg_syndication_search_url(item: ManifestItem) -> str:
    return _syndication_search_url(item, publisher_label="Bloomberg")


def ft_syndication_search_url(item: ManifestItem) -> str:
    return REUTERS_SYNDICATION_SEARCH_ENDPOINT + "?" + urlencode(
        {"p": item.canonical_url}
    )


def ft_syndication_title_search_url(expected_headline: str) -> str:
    return REUTERS_SYNDICATION_SEARCH_ENDPOINT + "?" + urlencode(
        {"p": f'"{expected_headline}" "Financial Times"'}
    )


def ft_syndication_broad_title_search_url(
    expected_headline: str,
) -> str:
    return REUTERS_SYNDICATION_SEARCH_ENDPOINT + "?" + urlencode(
        {"p": expected_headline}
    )


def ft_google_news_headline_search_url(item: ManifestItem) -> str:
    article_identifier = (
        urlsplit(item.canonical_url).path.rstrip("/").rsplit("/", 1)[-1]
    )
    return FT_GOOGLE_NEWS_RSS_ENDPOINT + "?" + urlencode(
        {
            "q": f'"{article_identifier}"',
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )


def ft_google_news_partner_search_url(
    expected_headline: str,
) -> str:
    return FT_GOOGLE_NEWS_RSS_ENDPOINT + "?" + urlencode(
        {
            "q": f'"{expected_headline}"',
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
    )


def ft_syndication_partner_site_search_url(
    expected_headline: str,
    source_host: str,
) -> str:
    return REUTERS_SYNDICATION_SEARCH_ENDPOINT + "?" + urlencode(
        {"p": f'"{expected_headline}" site:{source_host}'}
    )


def _discover_ft_known_partner_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    expected_headline: str,
) -> tuple[CaptureCandidate, ...]:
    expected_date = _parse_iso_datetime(item.published_at)
    if expected_date is None or expected_date.year != 2026:
        return ()
    slug = _headline_slug(expected_headline)
    if not slug:
        return ()
    partner_url = _load_ft_known_partner_urls(archive_client).get(slug)
    if not partner_url:
        return ()
    return (
        CaptureCandidate(
            provider=CaptureProvider.OTHER,
            snapshot_url=partner_url,
            expected_headline=expected_headline,
        ),
    )


def _load_ft_known_partner_urls(
    archive_client: ArchiveClient,
) -> dict[str, str]:
    global _ft_known_partner_urls
    with _ft_known_partner_urls_lock:
        if _ft_known_partner_urls is not None:
            return _ft_known_partner_urls
        discovered: dict[str, str] = {}
        for public_origin, sitemap_url in FT_KNOWN_PARTNER_SITEMAPS:
            try:
                status_code, headers, content, _ = archive_client.fetch(
                    sitemap_url,
                    maximum_bytes=REUTERS_SYNDICATION_SEARCH_MAXIMUM_BYTES,
                )
                content_type = headers.get("content-type", "").casefold()
                if (
                    status_code != 200
                    or not content
                    or (
                        "xml" not in content_type
                        and not content.lstrip().startswith(b"<?xml")
                    )
                ):
                    continue
                root = ElementTree.fromstring(content.lstrip())
            except Exception:
                continue
            for location in root.findall(".//{*}loc"):
                source_url = (location.text or "").strip()
                source_path = urlsplit(source_url).path.rstrip("/")
                prefix = "/resources/articles/"
                if not source_path.startswith(prefix):
                    continue
                candidate_slug = source_path.removeprefix(prefix)
                if not candidate_slug or "/" in candidate_slug:
                    continue
                discovered.setdefault(
                    candidate_slug.casefold(),
                    public_origin.rstrip("/") + source_path,
                )
        _ft_known_partner_urls = discovered
        return _ft_known_partner_urls


def _headline_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", errors="ignore").decode()
    return re.sub(
        r"[^a-z0-9]+",
        "-",
        ascii_value.casefold(),
    ).strip("-")


def _syndication_search_url(
    item: ManifestItem,
    *,
    publisher_label: str,
) -> str:
    parsed = urlsplit(item.canonical_url)
    slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if item.publisher == "reuters":
        slug = re.sub(r"-20\d{2}-\d{2}-\d{2}$", "", slug)
    words = " ".join(part for part in slug.split("-") if part)
    query = f"{words} {publisher_label}"
    return REUTERS_SYNDICATION_SEARCH_ENDPOINT + "?" + urlencode({"p": query})


def discover_reuters_syndication_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
) -> tuple[CaptureCandidate, ...]:
    initial_results = _fetch_syndication_search_results(
        item,
        archive_client=archive_client,
        search_url=reuters_syndication_search_url(item),
    )
    expected_headline = next(
        (
            title
            for _, title, candidate_url in initial_results
            if title and _same_article_url(candidate_url, item.canonical_url)
        ),
        None,
    )
    all_results = list(initial_results)
    if (
        expected_headline
        and len(_significant_tokens(expected_headline)) >= 4
    ):
        try:
            title_results = _fetch_syndication_search_results(
                item,
                archive_client=archive_client,
                search_url=reuters_syndication_title_search_url(
                    expected_headline
                ),
            )
        except ValueError:
            title_results = []
        if title_results:
            offset = len(all_results)
            all_results.extend(
                (offset + position, title, candidate_url)
                for position, title, candidate_url in title_results
            )
    return _rank_syndication_candidates(
        all_results,
        excluded_publisher="reuters",
        expected_headline=expected_headline,
    )


def discover_bloomberg_syndication_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
) -> tuple[CaptureCandidate, ...]:
    return _discover_syndication_candidates(
        item,
        archive_client=archive_client,
        search_url=bloomberg_syndication_search_url(item),
        excluded_publisher="bloomberg",
    )


def discover_ft_syndication_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    expected_headline: str | None = None,
    skip_title_search: bool = False,
    exhaustive: bool = False,
) -> tuple[CaptureCandidate, ...]:
    initial_results: list[tuple[int, str, str]] = []
    if not expected_headline:
        try:
            initial_results = _fetch_syndication_search_results(
                item,
                archive_client=archive_client,
                search_url=ft_syndication_search_url(item),
            )
        except Exception:
            initial_results = []
        expected_headline = next(
            (
                title
                for _, title, candidate_url in initial_results
                if title
                and _same_article_url(candidate_url, item.canonical_url)
            ),
            None,
        )
    if not expected_headline:
        try:
            expected_headline = (
                _discover_ft_headline_from_google_news(
                    item,
                    archive_client=archive_client,
                )
            )
        except Exception:
            expected_headline = None
    if (
        not expected_headline
        or len(_significant_tokens(expected_headline)) < 4
    ):
        return ()
    title_results: list[tuple[int, str, str]] = []
    if not skip_title_search:
        try:
            title_results = _fetch_syndication_search_results(
                item,
                archive_client=archive_client,
                search_url=ft_syndication_title_search_url(
                    expected_headline
                ),
            )
        except ValueError:
            title_results = []
    offset = len(initial_results)
    all_results = initial_results + [
        (offset + position, title, candidate_url)
        for position, title, candidate_url in title_results
    ]
    ranked = _rank_syndication_candidates(
        all_results,
        excluded_publisher="ft",
        expected_headline=expected_headline,
    )
    if ranked and not exhaustive:
        return ranked
    try:
        broad_results = _fetch_syndication_search_results(
            item,
            archive_client=archive_client,
            search_url=ft_syndication_broad_title_search_url(
                expected_headline
            ),
        )
    except Exception:
        broad_results = []
    offset = len(all_results)
    all_results.extend(
        (offset + position, title, candidate_url)
        for position, title, candidate_url in broad_results
    )
    ranked = _rank_syndication_candidates(
        all_results,
        excluded_publisher="ft",
        expected_headline=expected_headline,
    )
    if ranked and not exhaustive:
        return ranked
    try:
        google_news_ranked = (
            _discover_ft_partner_candidates_from_google_news(
                item,
                archive_client=archive_client,
                expected_headline=expected_headline,
            )
        )
    except Exception:
        google_news_ranked = ()
    if not exhaustive:
        return google_news_ranked
    try:
        known_partner_ranked = _discover_ft_known_partner_candidates(
            item,
            archive_client=archive_client,
            expected_headline=expected_headline,
        )
    except Exception:
        known_partner_ranked = ()
    combined: list[CaptureCandidate] = []
    seen_urls: set[str] = set()
    for candidate in (
        *known_partner_ranked,
        *google_news_ranked,
        *ranked,
    ):
        if candidate.snapshot_url in seen_urls:
            continue
        seen_urls.add(candidate.snapshot_url)
        combined.append(candidate)
    return tuple(combined)


def _discover_ft_partner_candidates_from_google_news(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    expected_headline: str,
) -> tuple[CaptureCandidate, ...]:
    expected_date = _parse_iso_datetime(item.published_at)
    if expected_date is None:
        return ()
    search_url = ft_google_news_partner_search_url(expected_headline)
    status_code, headers, content, _ = archive_client.fetch(
        search_url,
        maximum_bytes=REUTERS_SYNDICATION_SEARCH_MAXIMUM_BYTES,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not content:
        raise ValueError(
            f"FT Google News partner search returned HTTP {status_code}"
        )
    if (
        "xml" not in content_type
        and not content.lstrip().startswith((b"<?xml", b"<rss"))
    ):
        raise ValueError("FT Google News partner search did not return XML")
    root = ElementTree.fromstring(content.lstrip())
    source_hosts: list[str] = []
    seen_hosts: set[str] = set()
    for result in root.findall("./channel/item"):
        result_title = _clean_syndication_search_title(
            result.findtext("title") or ""
        )
        if (
            _headline_text_overlap(expected_headline, result_title)
            < 0.8
        ):
            continue
        try:
            published_at = parsedate_to_datetime(
                result.findtext("pubDate") or ""
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        if (
            abs(
                (
                    published_at.astimezone(timezone.utc).date()
                    - expected_date.date()
                ).days
            )
            > FT_GOOGLE_NEWS_MAXIMUM_DATE_DELTA_DAYS
        ):
            continue
        source = result.find("source")
        source_url = (
            source.attrib.get("url", "").strip()
            if source is not None
            else ""
        )
        source_host = (urlsplit(source_url).hostname or "").casefold()
        if (
            not source_host
            or source_host in seen_hosts
            or not _is_public_syndication_url(
                source_url,
                excluded_publisher="ft",
            )
        ):
            continue
        seen_hosts.add(source_host)
        source_hosts.append(source_host)
        if (
            len(source_hosts)
            >= FT_GOOGLE_NEWS_MAXIMUM_PARTNER_SOURCES
        ):
            break
    all_results: list[tuple[int, str, str]] = []
    for source_host in source_hosts:
        try:
            results = _fetch_syndication_search_results(
                item,
                archive_client=archive_client,
                search_url=ft_syndication_partner_site_search_url(
                    expected_headline,
                    source_host,
                ),
            )
        except Exception:
            continue
        offset = len(all_results)
        all_results.extend(
            (offset + position, title, candidate_url)
            for position, title, candidate_url in results
        )
    return _rank_syndication_candidates(
        all_results,
        excluded_publisher="ft",
        expected_headline=expected_headline,
    )


def _discover_ft_headline_from_google_news(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
) -> str | None:
    search_url = ft_google_news_headline_search_url(item)
    status_code, headers, content, _ = archive_client.fetch(
        search_url,
        maximum_bytes=REUTERS_SYNDICATION_SEARCH_MAXIMUM_BYTES,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not content:
        raise ValueError(
            f"FT Google News search returned HTTP {status_code}"
        )
    if (
        "xml" not in content_type
        and not content.lstrip().startswith((b"<?xml", b"<rss"))
    ):
        raise ValueError("FT Google News search did not return XML")
    root = ElementTree.fromstring(content.lstrip())
    expected_date = _parse_iso_datetime(item.published_at)
    if expected_date is None:
        return None
    ranked: list[tuple[int, int, str]] = []
    for position, result in enumerate(root.findall("./channel/item")):
        source = result.find("source")
        source_name = (source.text or "").strip() if source is not None else ""
        source_url = (
            source.attrib.get("url", "").strip()
            if source is not None
            else ""
        )
        source_host = (urlsplit(source_url).hostname or "").casefold()
        if (
            source_name.casefold() != "financial times"
            and source_host not in {"ft.com", "www.ft.com"}
        ):
            continue
        try:
            published_at = parsedate_to_datetime(
                result.findtext("pubDate") or ""
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        date_delta = abs(
            (
                published_at.astimezone(timezone.utc).date()
                - expected_date.date()
            ).days
        )
        if date_delta > 2:
            continue
        cleaned = _clean_syndication_search_title(
            result.findtext("title") or ""
        )
        if len(_significant_tokens(cleaned)) < 4:
            continue
        ranked.append((date_delta, position, cleaned))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][2]


def discover_nyt_syndication(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
) -> NytSyndicationDiscovery:
    for endpoint in NYT_TRUSTED_WORDPRESS_ENDPOINTS:
        try:
            trusted = _discover_nyt_trusted_wordpress_copy(
                item,
                endpoint=endpoint,
                archive_client=archive_client,
            )
        except Exception:
            trusted = None
        if trusted is not None:
            return trusted

    canonical_search_url = nyt_syndication_search_url(item)
    status_code, headers, content, _ = archive_client.fetch(
        canonical_search_url,
        maximum_bytes=NYT_SYNDICATION_SEARCH_MAXIMUM_BYTES,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not content:
        raise ValueError(
            f"NYT syndication search returned HTTP {status_code}"
        )
    if "html" not in content_type and b"<html" not in content[:1_000].lower():
        raise ValueError("NYT syndication search did not return HTML")

    soup = BeautifulSoup(content, "html.parser")
    expected_headline: str | None = None
    initial_results = _yahoo_search_results(soup)
    for _, result_title, candidate_url in initial_results:
        if _same_article_url(candidate_url, item.canonical_url):
            if result_title:
                expected_headline = result_title
            break

    if (
        not expected_headline
        or len(_significant_tokens(expected_headline)) < 4
    ):
        return NytSyndicationDiscovery(
            expected_headline=None,
            candidates=(),
        )

    for endpoint in NYT_HEADLINE_WORDPRESS_ENDPOINTS:
        try:
            trusted = _discover_nyt_headline_wordpress_copy(
                item,
                expected_headline=expected_headline,
                endpoint=endpoint,
                archive_client=archive_client,
            )
        except Exception:
            trusted = None
        if trusted is not None:
            return trusted

    title_search_url = nyt_syndication_title_search_url(expected_headline)
    status_code, headers, content, _ = archive_client.fetch(
        title_search_url,
        maximum_bytes=NYT_SYNDICATION_SEARCH_MAXIMUM_BYTES,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not content:
        raise ValueError(
            f"NYT title search returned HTTP {status_code}"
        )
    if "html" not in content_type and b"<html" not in content[:1_000].lower():
        raise ValueError("NYT title search did not return HTML")
    title_results = _yahoo_search_results(
        BeautifulSoup(content, "html.parser")
    )

    ranked: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for position, result_title, candidate_url in (
        initial_results + title_results
    ):
        if (
            candidate_url in seen
            or not _is_public_syndication_url(
                candidate_url,
                excluded_publisher="nyt",
            )
        ):
            continue
        title_overlap = _headline_text_overlap(
            expected_headline,
            result_title,
        )
        if title_overlap < 0.55:
            continue
        seen.add(candidate_url)
        ranked.append((-title_overlap, position, candidate_url))
    ranked.sort()
    return NytSyndicationDiscovery(
        expected_headline=expected_headline,
        candidates=tuple(
            CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=candidate_url,
            )
            for _, _, candidate_url in ranked[
                :NYT_SYNDICATION_MAXIMUM_CANDIDATES
            ]
        ),
    )


def _discover_nyt_trusted_wordpress_copy(
    item: ManifestItem,
    *,
    endpoint: str,
    archive_client: ArchiveClient,
) -> NytSyndicationDiscovery | None:
    search_url = nyt_trusted_wordpress_search_url(
        item,
        endpoint=endpoint,
    )
    status_code, headers, content, _ = _fetch_limited_archive(
        archive_client,
        search_url,
        maximum_bytes=NYT_SYNDICATION_SEARCH_MAXIMUM_BYTES,
        attempts=2,
        timeout=35.0,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not content:
        raise ValueError(
            f"trusted NYT syndication search returned HTTP {status_code}"
        )
    if "json" not in content_type and not content.lstrip().startswith(b"["):
        raise ValueError(
            "trusted NYT syndication search did not return JSON"
        )
    payload = json.loads(content)
    if not isinstance(payload, list):
        raise ValueError("trusted NYT syndication response is invalid")
    expected_date = _parse_iso_datetime(item.published_at)
    for row in payload:
        if not isinstance(row, dict):
            continue
        link = row.get("link")
        title_value = row.get("title")
        content_value = row.get("content")
        date_value = row.get("date_gmt") or row.get("date")
        if (
            not isinstance(link, str)
            or not _is_public_syndication_url(
                link,
                excluded_publisher="nyt",
            )
            or not isinstance(title_value, dict)
            or not isinstance(content_value, dict)
        ):
            continue
        rendered_title = title_value.get("rendered")
        rendered_content = content_value.get("rendered")
        if (
            not isinstance(rendered_title, str)
            or not isinstance(rendered_content, str)
            or not _html_links_to_article(
                rendered_content,
                item.canonical_url,
            )
        ):
            continue
        partner_date = _parse_iso_datetime(
            date_value if isinstance(date_value, str) else None
        )
        if (
            expected_date is not None
            and partner_date is not None
            and abs((partner_date.date() - expected_date.date()).days) > 2
        ):
            continue
        expected_headline = _clean_nyt_search_result_title(
            BeautifulSoup(
                rendered_title,
                "html.parser",
            ).get_text(" ", strip=True)
        )
        if len(_significant_tokens(expected_headline)) < 4:
            continue
        return NytSyndicationDiscovery(
            expected_headline=expected_headline,
            candidates=(
                CaptureCandidate(
                    provider=CaptureProvider.OTHER,
                    snapshot_url=link,
                ),
            ),
        )
    return None


def _structured_subscription_article_usable(
    content: bytes,
    *,
    publisher: str,
    canonical_url: str,
    raw_capture: RawCapture | None = None,
) -> bool:
    if publisher != "wsj":
        return False
    from .news_parser import parse_article

    try:
        article = parse_article(
            content,
            publisher=publisher,
            canonical_url=canonical_url,
            raw_capture=raw_capture,
        )
    except Exception:
        return False
    prefix = article.plain_text[:1_500].casefold()
    suspected_paywall = (
        article.quality.body_characters < 1_000
        and any(
            phrase in prefix
            for phrase in (
                "subscribe to read",
                "subscribe to continue",
                "sign in to continue",
                "already a subscriber",
                "unlock this article",
            )
        )
    )
    return bool(
        article.quality.status.value == "complete"
        and article.headline
        and article.quality.body_characters >= 100
        and not suspected_paywall
    )


def nyt_trusted_wordpress_search_url(
    item: ManifestItem,
    *,
    endpoint: str = NYT_TRUSTED_WORDPRESS_ENDPOINTS[0],
) -> str:
    slug = urlsplit(item.canonical_url).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.html$", "", slug, flags=re.IGNORECASE)
    query = " ".join(part for part in slug.split("-") if part)
    return endpoint + "?" + urlencode(
        {
            "search": query,
            "per_page": 10,
            "_fields": "date,date_gmt,link,title,content",
        }
    )


def _discover_nyt_headline_wordpress_copy(
    item: ManifestItem,
    *,
    expected_headline: str,
    endpoint: str,
    archive_client: ArchiveClient,
) -> NytSyndicationDiscovery | None:
    search_url = nyt_headline_wordpress_search_url(
        expected_headline,
        endpoint=endpoint,
    )
    status_code, headers, content, _ = _fetch_limited_archive(
        archive_client,
        search_url,
        maximum_bytes=NYT_SYNDICATION_SEARCH_MAXIMUM_BYTES,
        attempts=2,
        timeout=35.0,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not content:
        raise ValueError(
            f"headline NYT syndication search returned HTTP {status_code}"
        )
    if "json" not in content_type and not content.lstrip().startswith(b"["):
        raise ValueError(
            "headline NYT syndication search did not return JSON"
        )
    payload = json.loads(content)
    if not isinstance(payload, list):
        raise ValueError("headline NYT syndication response is invalid")
    expected_date = _parse_iso_datetime(item.published_at)
    if expected_date is None:
        return None
    ranked: list[tuple[float, int, int, str]] = []
    for position, row in enumerate(payload):
        if not isinstance(row, dict):
            continue
        link = row.get("link")
        title_value = row.get("title")
        content_value = row.get("content")
        date_value = row.get("date_gmt") or row.get("date")
        if (
            not isinstance(link, str)
            or not _is_public_syndication_url(
                link,
                excluded_publisher="nyt",
            )
            or not isinstance(title_value, dict)
            or not isinstance(content_value, dict)
            or not isinstance(date_value, str)
        ):
            continue
        rendered_title = title_value.get("rendered")
        rendered_content = content_value.get("rendered")
        if (
            not isinstance(rendered_title, str)
            or not isinstance(rendered_content, str)
        ):
            continue
        candidate_headline = _clean_nyt_search_result_title(
            BeautifulSoup(
                rendered_title,
                "html.parser",
            ).get_text(" ", strip=True)
        )
        headline_overlap = _headline_text_overlap(
            expected_headline,
            candidate_headline,
        )
        if headline_overlap < 0.8:
            continue
        partner_date = _parse_iso_datetime(date_value)
        if partner_date is None:
            continue
        date_delta = abs(
            (partner_date.date() - expected_date.date()).days
        )
        if date_delta > 2:
            continue
        rendered_soup = BeautifulSoup(rendered_content, "html.parser")
        attribution = rendered_soup.get_text(" ", strip=True)
        if re.search(
            r"(?i)(?:the\s+)?new\s+york\s+times|nytimes\.com",
            attribution,
        ) is None:
            continue
        canonical_linked = _html_links_to_article(
            rendered_content,
            item.canonical_url,
        )
        ranked.append(
            (
                -headline_overlap,
                0 if canonical_linked else 1,
                date_delta * 100 + position,
                link,
            )
        )
    if not ranked:
        return None
    _, _, _, link = min(ranked)
    return NytSyndicationDiscovery(
        expected_headline=expected_headline,
        candidates=(
            CaptureCandidate(
                provider=CaptureProvider.OTHER,
                snapshot_url=link,
                expected_headline=expected_headline,
            ),
        ),
    )


def nyt_headline_wordpress_search_url(
    expected_headline: str,
    *,
    endpoint: str = NYT_HEADLINE_WORDPRESS_ENDPOINTS[0],
) -> str:
    return endpoint + "?" + urlencode(
        {
            "search": expected_headline,
            "per_page": 10,
            "_fields": "date,date_gmt,link,title,content",
        }
    )


def nyt_syndication_search_url(item: ManifestItem) -> str:
    return NYT_SYNDICATION_SEARCH_ENDPOINT + "?" + urlencode(
        {"p": item.canonical_url}
    )


def nyt_syndication_title_search_url(expected_headline: str) -> str:
    return NYT_SYNDICATION_SEARCH_ENDPOINT + "?" + urlencode(
        {"p": expected_headline}
    )


def _yahoo_search_results(
    soup: BeautifulSoup,
) -> list[tuple[int, str, str]]:
    results: list[tuple[int, str, str]] = []
    for position, result in enumerate(soup.select("#web li")):
        anchor = (
            result.select_one(".compTitle > a")
            or result.select_one("h3 a")
            or result.select_one("a")
        )
        heading = result.select_one("h3")
        if anchor is None or heading is None:
            continue
        candidate_url = _decode_yahoo_search_result(anchor.get("href"))
        if candidate_url is None:
            continue
        result_title = _clean_nyt_search_result_title(
            heading.get_text(" ", strip=True)
        )
        results.append((position, result_title, candidate_url))
    return results


def _discover_syndication_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    search_url: str,
    excluded_publisher: str,
) -> tuple[CaptureCandidate, ...]:
    results = _fetch_syndication_search_results(
        item,
        archive_client=archive_client,
        search_url=search_url,
    )
    return _rank_syndication_candidates(
        results,
        excluded_publisher=excluded_publisher,
    )


def _fetch_syndication_search_results(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    search_url: str,
) -> list[tuple[int, str, str]]:
    status_code, headers, content, _ = archive_client.fetch(
        search_url,
        maximum_bytes=REUTERS_SYNDICATION_SEARCH_MAXIMUM_BYTES,
    )
    content_type = headers.get("content-type", "").casefold()
    if status_code != 200 or not content:
        raise ValueError(
            f"{item.publisher} syndication search returned HTTP {status_code}"
        )
    if "html" not in content_type and b"<html" not in content[:1_000].lower():
        raise ValueError(
            f"{item.publisher} syndication search did not return HTML"
        )

    soup = BeautifulSoup(content, "html.parser")
    results: list[tuple[int, str, str]] = []
    for position, result in enumerate(soup.select("#web li")):
        anchor = result.select_one("h3 a") or result.select_one("a")
        heading = result.select_one("h3")
        if anchor is None:
            continue
        candidate_url = _decode_yahoo_search_result(anchor.get("href"))
        if candidate_url is None:
            continue
        title = (
            _clean_syndication_search_title(
                heading.get_text(" ", strip=True)
            )
            if heading is not None
            else ""
        )
        results.append((position, title, candidate_url))
    return results


def _rank_syndication_candidates(
    results: Iterable[tuple[int, str, str]],
    *,
    excluded_publisher: str,
    expected_headline: str | None = None,
) -> tuple[CaptureCandidate, ...]:
    ranked: list[tuple[float, int, int, str]] = []
    seen: set[str] = set()
    for position, result_title, candidate_url in results:
        if excluded_publisher == "ft":
            candidate_url = _normalize_ft_syndication_candidate_url(
                candidate_url
            )
        if (
            candidate_url in seen
            or not _is_public_syndication_url(
                candidate_url,
                excluded_publisher=excluded_publisher,
            )
        ):
            continue
        headline_overlap = (
            _headline_text_overlap(expected_headline, result_title)
            if expected_headline and result_title
            else 0.0
        )
        if expected_headline and headline_overlap < 0.35:
            continue
        seen.add(candidate_url)
        ranked.append(
            (
                -headline_overlap,
                _reuters_syndication_url_priority(candidate_url),
                position,
                candidate_url,
            )
        )
    ranked.sort()
    return tuple(
        CaptureCandidate(
            provider=CaptureProvider.OTHER,
            snapshot_url=candidate_url,
            expected_headline=expected_headline,
        )
        for _, _, _, candidate_url in ranked[
            :REUTERS_SYNDICATION_MAXIMUM_CANDIDATES
        ]
    )


def _normalize_ft_syndication_candidate_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        host
        not in {
            "ftchinese.com",
            "www.ftchinese.com",
            "m.ftchinese.com",
            "cn.ft.com",
        }
        or not re.fullmatch(
            r"/interactive/\d+(?:/en)?/?",
            parsed.path,
            flags=re.IGNORECASE,
        )
    ):
        return value
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["full"] = ["y"]
    return urlunsplit(
        (
            "https",
            "m.ftchinese.com",
            parsed.path,
            urlencode(query, doseq=True),
            "",
        )
    )


def _clean_syndication_search_title(value: str) -> str:
    cleaned = re.sub(
        r"\s+(?:[-|]\s*)?"
        r"(?:(?:Reuters|Bloomberg)(?:\s+News)?|"
        r"Financial\s+Times|FT\.com)\s*$",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    ).strip()
    return re.sub(r"\s*(?:…|\.\.\.)\s*$", "", cleaned).strip()


def _decode_yahoo_search_result(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    match = re.search(r"/RU=([^/]+)/RK=", value)
    candidate_url = unquote(match.group(1)) if match else value
    parsed = urlsplit(candidate_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate_url


def _clean_nyt_search_result_title(value: str) -> str:
    cleaned = re.sub(
        r"\s+(?:[-|]\s*)?(?:The )?New York Times\s*$",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    ).strip()
    return re.sub(r"\s*(?:…|\.\.\.)\s*$", "", cleaned).strip()


def _html_links_to_article(html_value: str, canonical_url: str) -> bool:
    soup = BeautifulSoup(html_value, "html.parser")
    return any(
        _same_article_url(href, canonical_url)
        for anchor in soup.select("a[href]")
        if isinstance(href := anchor.get("href"), str)
    )


def _is_public_syndication_url(
    value: str,
    *,
    excluded_publisher: str,
) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
    ):
        return False
    try:
        if parsed.port not in {None, 80, 443}:
            return False
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return False
    excluded_domains = {
        "reuters": "reuters.com",
        "bloomberg": "bloomberg.com",
        "nyt": "nytimes.com",
        "ft": "ft.com",
    }
    excluded_domain = excluded_domains[excluded_publisher]
    if host == excluded_domain or host.endswith("." + excluded_domain):
        return False
    if (
        host in {"search.yahoo.com", "www.google.com", "www.bing.com"}
        or host.startswith("video.search.")
        or parsed.path.startswith(("/search", "/search/"))
    ):
        return False
    return True


def _reuters_syndication_url_priority(value: str) -> int:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if host == "yahoo.com" or host.endswith(".yahoo.com"):
        return 0
    if (
        "/wires/reuters/" in path
        or "/news/reuters" in path
        or "reuters.com" in path
    ):
        return 1
    return 2


def _validate_reuters_syndication_response(
    item: ManifestItem,
    *,
    expected_headline: str | None = None,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    try:
        article = parse_article(
            content,
            publisher="reuters",
            canonical_url=item.canonical_url,
            allow_generic_syndication=True,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "reutersSyndicationValidated": False,
        }
    headline_overlap = _reuters_syndication_headline_overlap(
        item.canonical_url,
        article.headline or "",
    )
    if expected_headline:
        headline_overlap = max(
            headline_overlap,
            _headline_text_overlap(
                expected_headline,
                article.headline or "",
            ),
        )
    author_text = " ".join(author.name for author in article.authors)
    attribution_text = (
        author_text + "\n" + article.plain_text[:1_000]
        + "\n" + article.plain_text[-1_000:]
    )
    attributed = re.search(
        r"(?i)(?:^|\W)reuters(?:\W|$)",
        attribution_text,
    ) is not None
    date_delta_days: int | None = None
    expected_date = _parse_iso_datetime(item.published_at)
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    date_matches = date_delta_days is not None and date_delta_days <= 2
    title_matches = headline_overlap >= 0.6 or (
        date_matches and headline_overlap >= 0.35
    )
    body_characters = article.quality.body_characters
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters >= REUTERS_SYNDICATION_MINIMUM_BODY_CHARACTERS
        and attributed
        and title_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < REUTERS_SYNDICATION_MINIMUM_BODY_CHARACTERS:
        reason = "body-too-short"
    elif not attributed:
        reason = "missing-reuters-attribution"
    elif not title_matches:
        reason = "headline-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "reutersSyndicationValidated": valid,
        "syndicationFinalUrl": final_url,
        "syndicationHeadlineOverlap": round(headline_overlap, 4),
        "syndicationBodyCharacters": body_characters,
        "syndicationReutersAttributed": attributed,
        "syndicationDateDeltaDays": date_delta_days,
        "syndicationExpectedHeadline": expected_headline,
    }


def _validate_bloomberg_syndication_response(
    item: ManifestItem,
    *,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    try:
        article = parse_article(
            content,
            publisher="bloomberg",
            canonical_url=item.canonical_url,
            allow_generic_syndication=True,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "bloombergSyndicationValidated": False,
        }
    headline_overlap = _syndication_headline_overlap(
        item.canonical_url,
        article.headline or "",
    )
    soup = BeautifulSoup(content, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    author_text = " ".join(author.name for author in article.authors)
    attribution_text = (
        author_text
        + "\n"
        + visible_text[:10_000]
        + "\n"
        + article.plain_text[-1_000:]
    )
    attributed = re.search(
        r"(?i)(?:^|\W)bloomberg(?:\s+(?:news|opinion))?(?:\W|$)",
        attribution_text,
    ) is not None
    expected_date = _parse_iso_datetime(item.published_at)
    date_delta_days: int | None = None
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    date_visible = _expected_date_visible(
        content,
        expected_date=expected_date,
    )
    date_matches = (
        date_delta_days is not None and date_delta_days <= 2
    ) or date_visible
    body_characters = article.quality.body_characters
    paywall_shell = _short_parsed_paywall_shell(
        body_characters=body_characters,
        plain_text=article.plain_text,
    )
    title_matches = headline_overlap >= 0.75
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters
        >= BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS
        and not paywall_shell
        and attributed
        and title_matches
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS:
        reason = "body-too-short"
    elif paywall_shell:
        reason = "suspected-paywall-shell"
    elif not attributed:
        reason = "missing-bloomberg-attribution"
    elif not title_matches:
        reason = "headline-mismatch"
    elif not date_matches:
        reason = "publication-date-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "bloombergSyndicationValidated": valid,
        "syndicationFinalUrl": final_url,
        "syndicationHeadlineOverlap": round(headline_overlap, 4),
        "syndicationBodyCharacters": body_characters,
        "syndicationPaywallShell": paywall_shell,
        "syndicationBloombergAttributed": attributed,
        "syndicationDateDeltaDays": date_delta_days,
        "syndicationExpectedDateVisible": date_visible,
    }


def _short_parsed_paywall_shell(
    *,
    body_characters: int,
    plain_text: str,
) -> bool:
    prefix = plain_text[:1_500].casefold()
    return bool(
        body_characters < _PARSED_PAYWALL_MAXIMUM_BODY_CHARACTERS
        and any(phrase in prefix for phrase in _PARSED_PAYWALL_PHRASES)
    )


def _validate_bloomberg_bnn_response(
    item: ManifestItem,
    *,
    expected_headline: str | None,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    partner_match = re.match(
        r"^https?://web\.archive\.org/web/\d{14}"
        r"(?:id_|im_|js_|cs_)?/(https?://.+)$",
        final_url,
        flags=re.IGNORECASE,
    )
    archived_partner_url = (
        unquote(partner_match.group(1)) if partner_match else ""
    )
    archived_partner = urlsplit(archived_partner_url)
    partner_validated = (
        archived_partner.scheme in {"http", "https"}
        and (archived_partner.hostname or "").casefold()
        in {"bnnbloomberg.ca", "www.bnnbloomberg.ca"}
    )
    if not partner_validated:
        return False, {
            "reason": "unexpected-bnn-archive-url",
            "bloombergBnnValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": False,
        }
    if not expected_headline:
        return False, {
            "reason": "missing-original-headline",
            "bloombergBnnValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": True,
        }
    try:
        article = parse_article(
            content,
            publisher="bloomberg",
            canonical_url=item.canonical_url,
            allow_generic_syndication=True,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "bloombergBnnValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": True,
        }
    soup = BeautifulSoup(content, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    author_text = " ".join(author.name for author in article.authors)
    attributed = re.search(
        r"(?i)(?:^|\W)bloomberg(?:\s+news)?(?:\W|$)",
        author_text + "\n" + visible_text[:5_000],
    ) is not None
    expected_date = _parse_iso_datetime(item.published_at)
    copyright_attributed = (
        expected_date is not None
        and re.search(
            rf"(?i)(?:©|\(c\)|copyright)\s*{expected_date.year}\s+"
            r"bloomberg\s+l\.p\.",
            visible_text,
        )
        is not None
    )
    decoded_html = content.decode(
        "utf-8",
        errors="ignore",
    ).replace("\\/", "/")
    canonical_linked = (
        item.canonical_url.rstrip("/").casefold()
        in decoded_html.casefold()
    )
    mirrored_slug_validated = _bnn_mirrored_slug_matches(
        archived_partner_url,
        item.canonical_url,
    )
    canonical_provenance_validated = (
        canonical_linked or mirrored_slug_validated
    )
    headline_overlap = _headline_text_overlap(
        expected_headline,
        article.headline or "",
    )
    date_delta_days: int | None = None
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    date_visible = _expected_date_visible(
        content,
        expected_date=expected_date,
    )
    date_matches = (
        date_delta_days is not None and date_delta_days <= 2
    ) or date_visible
    body_characters = article.quality.body_characters
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters
        >= BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS
        and attributed
        and copyright_attributed
        and canonical_provenance_validated
        and headline_overlap >= 0.8
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS:
        reason = "body-too-short"
    elif not attributed:
        reason = "missing-bloomberg-attribution"
    elif not copyright_attributed:
        reason = "missing-bloomberg-copyright"
    elif not canonical_provenance_validated:
        reason = "missing-original-url-provenance"
    elif headline_overlap < 0.8:
        reason = "headline-mismatch"
    elif not date_matches:
        reason = "publication-date-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "bloombergBnnValidated": valid,
        "syndicationFinalUrl": final_url,
        "syndicationHeadlineOverlap": round(headline_overlap, 4),
        "syndicationBodyCharacters": body_characters,
        "syndicationBloombergAttributed": attributed,
        "syndicationBloombergCopyrightAttributed": copyright_attributed,
        "syndicationCanonicalArticleLinked": canonical_linked,
        "syndicationMirroredSlugValidated": mirrored_slug_validated,
        "syndicationDateDeltaDays": date_delta_days,
        "syndicationExpectedDateVisible": date_visible,
        "syndicationOriginalHeadline": expected_headline,
        "syndicationPartnerHostValidated": partner_validated,
        "syndicationPartnerUrl": archived_partner_url,
    }


def _validate_bloomberg_partner_archive_response(
    item: ManifestItem,
    *,
    expected_headline: str,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    partner_match = re.match(
        r"^https?://web\.archive\.org/web/\d{14}"
        r"(?:id_|im_|js_|cs_)?/(https?://.+)$",
        final_url,
        flags=re.IGNORECASE,
    )
    archived_partner_url = (
        unquote(partner_match.group(1)) if partner_match else ""
    )
    archived_partner = urlsplit(archived_partner_url)
    partner_host = (archived_partner.hostname or "").casefold()
    partner_validated = (
        archived_partner.scheme in {"http", "https"}
        and bool(partner_host)
        and partner_host not in {"bloomberg.com", "www.bloomberg.com"}
    )
    if not partner_validated:
        return False, {
            "reason": "unexpected-partner-archive-url",
            "bloombergPartnerValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": False,
        }
    try:
        article = parse_article(
            content,
            publisher="bloomberg",
            canonical_url=item.canonical_url,
            allow_generic_syndication=True,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "bloombergPartnerValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": True,
        }
    soup = BeautifulSoup(content, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    author_text = " ".join(author.name for author in article.authors)
    attributed = re.search(
        r"(?i)(?:^|\W)bloomberg(?:\s+news)?(?:\W|$)",
        author_text + "\n" + visible_text[:5_000],
    ) is not None
    expected_date = _parse_iso_datetime(item.published_at)
    copyright_attributed = (
        expected_date is not None
        and re.search(
            rf"(?i)(?:©|\(c\)|copyright)\s*{expected_date.year}\s+"
            r"bloomberg\s+l\.p\.",
            visible_text,
        )
        is not None
    )
    headline_overlap = _headline_text_overlap(
        expected_headline,
        article.headline or "",
    )
    date_delta_days: int | None = None
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    date_visible = _expected_date_visible(
        content,
        expected_date=expected_date,
    )
    date_matches = (
        date_delta_days is not None and date_delta_days <= 2
    ) or date_visible
    body_characters = article.quality.body_characters
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters
        >= BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS
        and attributed
        and copyright_attributed
        and headline_overlap >= 0.8
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS:
        reason = "body-too-short"
    elif not attributed:
        reason = "missing-bloomberg-attribution"
    elif not copyright_attributed:
        reason = "missing-bloomberg-copyright"
    elif headline_overlap < 0.8:
        reason = "headline-mismatch"
    elif not date_matches:
        reason = "publication-date-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "bloombergPartnerValidated": valid,
        "syndicationFinalUrl": final_url,
        "syndicationHeadlineOverlap": round(headline_overlap, 4),
        "syndicationBodyCharacters": body_characters,
        "syndicationBloombergAttributed": attributed,
        "syndicationBloombergCopyrightAttributed": copyright_attributed,
        "syndicationDateDeltaDays": date_delta_days,
        "syndicationExpectedDateVisible": date_visible,
        "syndicationOriginalHeadline": expected_headline,
        "syndicationPartnerHostValidated": partner_validated,
        "syndicationPartnerUrl": archived_partner_url,
    }


def _is_bnn_wayback_candidate(value: str) -> bool:
    return (
        re.match(
            r"^https?://web\.archive\.org/web/\d{14}"
            r"(?:id_|im_|js_|cs_)?/https?://"
            r"(?:www\.)?bnnbloomberg\.ca/",
            value,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _bnn_mirrored_slug_matches(
    partner_url: str,
    canonical_url: str,
) -> bool:
    partner = urlsplit(partner_url)
    canonical = urlsplit(canonical_url)
    partner_match = re.fullmatch(
        r"/bloomberg/(20\d{2})/(\d{2})/(\d{2})/"
        r"([a-z0-9][a-z0-9-]*)/?",
        partner.path,
        flags=re.IGNORECASE,
    )
    if (
        partner_match is None
        or (partner.hostname or "").casefold()
        not in {"bnnbloomberg.ca", "www.bnnbloomberg.ca"}
        or (canonical.hostname or "").casefold()
        not in {"bloomberg.com", "www.bloomberg.com"}
    ):
        return False
    expected_path = (
        "/news/articles/"
        f"{partner_match.group(1)}-{partner_match.group(2)}-"
        f"{partner_match.group(3)}/{partner_match.group(4)}"
    )
    return canonical.path.rstrip("/").casefold() == expected_path.casefold()


def _validate_nyt_syndication_response(
    item: ManifestItem,
    *,
    expected_headline: str | None,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    try:
        article = parse_article(
            content,
            publisher="nyt",
            canonical_url=item.canonical_url,
            allow_generic_syndication=True,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "nytSyndicationValidated": False,
        }
    soup = BeautifulSoup(content, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    author_text = " ".join(author.name for author in article.authors)
    attribution_text = author_text + "\n" + visible_text[:20_000]
    attributed = re.search(
        r"(?i)(?:the\s+)?new\s+york\s+times|"
        r"(?:nytimes|nyt)\s+news\s+service",
        attribution_text,
    ) is not None
    expected_date = _parse_iso_datetime(item.published_at)
    date_delta_days: int | None = None
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    date_visible = _expected_date_visible(
        content,
        expected_date=expected_date,
    )
    date_matches = (
        date_delta_days is not None and date_delta_days <= 2
    ) or date_visible
    canonical_linked = _html_links_to_article(
        content.decode("utf-8", errors="ignore"),
        item.canonical_url,
    )
    has_provenance = bool(expected_headline) or canonical_linked
    headline_overlap = (
        _headline_text_overlap(
            expected_headline,
            article.headline or "",
        )
        if expected_headline
        else 1.0
        if canonical_linked
        else 0.0
    )
    body_characters = article.quality.body_characters
    title_matches = headline_overlap >= 0.75
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters >= NYT_SYNDICATION_MINIMUM_BODY_CHARACTERS
        and attributed
        and has_provenance
        and title_matches
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < NYT_SYNDICATION_MINIMUM_BODY_CHARACTERS:
        reason = "body-too-short"
    elif not attributed:
        reason = "missing-nyt-attribution"
    elif not has_provenance:
        reason = "missing-original-headline"
    elif not title_matches:
        reason = "headline-mismatch"
    elif not date_matches:
        reason = "publication-date-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "nytSyndicationValidated": valid,
        "syndicationFinalUrl": final_url,
        "syndicationHeadlineOverlap": round(headline_overlap, 4),
        "syndicationBodyCharacters": body_characters,
        "syndicationNytAttributed": attributed,
        "syndicationDateDeltaDays": date_delta_days,
        "syndicationExpectedDateVisible": date_visible,
        "syndicationOriginalHeadline": expected_headline,
        "syndicationCanonicalArticleLinked": canonical_linked,
    }


def _validate_wsj_syndication_response(
    item: ManifestItem,
    *,
    expected_headline: str | None,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    parsed_final_url = urlsplit(final_url)
    final_host = (parsed_final_url.hostname or "").casefold()
    partner_host_validated = (
        parsed_final_url.scheme == "https"
        and final_host in {"tovima.com", "www.tovima.com"}
        and parsed_final_url.path.startswith("/wsj/")
    )
    if not partner_host_validated:
        return False, {
            "reason": "unexpected-partner-url",
            "wsjSyndicationValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": False,
        }
    if not expected_headline:
        return False, {
            "reason": "missing-original-headline",
            "wsjSyndicationValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": True,
        }
    try:
        article = parse_article(
            content,
            publisher="wsj",
            canonical_url=item.canonical_url,
            allow_generic_syndication=True,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "wsjSyndicationValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": True,
        }
    soup = BeautifulSoup(content, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    author_text = " ".join(author.name for author in article.authors)
    attribution_text = author_text + "\n" + visible_text[:30_000]
    attributed = re.search(
        r"(?i)(?:the\s+)?wall\s+street\s+journal|(?:^|\W)WSJ(?:\W|$)",
        attribution_text,
    ) is not None
    headline_overlap = _headline_text_overlap(
        expected_headline,
        article.headline or "",
    )
    expected_date = _parse_iso_datetime(item.published_at)
    date_delta_days: int | None = None
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    date_visible = _expected_date_visible(
        content,
        expected_date=expected_date,
    )
    date_matches = (
        date_delta_days is not None and date_delta_days <= 2
    ) or date_visible
    body_characters = article.quality.body_characters
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters >= WSJ_SYNDICATION_MINIMUM_BODY_CHARACTERS
        and attributed
        and headline_overlap >= 0.8
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < WSJ_SYNDICATION_MINIMUM_BODY_CHARACTERS:
        reason = "body-too-short"
    elif not attributed:
        reason = "missing-wsj-attribution"
    elif headline_overlap < 0.8:
        reason = "headline-mismatch"
    elif not date_matches:
        reason = "publication-date-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "wsjSyndicationValidated": valid,
        "syndicationFinalUrl": final_url,
        "syndicationHeadlineOverlap": round(headline_overlap, 4),
        "syndicationBodyCharacters": body_characters,
        "syndicationWsjAttributed": attributed,
        "syndicationDateDeltaDays": date_delta_days,
        "syndicationExpectedDateVisible": date_visible,
        "syndicationOriginalHeadline": expected_headline,
        "syndicationPartnerHostValidated": partner_host_validated,
    }


def _validate_ft_syndication_response(
    item: ManifestItem,
    *,
    expected_partner_url: str,
    expected_headline: str | None,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    expected_host = (
        urlsplit(expected_partner_url).hostname or ""
    ).casefold().removeprefix("www.")
    final_host = (
        urlsplit(final_url).hostname or ""
    ).casefold().removeprefix("www.")
    partner_host_validated = (
        bool(expected_host)
        and final_host == expected_host
        and final_host not in {"ft.com"}
    )
    if not partner_host_validated:
        return False, {
            "reason": "unexpected-partner-url",
            "ftSyndicationValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": False,
        }
    if not expected_headline:
        return False, {
            "reason": "missing-original-headline",
            "ftSyndicationValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": True,
        }
    try:
        article = parse_article(
            content,
            publisher="ft",
            canonical_url=item.canonical_url,
            allow_generic_syndication=True,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "ftSyndicationValidated": False,
            "syndicationFinalUrl": final_url,
            "syndicationPartnerHostValidated": True,
        }
    soup = BeautifulSoup(content, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    copyright_attributed = re.search(
        r"(?i)(?:copyright|©|\(c\))\s*(?:20\d{2}\s+)?"
        r"(?:the\s+)?financial\s+times\s+(?:limited|ltd\.?)"
        r"(?:\s+20\d{2})?",
        visible_text,
    ) is not None
    advisorstream_licensed = re.search(
        r"(?i)(?:this(?:\s+financial\s+times)?|financial\s+times)"
        r"\s+article\s+was\s+legally\s+licensed\s+"
        r"(?:by|through)\s+advisorstream",
        visible_text,
    ) is not None
    headline_overlap = _headline_text_overlap(
        expected_headline,
        article.headline or "",
    )
    expected_date = _parse_iso_datetime(item.published_at)
    date_delta_days: int | None = None
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    visible_date_delta_days = _nearest_visible_date_delta_days(
        visible_text,
        expected_date=expected_date,
    )
    if visible_date_delta_days is not None and (
        date_delta_days is None
        or visible_date_delta_days < date_delta_days
    ):
        date_delta_days = visible_date_delta_days
    date_visible = _expected_date_visible(
        content,
        expected_date=expected_date,
    )
    maximum_date_delta_days = (
        FT_ADVISORSTREAM_MAXIMUM_DATE_DELTA_DAYS
        if advisorstream_licensed
        else FT_SYNDICATION_MAXIMUM_DATE_DELTA_DAYS
    )
    date_matches = (
        date_delta_days is not None
        and date_delta_days <= maximum_date_delta_days
    ) or date_visible
    body_characters = article.quality.body_characters
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters >= FT_SYNDICATION_MINIMUM_BODY_CHARACTERS
        and copyright_attributed
        and headline_overlap >= 0.8
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < FT_SYNDICATION_MINIMUM_BODY_CHARACTERS:
        reason = "body-too-short"
    elif not copyright_attributed:
        reason = "missing-ft-copyright"
    elif headline_overlap < 0.8:
        reason = "headline-mismatch"
    elif not date_matches:
        reason = "publication-date-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "ftSyndicationValidated": valid,
        "syndicationFinalUrl": final_url,
        "syndicationHeadlineOverlap": round(headline_overlap, 4),
        "syndicationBodyCharacters": body_characters,
        "syndicationFtCopyrightAttributed": copyright_attributed,
        "syndicationAdvisorStreamLicensed": advisorstream_licensed,
        "syndicationDateDeltaDays": date_delta_days,
        "syndicationMaximumDateDeltaDays": maximum_date_delta_days,
        "syndicationExpectedDateVisible": date_visible,
        "syndicationOriginalHeadline": expected_headline,
        "syndicationPartnerHostValidated": partner_host_validated,
    }


def _validate_ft_infini_origin_response(
    item: ManifestItem,
    *,
    expected_source_url: str,
    expected_headline: str | None,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    origin_url_validated = (
        _same_ft_origin_article_url(expected_source_url, item.canonical_url)
        and _same_ft_origin_article_url(final_url, item.canonical_url)
    )
    if not origin_url_validated:
        return False, {
            "reason": "unexpected-origin-url",
            "ftInfiniOriginValidated": False,
            "infiniOriginUrlValidated": False,
            "infiniOriginFinalUrl": final_url,
        }
    if not expected_headline:
        return False, {
            "reason": "missing-original-headline",
            "ftInfiniOriginValidated": False,
            "infiniOriginUrlValidated": True,
            "infiniOriginFinalUrl": final_url,
        }
    try:
        article = parse_article(
            content,
            publisher="ft",
            canonical_url=item.canonical_url,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "ftInfiniOriginValidated": False,
            "infiniOriginUrlValidated": True,
            "infiniOriginFinalUrl": final_url,
        }
    headline_overlap = _headline_text_overlap(
        expected_headline,
        article.headline or "",
    )
    expected_date = _parse_iso_datetime(item.published_at)
    date_delta_days: int | None = None
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    date_visible = _expected_date_visible(
        content,
        expected_date=expected_date,
    )
    date_matches = (
        date_delta_days is not None and date_delta_days <= 2
    ) or date_visible
    body_characters = article.quality.body_characters
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters >= 1_000
        and headline_overlap >= 0.8
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < 1_000:
        reason = "body-too-short"
    elif headline_overlap < 0.8:
        reason = "headline-mismatch"
    elif not date_matches:
        reason = "publication-date-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "ftInfiniOriginValidated": valid,
        "infiniOriginUrlValidated": origin_url_validated,
        "infiniOriginFinalUrl": final_url,
        "infiniOriginHeadlineOverlap": round(headline_overlap, 4),
        "infiniOriginBodyCharacters": body_characters,
        "infiniOriginDateDeltaDays": date_delta_days,
        "infiniOriginExpectedDateVisible": date_visible,
        "infiniOriginExpectedHeadline": expected_headline,
    }


def _validate_ft_ghostarchive_response(
    item: ManifestItem,
    *,
    expected_headline: str | None,
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    origin_url_validated = _same_ft_origin_article_url(
        final_url,
        item.canonical_url,
    )
    if not origin_url_validated:
        return False, {
            "reason": "unexpected-origin-url",
            "ftGhostarchiveOriginValidated": False,
            "ghostarchiveOriginUrlValidated": False,
            "ghostarchiveOriginFinalUrl": final_url,
        }
    try:
        article = parse_article(
            content,
            publisher="ft",
            canonical_url=item.canonical_url,
        )
    except Exception as exc:
        return False, {
            "reason": f"parser-{type(exc).__name__}",
            "ftGhostarchiveOriginValidated": False,
            "ghostarchiveOriginUrlValidated": True,
            "ghostarchiveOriginFinalUrl": final_url,
        }
    parsed_headline = article.headline or ""
    headline_present = len(_significant_tokens(parsed_headline)) >= 4
    headline_overlap = (
        _headline_text_overlap(expected_headline, parsed_headline)
        if expected_headline
        else None
    )
    headline_matches = (
        headline_present
        and (
            headline_overlap is None
            or headline_overlap >= 0.8
        )
    )
    expected_date = _parse_iso_datetime(item.published_at)
    date_delta_days: int | None = None
    if expected_date is not None and article.published_at is not None:
        date_delta_days = abs(
            (article.published_at.date() - expected_date.date()).days
        )
    date_visible = _expected_date_visible(
        content,
        expected_date=expected_date,
    )
    date_matches = (
        date_delta_days is not None
        and date_delta_days <= FT_SYNDICATION_MAXIMUM_DATE_DELTA_DAYS
    ) or date_visible
    body_characters = article.quality.body_characters
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters >= 1_000
        and headline_matches
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < 1_000:
        reason = "body-too-short"
    elif not headline_matches:
        reason = "headline-mismatch"
    elif not date_matches:
        reason = "publication-date-mismatch"
    else:
        reason = None
    return valid, {
        "reason": reason,
        "ftGhostarchiveOriginValidated": valid,
        "ghostarchiveOriginUrlValidated": origin_url_validated,
        "ghostarchiveOriginFinalUrl": final_url,
        "ghostarchiveOriginHeadline": parsed_headline,
        "ghostarchiveOriginHeadlineOverlap": (
            round(headline_overlap, 4)
            if headline_overlap is not None
            else None
        ),
        "ghostarchiveOriginBodyCharacters": body_characters,
        "ghostarchiveOriginDateDeltaDays": date_delta_days,
        "ghostarchiveOriginExpectedDateVisible": date_visible,
        "ghostarchiveOriginExpectedHeadline": expected_headline,
    }


def _extract_ft_original_headline(
    content: bytes,
    *,
    expected_published_at: str | None,
    final_url: str,
) -> str | None:
    decoded_url = unquote(final_url).casefold()
    if (
        "/content/" not in decoded_url
        or re.search(
            r"https?://(?:[^/?#]+\.)?ft\.com(?:[/?#]|$)",
            decoded_url,
        )
        is None
    ):
        return None
    soup = BeautifulSoup(content, "html.parser")
    expected_date = _parse_iso_datetime(expected_published_at)

    def structured_articles(value: object) -> Iterable[dict]:
        if isinstance(value, dict):
            article_type = value.get("@type")
            types = (
                {str(item).casefold() for item in article_type}
                if isinstance(article_type, list)
                else {str(article_type).casefold()}
            )
            if types & {"article", "newsarticle", "reportagenewsarticle"}:
                yield value
            for child in value.values():
                yield from structured_articles(child)
        elif isinstance(value, list):
            for child in value:
                yield from structured_articles(child)

    for script in soup.select('script[type="application/ld+json"]'):
        serialized = script.string or script.get_text()
        if not serialized.strip():
            continue
        try:
            payload = json.loads(serialized)
        except (json.JSONDecodeError, TypeError):
            continue
        for article in structured_articles(payload):
            headline = article.get("headline")
            if not isinstance(headline, str):
                continue
            published_at = _parse_iso_datetime(
                article.get("datePublished")
                if isinstance(article.get("datePublished"), str)
                else None
            )
            if (
                expected_date is not None
                and published_at is not None
                and abs(
                    (published_at.date() - expected_date.date()).days
                )
                > 2
            ):
                continue
            cleaned = _clean_syndication_search_title(
                BeautifulSoup(
                    headline,
                    "html.parser",
                ).get_text(" ", strip=True)
            )
            if len(_significant_tokens(cleaned)) >= 4:
                return cleaned

    for selector, attribute in (
        ("meta[property='og:title']", "content"),
        ("meta[name='twitter:title']", "content"),
    ):
        node = soup.select_one(selector)
        value = node.get(attribute) if node is not None else None
        if not isinstance(value, str):
            continue
        cleaned = _clean_syndication_search_title(value)
        if len(_significant_tokens(cleaned)) >= 4:
            return cleaned
    return None


def _reuters_syndication_headline_overlap(
    canonical_url: str,
    headline: str,
) -> float:
    return _syndication_headline_overlap(
        canonical_url,
        headline,
        strip_reuters_date_suffix=True,
    )


def _syndication_headline_overlap(
    canonical_url: str,
    headline: str,
    *,
    strip_reuters_date_suffix: bool = False,
) -> float:
    slug = urlsplit(canonical_url).path.rstrip("/").rsplit("/", 1)[-1]
    if strip_reuters_date_suffix:
        slug = re.sub(r"-20\d{2}-\d{2}-\d{2}$", "", slug)
    slug_tokens = _significant_tokens(slug.replace("-", " "))
    headline_tokens = _significant_tokens(headline)
    if not slug_tokens or not headline_tokens:
        return 0.0
    return len(slug_tokens & headline_tokens) / min(
        len(slug_tokens),
        len(headline_tokens),
    )


def _headline_text_overlap(first: str, second: str) -> float:
    first_tokens = _significant_tokens(first)
    second_tokens = _significant_tokens(second)
    if not first_tokens or not second_tokens:
        return 0.0
    return len(first_tokens & second_tokens) / min(
        len(first_tokens),
        len(second_tokens),
    )


def _expected_date_visible(
    content: bytes,
    *,
    expected_date: datetime | None,
) -> bool:
    if expected_date is None:
        return False
    text = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)
    raw = content.decode("utf-8", errors="ignore")
    month = expected_date.strftime("%B")
    abbreviated_month = expected_date.strftime("%b")
    values = (
        expected_date.strftime("%Y-%m-%d"),
        f"{month} {expected_date.day}, {expected_date.year}",
        f"{abbreviated_month} {expected_date.day}, {expected_date.year}",
        f"{expected_date.day} {month} {expected_date.year}",
        f"{expected_date.day} {abbreviated_month} {expected_date.year}",
    )
    haystack = raw.casefold() + "\n" + text.casefold()
    return any(value.casefold() in haystack for value in values)


def _nearest_visible_date_delta_days(
    visible_text: str,
    *,
    expected_date: datetime | None,
) -> int | None:
    if expected_date is None:
        return None
    patterns = (
        (r"\b20\d{2}-\d{2}-\d{2}\b", "%Y-%m-%d"),
        (
            r"\b(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{1,2},?\s+"
            r"20\d{2}\b",
            "%B %d %Y",
        ),
        (
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\.?\s+\d{1,2},?\s+20\d{2}\b",
            "%b %d %Y",
        ),
        (
            r"\b\d{1,2}\s+(?:January|February|March|April|May|June|"
            r"July|August|September|October|November|December)\s+"
            r"20\d{2}\b",
            "%d %B %Y",
        ),
        (
            r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|"
            r"Oct|Nov|Dec)\.?\s+20\d{2}\b",
            "%d %b %Y",
        ),
    )
    deltas: list[int] = []
    date_text = visible_text[:4_000]
    for pattern, date_format in patterns:
        for match in re.finditer(pattern, date_text, flags=re.IGNORECASE):
            normalized = re.sub(
                r"(?<=\b[A-Za-z]{3})\.",
                "",
                match.group(0),
            ).replace(",", "")
            try:
                parsed = datetime.strptime(normalized, date_format)
            except ValueError:
                continue
            deltas.append(
                abs((parsed.date() - expected_date.date()).days)
            )
    return min(deltas) if deltas else None


def _significant_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in REUTERS_SYNDICATION_STOP_WORDS
    }


def _timemap_value(
    row: list[object],
    columns: dict[str, int],
    name: str,
) -> str:
    index = columns.get(name)
    if index is None or index >= len(row):
        return ""
    return str(row[index]).strip()


def _fetch_limited_archive(
    archive_client: ArchiveClient,
    url: str,
    *,
    maximum_bytes: int,
    attempts: int,
    timeout: float,
) -> tuple[int, dict[str, str], bytes, str]:
    limited = getattr(archive_client, "fetch_limited", None)
    if callable(limited):
        return limited(
            url,
            maximum_bytes=maximum_bytes,
            attempts=attempts,
            timeout=timeout,
        )
    return archive_client.fetch(
        url,
        maximum_bytes=maximum_bytes,
    )


def _same_article_url(first: str, second: str) -> bool:
    first_parts = urlsplit(first)
    second_parts = urlsplit(second)
    first_host = (first_parts.hostname or "").casefold().removeprefix("www.")
    second_host = (second_parts.hostname or "").casefold().removeprefix("www.")
    return (
        first_host == second_host
        and bool(first_host)
        and first_parts.path.rstrip("/") == second_parts.path.rstrip("/")
    )


def _is_ft_origin_url(value: str | None) -> bool:
    hostname = (urlsplit(value or "").hostname or "").casefold()
    return hostname == "ft.com" or hostname.endswith(".ft.com")


def _same_ft_origin_article_url(first: str, second: str) -> bool:
    first_parts = urlsplit(first)
    second_parts = urlsplit(second)
    return bool(
        _is_ft_origin_url(first)
        and _is_ft_origin_url(second)
        and first_parts.path.rstrip("/").casefold()
        == second_parts.path.rstrip("/").casefold()
    )


def _arquivo_pt_replay_matches(
    replay_url: str,
    canonical_url: str,
) -> bool:
    parsed = urlsplit(unquote(replay_url))
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    marker = "/noFrame/replay/"
    if host != "arquivo.pt" or marker not in parsed.path:
        return False
    remainder = parsed.path.split(marker, 1)[1]
    _, separator, target_url = remainder.partition("/")
    return bool(
        separator
        and target_url.startswith(("http://", "https://"))
        and _same_article_url(target_url, canonical_url)
    )


def _timemap_candidate_sort_key(
    candidate: CaptureCandidate,
    *,
    published_at: str | None,
) -> tuple[float, str]:
    timestamp = candidate.captured_at
    if timestamp is None:
        return (float("inf"), candidate.snapshot_url)
    published = _parse_iso_datetime(published_at)
    if published is None:
        return (timestamp.timestamp(), candidate.snapshot_url)
    return (
        abs((timestamp - published).total_seconds()),
        candidate.snapshot_url,
    )


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolved_capture_candidate(
    candidate: CaptureCandidate,
    *,
    final_url: str,
    http_status: int,
    content_type: str,
    byte_count: int,
) -> CaptureCandidate:
    updates: dict[str, object] = {
        "status_code": http_status,
        "mime_type": content_type or candidate.mime_type,
        "byte_count": byte_count,
    }
    if candidate.provider == CaptureProvider.WAYBACK:
        match = _WAYBACK_FINAL_RE.search(final_url)
        if match:
            updates["snapshot_url"] = final_url
            updates["captured_at"] = _wayback_datetime(match.group(1))
    return candidate.model_copy(update=updates)


def _ft_article_body_evidence(
    content: bytes,
    *,
    final_url: str,
) -> tuple[int | None, int]:
    decoded_url = unquote(final_url).casefold()
    if (
        "/content/" not in decoded_url
        or re.search(
            r"https?://(?:[^/?#]+\.)?ft\.com(?:[/?#]|$)",
            decoded_url,
        )
        is None
    ):
        return None, 0

    soup = BeautifulSoup(content, "html.parser")
    body_nodes = soup.select(".article__content-body, .article-body")
    if not body_nodes:
        for selector in (
            "#article-body",
            "[data-trackable='article-body']",
            "[data-testid='article-body']",
        ):
            body_nodes.extend(soup.select(selector))

    body_characters = 0
    body_images = 0
    for node in body_nodes:
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        body_characters = max(body_characters, len(text))
        image_count = sum(
            bool(
                image.get("src")
                or image.get("data-src")
                or image.get("srcset")
            )
            for image in node.select("img")
        )
        body_images = max(body_images, image_count)

    def visit(value: object) -> None:
        nonlocal body_characters
        if isinstance(value, dict):
            article_body = value.get("articleBody")
            if isinstance(article_body, str):
                normalized = re.sub(r"\s+", " ", article_body).strip()
                body_characters = max(body_characters, len(normalized))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for script in soup.select('script[type="application/ld+json"]'):
        value = script.string or script.get_text()
        if not value.strip():
            continue
        try:
            visit(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            continue
    return body_characters, body_images


def score_raw_capture(
    content: bytes,
    *,
    http_status: int,
    content_type: str,
    final_url: str = "",
) -> tuple[int, dict[str, object]]:
    sampled_content = (
        content
        if len(content) <= 2_000_000
        else content[:1_000_000] + content[-1_000_000:]
    )
    prefix = sampled_content.lower()
    looks_like_html = (
        "html" in content_type.casefold()
        or any(marker in prefix for marker in _HTML_MARKERS)
    )
    archive_error_page = any(marker in prefix for marker in _ARCHIVE_ERROR_MARKERS)
    has_article_marker = b"<article" in prefix or b"newsarticle" in prefix
    final_url_lower = final_url.casefold()
    decoded_final_url = unquote(final_url_lower)
    authentication_shell = (
        not has_article_marker
        and (
            any(marker in prefix for marker in _AUTH_SHELL_MARKERS)
            or "/auth/login" in final_url_lower
            or "/auth/enter-email" in final_url_lower
            or "/account/login" in final_url_lower
            or "/signin" in final_url_lower
            or "/sign-in" in final_url_lower
        )
    )
    access_challenge_shell = (
        not has_article_marker
        and (
            any(marker in prefix for marker in _ACCESS_CHALLENGE_MARKERS)
            or "/access-error/" in final_url_lower
            or "/tosv2.html" in final_url_lower
        )
    )
    has_strong_body_marker = (
        b'"articlebody"' in prefix
        or any(marker in prefix for marker in _ARTICLE_BODY_MARKERS)
    )
    ft_body_characters, ft_body_images = _ft_article_body_evidence(
        sampled_content,
        final_url=final_url,
    )
    ft_truncated_article_shell = (
        ft_body_characters is not None
        and has_article_marker
        and has_strong_body_marker
        and ft_body_characters < FT_CAPTURE_MINIMUM_BODY_CHARACTERS
        and ft_body_images < FT_IMAGE_LED_MINIMUM_IMAGES
    )
    wsj_subscription_shell = (
        b"continue reading" in prefix
        and b"wsj subscription" in prefix
        and b"already a subscriber" in prefix
    )
    bloomberg_subscription_shell = (
        b"already a subscriber" in prefix
        and b"log in to keep reading" in prefix
        and b"bloomberg" in prefix
    )
    wsj_snippet_shell = b'"issnippetview":true' in prefix
    wsj_empty_article_shell = bool(
        re.search(br'"headline"\s*:\s*""', prefix)
        and re.search(br'"datepublished"\s*:\s*""', prefix)
        and re.search(
            br'"url"\s*:\s*"https?://(?:www\.)?wsj\.com/articles/"',
            prefix,
        )
    )
    ft_legacy_barrier_url = (
        "authorised=false" in decoded_final_url
        or "iab=barrier-app" in decoded_final_url
        or "classification=conditional_standard" in decoded_final_url
    )
    subscription_shell = wsj_snippet_shell or wsj_empty_article_shell or (
        not has_strong_body_marker
        and (
            wsj_subscription_shell
            or bloomberg_subscription_shell
            or ft_legacy_barrier_url
            or any(marker in prefix for marker in _SUBSCRIPTION_SHELL_MARKERS)
        )
    )
    redirect_shell = not has_strong_body_marker and any(
        marker in prefix for marker in _REDIRECT_SHELL_MARKERS
    )
    substantial = len(content) >= 2_048
    score = 0
    if http_status in ACCEPTED_HTTP_STATUSES:
        score += 35
    if looks_like_html:
        score += 25
    if substantial:
        score += 15
    if has_article_marker:
        score += 15
    if not archive_error_page:
        score += 10
    if (
        authentication_shell
        or access_challenge_shell
        or subscription_shell
        or redirect_shell
        or ft_truncated_article_shell
    ):
        score = max(0, score - 60)
    return score, {
        "looksLikeHtml": looks_like_html,
        "archiveErrorPage": archive_error_page,
        "hasArticleMarker": has_article_marker,
        "hasStrongBodyMarker": has_strong_body_marker,
        "authenticationShell": authentication_shell,
        "accessChallengeShell": access_challenge_shell,
        "subscriptionShell": subscription_shell,
        "ftLegacyBarrierUrl": ft_legacy_barrier_url,
        "wsjEmptyArticleShell": wsj_empty_article_shell,
        "redirectShell": redirect_shell,
        "ftTruncatedArticleShell": ft_truncated_article_shell,
        "ftBodyCharacters": ft_body_characters,
        "ftBodyImages": ft_body_images,
        "substantialResponse": substantial,
        "rawBytes": len(content),
    }


def store_raw_html(output_dir: Path, content: bytes) -> BlobReference:
    digest = hashlib.sha256(content).hexdigest()
    relative = Path("objects") / "html" / digest[:2] / f"{digest}.html.gz"
    destination = output_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(content, compresslevel=9, mtime=0)
    if destination.exists():
        if destination.read_bytes() != compressed:
            raise RuntimeError(f"content-addressed object collision: {relative}")
    else:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(compressed)
        temporary.replace(destination)
    return BlobReference(
        path=relative.as_posix(),
        sha256=digest,
        byte_count=len(content),
        stored_byte_count=len(compressed),
        content_encoding="gzip",
    )


def store_capture_record(output_dir: Path, capture: RawCapture) -> str:
    article_hash = capture.article_id.rsplit(":", 1)[-1]
    relative = Path("records") / article_hash[:2] / f"{article_hash}.json"
    destination = output_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        capture.model_dump_json(
            by_alias=True,
            exclude_none=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    if destination.exists() and destination.read_bytes() != payload:
        raise RuntimeError(f"capture record changed after completion: {relative}")
    if not destination.exists():
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(destination)
    return relative.as_posix()


def record_capture_result(
    connection: sqlite3.Connection,
    result: dict,
) -> None:
    capture: RawCapture | None = result.get("capture")
    values = {
        "status": result["status"],
        "selected_candidate_json": None,
        "final_url": None,
        "http_status": None,
        "content_type": None,
        "quality_score": None,
        "quality_signals_json": None,
        "raw_path": None,
        "raw_sha256": None,
        "raw_bytes": None,
        "stored_bytes": None,
        "record_path": result.get("recordPath"),
        "last_error": result.get("error"),
        "retrieved_at": None,
    }
    if capture:
        values.update(
            {
                "selected_candidate_json": capture.selected_candidate.model_dump_json(
                    by_alias=True,
                    exclude_none=True,
                ),
                "final_url": capture.final_url,
                "http_status": capture.http_status,
                "content_type": capture.content_type,
                "quality_score": capture.quality_score,
                "quality_signals_json": json.dumps(
                    capture.quality_signals,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "raw_path": capture.raw_html.path,
                "raw_sha256": capture.raw_html.sha256,
                "raw_bytes": capture.raw_html.byte_count,
                "stored_bytes": capture.raw_html.stored_byte_count,
                "retrieved_at": capture.retrieved_at.isoformat(),
            }
        )
    with connection:
        connection.execute(
            """
            UPDATE captures SET
                status=:status,
                selected_candidate_json=:selected_candidate_json,
                final_url=:final_url,
                http_status=:http_status,
                content_type=:content_type,
                quality_score=:quality_score,
                quality_signals_json=:quality_signals_json,
                raw_path=:raw_path,
                raw_sha256=:raw_sha256,
                raw_bytes=:raw_bytes,
                stored_bytes=:stored_bytes,
                record_path=:record_path,
                last_error=:last_error,
                retrieved_at=:retrieved_at,
                updated_at=:updated_at
            WHERE canonical_url=:canonical_url
            """,
            {
                **values,
                "canonical_url": result["canonicalUrl"],
                "updated_at": _now_iso(),
            },
        )


def completed_raw_capture(
    connection: sqlite3.Connection,
    *,
    canonical_url: str,
) -> RawCapture:
    row = connection.execute(
        """
        SELECT
            article_id,
            publisher,
            canonical_url,
            published_at,
            section,
            selected_candidate_json,
            candidates_json,
            retrieved_at,
            final_url,
            http_status,
            content_type,
            quality_score,
            quality_signals_json,
            raw_path,
            raw_sha256,
            raw_bytes,
            stored_bytes
        FROM captures
        WHERE canonical_url=? AND status='complete'
        """,
        (canonical_url,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"completed capture not found for {canonical_url}"
        )
    required = {
        "selected_candidate_json": row[5],
        "retrieved_at": row[7],
        "final_url": row[8],
        "http_status": row[9],
        "content_type": row[10],
        "quality_score": row[11],
        "raw_path": row[13],
        "raw_sha256": row[14],
        "raw_bytes": row[15],
        "stored_bytes": row[16],
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(
            "completed capture is missing state fields: "
            + ", ".join(missing)
        )
    return RawCapture(
        article_id=str(row[0]),
        publisher=str(row[1]),
        canonical_url=str(row[2]),
        published_at=row[3],
        section=row[4],
        selected_candidate=CaptureCandidate.model_validate_json(
            str(row[5])
        ),
        candidates_considered=[
            CaptureCandidate.model_validate(candidate)
            for candidate in json.loads(str(row[6]))
        ],
        retrieved_at=str(row[7]),
        final_url=str(row[8]),
        http_status=int(row[9]),
        content_type=str(row[10]),
        quality_score=int(row[11]),
        quality_signals=(
            json.loads(str(row[12])) if row[12] is not None else {}
        ),
        raw_html=BlobReference(
            path=str(row[13]),
            sha256=str(row[14]),
            byte_count=int(row[15]),
            stored_byte_count=int(row[16]),
            content_encoding="gzip",
        ),
    )


def completed_capture_rejection_reason(
    capture: RawCapture,
    *,
    archive_root: Path,
) -> str | None:
    content = _read_capture_html(capture, archive_root=archive_root)
    _, signals = score_raw_capture(
        content,
        http_status=capture.http_status,
        content_type=capture.content_type,
        final_url=capture.final_url,
    )
    structured_subscription_article = bool(
        signals["subscriptionShell"]
        and _structured_subscription_article_usable(
            content,
            publisher=capture.publisher,
            canonical_url=capture.canonical_url,
            raw_capture=capture,
        )
    )
    checks = (
        ("empty-response", not content),
        ("not-html", not bool(signals["looksLikeHtml"])),
        ("archive-error-page", bool(signals["archiveErrorPage"])),
        ("authentication-shell", bool(signals["authenticationShell"])),
        ("access-challenge-shell", bool(signals["accessChallengeShell"])),
        (
            "subscription-shell",
            bool(
                signals["subscriptionShell"]
                and not structured_subscription_article
            ),
        ),
        ("redirect-shell", bool(signals["redirectShell"])),
        (
            "ft-truncated-article-shell",
            bool(signals["ftTruncatedArticleShell"]),
        ),
    )
    for reason, rejected in checks:
        if rejected:
            return reason
    if (
        capture.publisher == "bloomberg"
        and capture.selected_candidate.provider == CaptureProvider.OTHER
    ):
        from .news_parser import parse_article

        try:
            article = parse_article(
                content,
                publisher="bloomberg",
                canonical_url=capture.canonical_url,
                allow_generic_syndication=True,
            )
        except Exception:
            article = None
        if article is not None and _short_parsed_paywall_shell(
            body_characters=article.quality.body_characters,
            plain_text=article.plain_text,
        ):
            return "bloomberg-syndication-paywall-shell"
    if capture.http_status not in ACCEPTED_HTTP_STATUSES:
        return f"http-{capture.http_status}"
    return None


def reset_completed_capture_for_retry(
    connection: sqlite3.Connection,
    *,
    canonical_url: str,
    reason: str,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE captures
            SET status='pending',
                attempts=0,
                last_error=?,
                updated_at=?
            WHERE canonical_url=? AND status='complete'
            """,
            (
                f"raw quality policy rejected stored capture: {reason}",
                _now_iso(),
                canonical_url,
            ),
        )


def _read_capture_html(
    capture: RawCapture,
    *,
    archive_root: Path,
) -> bytes:
    path = archive_root / capture.raw_html.path
    if capture.raw_html.content_encoding == "gzip":
        with gzip.open(path, "rb") as handle:
            content = handle.read()
    else:
        content = path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != capture.raw_html.sha256:
        raise ValueError(
            "raw HTML checksum mismatch: "
            f"expected {capture.raw_html.sha256}, got {actual}"
        )
    return content


def capture_summary(
    connection: sqlite3.Connection,
    *,
    output_dir: Path,
) -> dict[str, object]:
    statuses = dict(
        connection.execute(
            "SELECT status, COUNT(*) FROM captures GROUP BY status"
        ).fetchall()
    )
    sizes = connection.execute(
        """
        SELECT
            COALESCE(SUM(raw_bytes), 0),
            COALESCE(SUM(stored_bytes), 0),
            COALESCE(AVG(quality_score), 0)
        FROM captures
        WHERE status='complete'
        """
    ).fetchone()
    result = {
        "formatVersion": SCHEMA_VERSION,
        "capturesByStatus": statuses,
        "rawHtmlBytes": int(sizes[0]),
        "storedHtmlBytes": int(sizes[1]),
        "averageQualityScore": round(float(sizes[2]), 2),
        "objectsOnDisk": sum(
            1 for path in (output_dir / "objects").rglob("*") if path.is_file()
        )
        if (output_dir / "objects").exists()
        else 0,
        "recordsOnDisk": sum(
            1 for path in (output_dir / "records").rglob("*.json") if path.is_file()
        )
        if (output_dir / "records").exists()
        else 0,
    }
    validation_table = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table' AND name='parser_validation_config'
        """
    ).fetchone()
    if validation_table:
        from .parser_validation import parser_validation_summary

        result["parserValidation"] = parser_validation_summary(connection)
    return result


def _insert_manifest_batch(
    connection: sqlite3.Connection,
    rows: list[tuple[object, ...]],
) -> int:
    before = connection.total_changes
    connection.executemany(
        """
        INSERT INTO captures(
            canonical_url,
            article_id,
            publisher,
            published_at,
            section,
            candidates_json,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_url) DO UPDATE SET
            published_at=COALESCE(
                excluded.published_at,
                captures.published_at
            ),
            section=COALESCE(excluded.section, captures.section),
            candidates_json=excluded.candidates_json,
            status=CASE
                WHEN captures.status='error'
                 AND captures.candidates_json != excluded.candidates_json
                THEN 'pending'
                ELSE captures.status
            END,
            attempts=CASE
                WHEN captures.status='error'
                 AND captures.candidates_json != excluded.candidates_json
                THEN 0
                ELSE captures.attempts
            END,
            last_error=CASE
                WHEN captures.status='error'
                 AND captures.candidates_json != excluded.candidates_json
                THEN NULL
                ELSE captures.last_error
            END,
            updated_at=excluded.updated_at
        WHERE captures.status IN ('pending', 'error')
          AND (
            (
                excluded.published_at IS NOT NULL
                AND captures.published_at IS NOT excluded.published_at
            )
            OR (
                excluded.section IS NOT NULL
                AND captures.section IS NOT excluded.section
            )
            OR captures.candidates_json != excluded.candidates_json
          )
        """,
        rows,
    )
    return connection.total_changes - before


def _read_jsonl(path: Path) -> Iterable[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSON on manifest line {line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"manifest line {line_number} must be a JSON object"
                )
            yield row


def _wayback_datetime(timestamp: str) -> datetime | None:
    if not re.fullmatch(r"\d{14}", timestamp):
        return None
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
