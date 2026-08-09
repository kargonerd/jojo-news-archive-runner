from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

import httpx

from .archive_sources import ArchiveSourceSpec, normalize_article_url
from .bloomberg_archive_download import GlobalRateLimiter
from .common_crawl import (
    COLLECTION_INFO_URL,
    DATA_BASE_URL,
    MAXIMUM_COMPRESSED_WARC_BYTES,
)
from .wayback_manifest import candidate_rank, infer_published_at


SCHEMA_VERSION = "jojo-common-crawl-prefix-discovery/1"
MANIFEST_FORMAT_VERSION = "jojo-capture-manifest/1"
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


class CommonCrawlNoCapturesError(Exception):
    """The queried prefix or filtered index page contains no captures."""


@dataclass(frozen=True)
class PrefixCollection:
    identifier: str
    index_url: str
    from_at: datetime
    to_at: datetime


@dataclass(frozen=True)
class PrefixIndexPage:
    rows: tuple[dict[str, object], ...]


class CommonCrawlPrefixClient:
    def __init__(
        self,
        *,
        minimum_interval: float = 2.0,
        timeout: float = 45.0,
        attempts: int = 4,
        page_size: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if minimum_interval < 0:
            raise ValueError("minimum_interval must not be negative")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if attempts < 1:
            raise ValueError("attempts must be positive")
        if page_size is not None and page_size < 1:
            raise ValueError("page_size must be positive when provided")
        self.rate_limiter = GlobalRateLimiter(minimum_interval)
        self.timeout = timeout
        self.attempts = attempts
        self.page_size = page_size
        self._provided_client = client
        self._client = client or httpx.Client(
            headers={
                "User-Agent": (
                    "JOJO-News-Archive-Research/0.1 "
                    "(nonprofit academic archive; contact via repository)"
                )
            },
            follow_redirects=True,
            timeout=timeout,
        )

    def close(self) -> None:
        if self._provided_client is None:
            self._client.close()

    def collections(self) -> tuple[PrefixCollection, ...]:
        response = self._get(COLLECTION_INFO_URL)
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Common Crawl collection list is not an array")
        result: list[PrefixCollection] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get("id") or "").strip()
            index_url = str(row.get("cdx-api") or "").strip()
            from_at = _parse_datetime(row.get("from"))
            to_at = _parse_datetime(row.get("to"))
            if (
                not identifier
                or not index_url.startswith(
                    "https://index.commoncrawl.org/"
                )
                or from_at is None
                or to_at is None
            ):
                continue
            result.append(
                PrefixCollection(
                    identifier=identifier,
                    index_url=index_url,
                    from_at=from_at,
                    to_at=to_at,
                )
            )
        if not result:
            raise ValueError("Common Crawl collection list is empty")
        return tuple(result)

    def page_count(self, *, index_url: str, pattern: str) -> int:
        try:
            response = self._get(
                index_url,
                params=_query_parameters(pattern, page_size=self.page_size)
                + [("showNumPages", "true")],
            )
        except CommonCrawlNoCapturesError:
            return 0
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Common Crawl page count is not an object")
        pages = _optional_int(payload.get("pages"))
        if pages is None or pages < 0:
            raise ValueError("Common Crawl page count is invalid")
        return pages

    def page(
        self,
        *,
        index_url: str,
        pattern: str,
        page: int,
    ) -> PrefixIndexPage:
        if page < 0:
            raise ValueError("page must not be negative")
        try:
            response = self._get(
                index_url,
                params=_query_parameters(pattern, page_size=self.page_size)
                + [("page", str(page))],
            )
        except CommonCrawlNoCapturesError:
            return PrefixIndexPage(rows=())
        rows: list[dict[str, object]] = []
        for line in response.text.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Common Crawl index row is not an object")
            rows.append(value)
        return PrefixIndexPage(rows=tuple(rows))

    def _get(
        self,
        url: str,
        *,
        params: list[tuple[str, str]] | None = None,
    ) -> httpx.Response:
        last_status: int | None = None
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            self.rate_limiter.wait()
            try:
                response = self._client.get(url, params=params)
                last_status = response.status_code
                if response.status_code == 404:
                    try:
                        message = str(response.json().get("message") or "")
                    except (ValueError, AttributeError):
                        message = ""
                    if message.startswith("No Captures found for:"):
                        raise CommonCrawlNoCapturesError(message)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise RuntimeError(
                        f"retryable HTTP {response.status_code}"
                    )
                response.raise_for_status()
                return response
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                last_error = exc
                if attempt + 1 >= self.attempts:
                    break
                time.sleep(min(30.0, 2.0**attempt))
        suffix = f" (last HTTP status {last_status})" if last_status else ""
        raise RuntimeError(
            f"Common Crawl index query failed after {self.attempts} attempts"
            f"{suffix}"
        ) from last_error


