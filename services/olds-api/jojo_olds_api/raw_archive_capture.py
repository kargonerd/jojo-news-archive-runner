from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterable
from urllib.parse import unquote, urlencode, urlsplit

from bs4 import BeautifulSoup
from .bloomberg_archive_download import ArchiveClient
from .news_models import (
    ArticleStatus,
    BlobReference,
    CaptureCandidate,
    CaptureProvider,
    RawCapture,
)


SCHEMA_VERSION = "jojo-raw-capture-state/1"
ACCEPTED_HTTP_STATUSES = {200, 206}
WAYBACK_TIMEMAP_ENDPOINT = "https://web.archive.org/web/timemap/json"
WAYBACK_TIMEMAP_MAXIMUM_BYTES = 2_000_000
WAYBACK_TIMEMAP_MAXIMUM_CANDIDATES = 8
WAYBACK_TIMEMAP_FALLBACK_PUBLISHERS = {"bloomberg", "nyt"}
REUTERS_SYNDICATION_SEARCH_ENDPOINT = "https://search.yahoo.com/search"
REUTERS_SYNDICATION_SEARCH_MAXIMUM_BYTES = 2_000_000
REUTERS_SYNDICATION_MAXIMUM_CANDIDATES = 8
REUTERS_SYNDICATION_MINIMUM_BODY_CHARACTERS = 400
BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS = 400
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
)
_REDIRECT_SHELL_MARKERS = (
    b"window.location = fullurl",
    b"window.location=fullurl",
)
_SUBSCRIPTION_SHELL_MARKERS = (
    b"<title>subscribe to read",
    b'id="barrier-page"',
    b"subscribe to unlock this article",
    b"window.zephr.outcomes['paywall']",
    b"join over 300,000 finance professionals",
    b"discover all the plans currently available in your country",
    b"during your trial you will have complete digital access to ft.com",
)
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
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "publisher": publisher,
        "authorization_reference": authorization_reference,
    }
    connection.executemany(
        """
        INSERT INTO archive_metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        metadata.items(),
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
    for candidate in item.candidates:
        response, failure = _fetch_usable_candidate(
            candidate,
            archive_client=archive_client,
            maximum_html_bytes=maximum_html_bytes,
        )
        if failure:
            failures.append(failure)
        if response is None:
            continue
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
            )
            if failure:
                failures.append(failure)
            if response is None:
                continue
            if best_response is None or response[5] > best_response[5]:
                best_response = response
            if response[5] == 100:
                break

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
            )
            if failure:
                failures.append(failure)
            if response is None:
                continue
            validated, validation_signals = (
                _validate_reuters_syndication_response(
                    item,
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


def _fetch_usable_candidate(
    candidate: CaptureCandidate,
    *,
    archive_client: ArchiveClient,
    maximum_html_bytes: int,
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
    try:
        status_code, headers, content, final_url = archive_client.fetch(
            candidate.snapshot_url,
            maximum_bytes=maximum_html_bytes,
        )
    except Exception as exc:
        return None, f"{candidate.provider.value}:{type(exc).__name__}"
    content_type = headers.get("content-type", "").split(";", 1)[0].strip()
    quality_score, signals = score_raw_capture(
        content,
        http_status=status_code,
        content_type=content_type,
        final_url=final_url,
    )
    if (
        status_code not in ACCEPTED_HTTP_STATUSES
        or not content
        or not signals["looksLikeHtml"]
        or signals["archiveErrorPage"]
        or signals["authenticationShell"]
        or signals["accessChallengeShell"]
        or signals["subscriptionShell"]
        or signals["redirectShell"]
    ):
        return (
            None,
            f"{candidate.provider.value}:http-{status_code}:score-{quality_score}",
        )
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


def discover_wayback_timemap_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
) -> tuple[CaptureCandidate, ...]:
    timemap_url = WAYBACK_TIMEMAP_ENDPOINT + "?" + urlencode(
        {"url": item.canonical_url}
    )
    status_code, headers, content, _ = archive_client.fetch(
        timemap_url,
        maximum_bytes=WAYBACK_TIMEMAP_MAXIMUM_BYTES,
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
    return tuple(candidates[:WAYBACK_TIMEMAP_MAXIMUM_CANDIDATES])


def reuters_syndication_search_url(item: ManifestItem) -> str:
    return _syndication_search_url(item, publisher_label="Reuters")


def bloomberg_syndication_search_url(item: ManifestItem) -> str:
    return _syndication_search_url(item, publisher_label="Bloomberg")


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
    return _discover_syndication_candidates(
        item,
        archive_client=archive_client,
        search_url=reuters_syndication_search_url(item),
        excluded_publisher="reuters",
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


def _discover_syndication_candidates(
    item: ManifestItem,
    *,
    archive_client: ArchiveClient,
    search_url: str,
    excluded_publisher: str,
) -> tuple[CaptureCandidate, ...]:
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
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for position, result in enumerate(soup.select("#web li")):
        anchor = result.select_one("h3 a") or result.select_one("a")
        if anchor is None:
            continue
        candidate_url = _decode_yahoo_search_result(anchor.get("href"))
        if (
            candidate_url is None
            or candidate_url in seen
            or not _is_public_syndication_url(
                candidate_url,
                excluded_publisher=excluded_publisher,
            )
        ):
            continue
        seen.add(candidate_url)
        ranked.append(
            (
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
        )
        for _, _, candidate_url in ranked[
            :REUTERS_SYNDICATION_MAXIMUM_CANDIDATES
        ]
    )


def _decode_yahoo_search_result(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    match = re.search(r"/RU=([^/]+)/RK=", value)
    candidate_url = unquote(match.group(1)) if match else value
    parsed = urlsplit(candidate_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate_url


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
    content: bytes,
    final_url: str,
) -> tuple[bool, dict[str, object]]:
    from .news_parser import parse_article

    try:
        article = parse_article(
            content,
            publisher="reuters",
            canonical_url=item.canonical_url,
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
    title_matches = headline_overlap >= 0.75
    valid = (
        article.quality.status == ArticleStatus.COMPLETE
        and body_characters
        >= BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS
        and attributed
        and title_matches
        and date_matches
    )
    if article.quality.status != ArticleStatus.COMPLETE:
        reason = f"parser-{article.quality.status.value}"
    elif body_characters < BLOOMBERG_SYNDICATION_MINIMUM_BODY_CHARACTERS:
        reason = "body-too-short"
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
        "syndicationBloombergAttributed": attributed,
        "syndicationDateDeltaDays": date_delta_days,
        "syndicationExpectedDateVisible": date_visible,
    }


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


def score_raw_capture(
    content: bytes,
    *,
    http_status: int,
    content_type: str,
    final_url: str = "",
) -> tuple[int, dict[str, object]]:
    prefix = content[:1_000_000].lower()
    looks_like_html = (
        "html" in content_type.casefold()
        or any(marker in prefix for marker in _HTML_MARKERS)
    )
    archive_error_page = any(marker in prefix for marker in _ARCHIVE_ERROR_MARKERS)
    has_article_marker = b"<article" in prefix or b"newsarticle" in prefix
    final_url_lower = final_url.casefold()
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
        and any(marker in prefix for marker in _ACCESS_CHALLENGE_MARKERS)
    )
    has_strong_body_marker = (
        b'"articlebody"' in prefix
        or any(marker in prefix for marker in _ARTICLE_BODY_MARKERS)
    )
    subscription_shell = not has_strong_body_marker and any(
        marker in prefix for marker in _SUBSCRIPTION_SHELL_MARKERS
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
        "redirectShell": redirect_shell,
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
    checks = (
        ("empty-response", not content),
        ("not-html", not bool(signals["looksLikeHtml"])),
        ("archive-error-page", bool(signals["archiveErrorPage"])),
        ("authentication-shell", bool(signals["authenticationShell"])),
        ("access-challenge-shell", bool(signals["accessChallengeShell"])),
        ("subscription-shell", bool(signals["subscriptionShell"])),
        ("redirect-shell", bool(signals["redirectShell"])),
    )
    for reason, rejected in checks:
        if rejected:
            return reason
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