def prefix_patterns(
    spec: ArchiveSourceSpec,
    *,
    from_year: int,
    to_year: int,
) -> tuple[str, ...]:
    result: list[str] = []
    for pattern in spec.expanded_wayback_patterns(
        from_year=from_year,
        to_year=to_year,
    ):
        if pattern.count("*") != 1 or not pattern.endswith("*"):
            continue
        prefix = pattern[:-1]
        if prefix and prefix not in result:
            result.append(prefix)
    if not result:
        raise ValueError(
            f"publisher {spec.publisher!r} has no prefix-compatible pattern"
        )
    return tuple(result)


def initialize_prefix_schema(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    from_year: int,
    to_year: int,
    collections: tuple[PrefixCollection, ...],
) -> None:
    if from_year > to_year:
        raise ValueError("from_year must not exceed to_year")
    patterns = prefix_patterns(
        spec,
        from_year=from_year,
        to_year=to_year,
    )
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS prefix_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prefix_queries (
            collection_id TEXT NOT NULL,
            index_url TEXT NOT NULL,
            pattern TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            total_pages INTEGER,
            next_page INTEGER NOT NULL DEFAULT 0,
            pages INTEGER NOT NULL DEFAULT 0,
            rows_seen INTEGER NOT NULL DEFAULT 0,
            rows_accepted INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(collection_id, pattern)
        );

        CREATE TABLE IF NOT EXISTS prefix_candidates (
            canonical_url TEXT NOT NULL,
            published_at TEXT NOT NULL,
            collection_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            digest TEXT NOT NULL DEFAULT '',
            mimetype TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            byte_count INTEGER NOT NULL,
            warc_filename TEXT NOT NULL,
            warc_offset INTEGER NOT NULL,
            warc_length INTEGER NOT NULL,
            rank_score INTEGER NOT NULL,
            PRIMARY KEY(canonical_url, warc_filename, warc_offset)
        );

        CREATE INDEX IF NOT EXISTS idx_prefix_candidates_rank
            ON prefix_candidates(canonical_url, rank_score, timestamp);
        """
    )
    fingerprint = _fingerprint(
        spec=spec,
        from_year=from_year,
        to_year=to_year,
        patterns=patterns,
    )
    existing = connection.execute(
        "SELECT value FROM prefix_metadata WHERE key='fingerprint'"
    ).fetchone()
    if existing is not None and str(existing[0]) != fingerprint:
        raise ValueError(
            "Common Crawl prefix state belongs to a different publisher, "
            "date window, or pattern set"
        )
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "publisher": spec.publisher,
        "from_year": str(from_year),
        "to_year": str(to_year),
        "fingerprint": fingerprint,
    }
    connection.executemany(
        """
        INSERT INTO prefix_metadata(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        metadata.items(),
    )
    now = _now_iso()
    connection.executemany(
        """
        INSERT OR IGNORE INTO prefix_queries(
            collection_id, index_url, pattern, updated_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            (collection.identifier, collection.index_url, pattern, now)
            for collection in collections
            for pattern in patterns
        ),
    )
    connection.commit()


def next_prefix_query(
    connection: sqlite3.Connection,
) -> tuple[str, str, str, int | None, int] | None:
    row = connection.execute(
        """
        SELECT collection_id, index_url, pattern, total_pages, next_page
        FROM prefix_queries
        WHERE status != 'complete'
        ORDER BY
            attempts,
            CAST(
                substr(pattern, instr(pattern, '/20') + 1, 4)
                AS INTEGER
            ),
            collection_id,
            pattern,
            updated_at
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]), str(row[2]), row[3], int(row[4])


def record_prefix_page_count(
    connection: sqlite3.Connection,
    *,
    collection_id: str,
    pattern: str,
    total_pages: int,
) -> None:
    if total_pages < 0:
        raise ValueError("total_pages must not be negative")
    with connection:
        connection.execute(
            """
            UPDATE prefix_queries
            SET total_pages=?, status=?, last_error=NULL, updated_at=?
            WHERE collection_id=? AND pattern=?
            """,
            (
                total_pages,
                "complete" if total_pages == 0 else "running",
                _now_iso(),
                collection_id,
                pattern,
            ),
        )


def record_prefix_error(
    connection: sqlite3.Connection,
    *,
    collection_id: str,
    pattern: str,
    error: str,
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE prefix_queries
            SET attempts=attempts+1, last_error=?, updated_at=?
            WHERE collection_id=? AND pattern=?
            """,
            (error[:1_000], _now_iso(), collection_id, pattern),
        )


def record_prefix_page(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    collection_id: str,
    pattern: str,
    page_number: int,
    total_pages: int,
    page: PrefixIndexPage,
) -> dict[str, int | bool]:
    if not 0 <= page_number < total_pages:
        raise ValueError("page_number is outside total_pages")
    window = dict(
        connection.execute(
            """
            SELECT key, value FROM prefix_metadata
            WHERE key IN ('from_year', 'to_year')
            """
        ).fetchall()
    )
    start = f"{int(window['from_year']):04d}-01-01"
    end = f"{int(window['to_year']) + 1:04d}-01-01"
    rows: list[tuple[object, ...]] = []
    touched_urls: set[str] = set()
    for value in page.rows:
        original_url = str(value.get("url") or "").strip()
        canonical_url = normalize_article_url(spec, original_url)
        if canonical_url is None:
            continue
        published_at = infer_published_at(canonical_url)
        timestamp = str(value.get("timestamp") or "").strip()
        captured_at = _parse_crawl_timestamp(timestamp)
        status_code = _optional_int(value.get("status"))
        mimetype = str(value.get("mime") or "").strip()
        byte_count = _optional_int(value.get("length"))
        warc_offset = _optional_int(value.get("offset"))
        warc_filename = str(value.get("filename") or "").strip()
        if (
            published_at is None
            or not start <= published_at < end
            or captured_at is None
            or status_code != 200
            or mimetype.casefold() != "text/html"
            or byte_count is None
            or not 0 < byte_count <= MAXIMUM_COMPRESSED_WARC_BYTES
            or warc_offset is None
            or warc_offset < 0
            or not warc_filename.startswith("crawl-data/")
        ):
            continue
        rows.append(
            (
                canonical_url,
                published_at,
                collection_id,
                timestamp,
                original_url,
                str(value.get("digest") or ""),
                mimetype,
                status_code,
                byte_count,
                warc_filename,
                warc_offset,
                byte_count,
                candidate_rank(timestamp, published_at=published_at),
            )
        )
        touched_urls.add(canonical_url)
    with connection:
        before = connection.total_changes
        connection.executemany(
            """
            INSERT OR IGNORE INTO prefix_candidates(
                canonical_url, published_at, collection_id, timestamp,
                original_url, digest, mimetype, status_code, byte_count,
                warc_filename, warc_offset, warc_length, rank_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        accepted = connection.total_changes - before
        if touched_urls:
            placeholders = ",".join("?" for _ in touched_urls)
            connection.execute(
                f"""
                DELETE FROM prefix_candidates
                WHERE rowid IN (
                    SELECT rowid FROM (
                        SELECT rowid,
                            ROW_NUMBER() OVER (
                                PARTITION BY canonical_url
                                ORDER BY rank_score, timestamp,
                                         collection_id, warc_filename,
                                         warc_offset
                            ) AS candidate_number
                        FROM prefix_candidates
                        WHERE canonical_url IN ({placeholders})
                    ) WHERE candidate_number > 3
                )
                """,
                sorted(touched_urls),
            )
        next_page = page_number + 1
        connection.execute(
            """
            UPDATE prefix_queries
            SET next_page=?, pages=pages+1, rows_seen=rows_seen+?,
                rows_accepted=rows_accepted+?, attempts=0,
                status=?, last_error=NULL, updated_at=?
            WHERE collection_id=? AND pattern=?
            """,
            (
                next_page,
                len(page.rows),
                accepted,
                "complete" if next_page >= total_pages else "running",
                _now_iso(),
                collection_id,
                pattern,
            ),
        )
    return {
        "seen": len(page.rows),
        "accepted": accepted,
        "complete": next_page >= total_pages,
    }


def export_prefix_manifest(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    destination: Path,
) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    opener = gzip.open if destination.suffix == ".gz" else open
    rows = connection.execute(
        """
        SELECT canonical_url, published_at, collection_id, timestamp,
               original_url, digest, mimetype, status_code, byte_count,
               warc_filename, warc_offset, warc_length
        FROM prefix_candidates
        ORDER BY canonical_url, rank_score, timestamp, collection_id
        """
    )
    article_count = 0
    candidate_count = 0
    with opener(temporary, "wt", encoding="utf-8") as handle:
        current_url: str | None = None
        current_published_at: str | None = None
        candidates: list[dict[str, object]] = []
        for row in rows:
            canonical_url = str(row[0])
            if current_url is not None and canonical_url != current_url:
                _write_row(
                    handle,
                    spec=spec,
                    canonical_url=current_url,
                    published_at=current_published_at,
                    candidates=candidates,
                )
                article_count += 1
                candidate_count += len(candidates)
                candidates = []
            current_url = canonical_url
            current_published_at = str(row[1])
            captured_at = _parse_crawl_timestamp(str(row[3]))
            candidates.append(
                {
                    "provider": "commoncrawl",
                    "snapshotUrl": DATA_BASE_URL + str(row[9]),
                    "capturedAt": captured_at.isoformat(),
                    **({"digest": str(row[5])} if row[5] else {}),
                    "mimeType": str(row[6]),
                    "statusCode": int(row[7]),
                    "byteCount": int(row[8]),
                    "warcFilename": str(row[9]),
                    "warcOffset": int(row[10]),
                    "warcLength": int(row[11]),
                }
            )
        if current_url is not None:
            _write_row(
                handle,
                spec=spec,
                canonical_url=current_url,
                published_at=current_published_at,
                candidates=candidates,
            )
            article_count += 1
            candidate_count += len(candidates)
    temporary.replace(destination)
    return {
        "publisher": spec.publisher,
        "articles": article_count,
        "candidates": candidate_count,
        "output": str(destination),
    }


def prefix_summary(connection: sqlite3.Connection) -> dict[str, object]:
    query_status = {
        str(status): int(count)
        for status, count in connection.execute(
            "SELECT status, COUNT(*) FROM prefix_queries GROUP BY status"
        )
    }
    years = {
        str(year): int(count)
        for year, count in connection.execute(
            """
            SELECT substr(published_at, 1, 4), COUNT(DISTINCT canonical_url)
            FROM prefix_candidates
            GROUP BY substr(published_at, 1, 4)
            ORDER BY 1
            """
        )
    }
    remaining = int(
        connection.execute(
            "SELECT COUNT(*) FROM prefix_queries WHERE status != 'complete'"
        ).fetchone()[0]
    )
    return {
        "formatVersion": SCHEMA_VERSION,
        "queryStatus": query_status,
        "articlesByYear": years,
        "queriesRemaining": remaining,
        "shouldContinue": remaining > 0,
    }


def _query_parameters(
    pattern: str,
    *,
    page_size: int | None = None,
) -> list[tuple[str, str]]:
    parameters = [
        ("url", pattern),
        ("output", "json"),
        ("filter", "status:200"),
        ("filter", "mime:text/html"),
        ("matchType", "prefix"),
        ("collapse", "urlkey"),
    ]
    if page_size is not None:
        parameters.append(("pageSize", str(page_size)))
    return parameters


def _write_row(
    handle,
    *,
    spec: ArchiveSourceSpec,
    canonical_url: str,
    published_at: str | None,
    candidates: list[dict[str, object]],
) -> None:
    handle.write(
        json.dumps(
            {
                "formatVersion": MANIFEST_FORMAT_VERSION,
                "publisher": spec.publisher,
                "canonicalUrl": canonical_url,
                **({"publishedAt": published_at} if published_at else {}),
                "candidates": candidates,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _fingerprint(
    *,
    spec: ArchiveSourceSpec,
    from_year: int,
    to_year: int,
    patterns: tuple[str, ...],
) -> str:
    value = json.dumps(
        {
            "publisher": spec.publisher,
            "fromYear": from_year,
            "toYear": to_year,
            "patterns": patterns,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_crawl_timestamp(value: str) -> datetime | None:
    if re.fullmatch(r"\d{14}", value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
