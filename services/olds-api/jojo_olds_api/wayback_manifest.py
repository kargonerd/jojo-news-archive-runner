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
from typing import Iterable

import httpx

from .archive_sources import ArchiveSourceSpec, normalize_article_url
from .bloomberg_archive_download import GlobalRateLimiter


CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
DISCOVERY_SCHEMA_VERSION = "jojo-wayback-discovery/1"
MANIFEST_FORMAT_VERSION = "jojo-capture-manifest/1"


@dataclass(frozen=True)
class CDXCapture:
    timestamp: str
    original: str
    mimetype: str
    status_code: int
    digest: str | None
    length: int | None

    @property
    def snapshot_url(self) -> str:
        return (
            f"https://web.archive.org/web/{self.timestamp}id_/{self.original}"
        )


@dataclass(frozen=True)
class CDXPage:
    captures: tuple[CDXCapture, ...]
    resume_key: str | None


class WaybackCDXClient:
    def __init__(
        self,
        *,
        minimum_interval: float = 1.0,
        timeout: float = 90.0,
        attempts: int = 6,
        page_limit: int = 10_000,
        client: httpx.Client | None = None,
    ) -> None:
        self.rate_limiter = GlobalRateLimiter(minimum_interval)
        self.timeout = timeout
        self.attempts = attempts
        self.page_limit = page_limit
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

    def fetch_page(
        self,
        *,
        pattern: str,
        from_year: int,
        to_year: int,
        resume_key: str | None,
    ) -> CDXPage:
        parameters: list[tuple[str, str]] = [
            ("url", pattern),
            ("output", "json"),
            (
                "fl",
                "timestamp,original,mimetype,statuscode,digest,length",
            ),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:text/html"),
            ("collapse", "digest"),
            ("from", str(from_year)),
            ("to", str(to_year)),
            ("limit", str(self.page_limit)),
            ("showResumeKey", "true"),
        ]
        if resume_key:
            parameters.append(("resumeKey", resume_key))
        last_status: int | None = None
        for attempt in range(self.attempts):
            self.rate_limiter.wait()
            try:
                response = self._client.get(CDX_ENDPOINT, params=parameters)
                last_status = response.status_code
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise RuntimeError(f"retryable HTTP {response.status_code}")
                response.raise_for_status()
                return parse_cdx_json(response.text)
            except (httpx.HTTPError, RuntimeError, ValueError):
                if attempt + 1 >= self.attempts:
                    break
                time.sleep(min(60.0, 2.0**attempt))
        raise RuntimeError(
            f"Wayback CDX query failed after {self.attempts} attempts"
            + (f" (last HTTP status {last_status})" if last_status else "")
        )


def parse_cdx_json(value: str) -> CDXPage:
    payload = json.loads(value)
    if not isinstance(payload, list) or not payload:
        return CDXPage(captures=(), resume_key=None)
    header = payload[0]
    expected = [
        "timestamp",
        "original",
        "mimetype",
        "statuscode",
        "digest",
        "length",
    ]
    if header != expected:
        raise ValueError(f"unexpected CDX header: {header!r}")
    captures: list[CDXCapture] = []
    resume_key: str | None = None
    for row in payload[1:]:
        if row == []:
            continue
        if isinstance(row, list) and len(row) == 1:
            resume_key = str(row[0]) or None
            continue
        if not isinstance(row, list) or len(row) != len(expected):
            raise ValueError(f"unexpected CDX row: {row!r}")
        captures.append(
            CDXCapture(
                timestamp=str(row[0]),
                original=str(row[1]),
                mimetype=str(row[2]),
                status_code=int(row[3]),
                digest=str(row[4]) if row[4] not in {None, "-"} else None,
                length=int(row[5]) if str(row[5]).isdigit() else None,
            )
        )
    return CDXPage(captures=tuple(captures), resume_key=resume_key)


def initialize_discovery_schema(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    from_year: int,
    to_year: int,
) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS discovery_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS discovery_queries (
            pattern TEXT PRIMARY KEY,
            resume_key TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            pages INTEGER NOT NULL DEFAULT 0,
            rows_seen INTEGER NOT NULL DEFAULT 0,
            rows_accepted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS candidates (
            canonical_url TEXT NOT NULL,
            published_at TEXT,
            timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            digest TEXT NOT NULL DEFAULT '',
            mimetype TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            byte_count INTEGER,
            rank_score INTEGER NOT NULL,
            PRIMARY KEY(canonical_url, timestamp, digest)
        );

        CREATE INDEX IF NOT EXISTS idx_candidates_canonical_rank
            ON candidates(canonical_url, rank_score, timestamp);
        """
    )
    fingerprint = _spec_fingerprint(spec, from_year=from_year, to_year=to_year)
    existing = connection.execute(
        "SELECT value FROM discovery_metadata WHERE key='fingerprint'"
    ).fetchone()
    if existing and existing[0] != fingerprint:
        raise ValueError(
            "discovery state belongs to a different publisher, date window, or spec"
        )
    metadata = {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "publisher": spec.publisher,
        "from_year": str(from_year),
        "to_year": str(to_year),
        "fingerprint": fingerprint,
    }
    connection.executemany(
        """
        INSERT INTO discovery_metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        metadata.items(),
    )
    patterns = spec.expanded_wayback_patterns(
        from_year=from_year,
        to_year=to_year,
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO discovery_queries(pattern, updated_at)
        VALUES (?, ?)
        """,
        [(pattern, _now_iso()) for pattern in patterns],
    )
    connection.commit()


def next_discovery_query(
    connection: sqlite3.Connection,
) -> tuple[str, str | None] | None:
    row = connection.execute(
        """
        SELECT pattern, resume_key
        FROM discovery_queries
        WHERE status != 'complete'
        ORDER BY pattern
        LIMIT 1
        """
    ).fetchone()
    return (row[0], row[1]) if row else None


def record_discovery_page(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    pattern: str,
    page: CDXPage,
) -> dict[str, int | bool]:
    accepted = 0
    rows: list[tuple[object, ...]] = []
    touched_urls: set[str] = set()
    for capture in page.captures:
        canonical_url = normalize_article_url(spec, capture.original)
        if not canonical_url:
            continue
        published_at = infer_published_at(canonical_url)
        rows.append(
            (
                canonical_url,
                published_at,
                capture.timestamp,
                capture.original,
                capture.digest or "",
                capture.mimetype,
                capture.status_code,
                capture.length,
                candidate_rank(capture.timestamp, published_at=published_at),
            )
        )
        touched_urls.add(canonical_url)
    with connection:
        before = connection.total_changes
        connection.executemany(
            """
            INSERT OR IGNORE INTO candidates(
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
            rows,
        )
        accepted = connection.total_changes - before
        if touched_urls:
            placeholders = ",".join("?" for _ in touched_urls)
            connection.execute(
                f"""
                DELETE FROM candidates
                WHERE rowid IN (
                    SELECT rowid FROM (
                        SELECT
                            rowid,
                            ROW_NUMBER() OVER (
                                PARTITION BY canonical_url
                                ORDER BY rank_score, timestamp, digest
                            ) AS candidate_number
                        FROM candidates
                        WHERE canonical_url IN ({placeholders})
                    )
                    WHERE candidate_number > 3
                )
                """,
                sorted(touched_urls),
            )
        connection.execute(
            """
            UPDATE discovery_queries
            SET resume_key=?,
                status=?,
                pages=pages+1,
                rows_seen=rows_seen+?,
                rows_accepted=rows_accepted+?,
                updated_at=?
            WHERE pattern=?
            """,
            (
                page.resume_key,
                "running" if page.resume_key else "complete",
                len(page.captures),
                accepted,
                _now_iso(),
                pattern,
            ),
        )
    return {
        "seen": len(page.captures),
        "accepted": accepted,
        "hasMore": bool(page.resume_key),
    }


def export_capture_manifest(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    destination: Path,
    from_year: int,
    to_year: int,
) -> dict[str, int | bool | str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        """
        SELECT
            canonical_url,
            published_at,
            timestamp,
            original_url,
            digest,
            mimetype,
            status_code,
            byte_count
        FROM candidates
        ORDER BY canonical_url, rank_score, timestamp, digest
        """
    )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    opener = gzip.open if destination.suffix == ".gz" else open
    article_count = 0
    candidate_count = 0
    with opener(temporary, "wt", encoding="utf-8") as handle:
        current_url: str | None = None
        current_published_at: str | None = None
        candidates: list[dict] = []
        for row in rows:
            canonical_url = row[0]
            if current_url is not None and canonical_url != current_url:
                _write_manifest_row(
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
            current_published_at = row[1]
            captured_at = _timestamp_datetime(row[2])
            candidates.append(
                {
                    "provider": "wayback",
                    "snapshotUrl": (
                        f"https://web.archive.org/web/{row[2]}id_/{row[3]}"
                    ),
                    "capturedAt": captured_at.isoformat(),
                    **({"digest": row[4]} if row[4] else {}),
                    "mimeType": row[5],
                    "statusCode": row[6],
                    **({"byteCount": row[7]} if row[7] is not None else {}),
                }
            )
        if current_url is not None:
            _write_manifest_row(
                handle,
                spec=spec,
                canonical_url=current_url,
                published_at=current_published_at,
                candidates=candidates,
            )
            article_count += 1
            candidate_count += len(candidates)
    temporary.replace(destination)
    incomplete = connection.execute(
        "SELECT COUNT(*) FROM discovery_queries WHERE status != 'complete'"
    ).fetchone()[0]
    return {
        "publisher": spec.publisher,
        "fromYear": from_year,
        "toYear": to_year,
        "complete": incomplete == 0,
        "remainingQueries": incomplete,
        "articles": article_count,
        "candidates": candidate_count,
        "manifest": str(destination),
    }


def discovery_summary(connection: sqlite3.Connection) -> dict[str, object]:
    query_counts = dict(
        connection.execute(
            "SELECT status, COUNT(*) FROM discovery_queries GROUP BY status"
        ).fetchall()
    )
    totals = connection.execute(
        """
        SELECT
            COALESCE(SUM(pages), 0),
            COALESCE(SUM(rows_seen), 0),
            COALESCE(SUM(rows_accepted), 0)
        FROM discovery_queries
        """
    ).fetchone()
    articles = connection.execute(
        "SELECT COUNT(DISTINCT canonical_url) FROM candidates"
    ).fetchone()[0]
    return {
        "queriesByStatus": query_counts,
        "pages": int(totals[0]),
        "rowsSeen": int(totals[1]),
        "rowsAccepted": int(totals[2]),
        "articles": int(articles),
        "shouldContinue": sum(
            count
            for status, count in query_counts.items()
            if status != "complete"
        )
        > 0,
    }


def infer_published_at(canonical_url: str) -> str | None:
    patterns = (
        r"/(20\d{2})/(\d{2})/(\d{2})(?:/|$)",
        r"/articles/(20\d{2})-(\d{2})-(\d{2})(?:/|$)",
        r"-(20\d{2})-(\d{2})-(\d{2})(?:/|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, canonical_url)
        if not match:
            continue
        try:
            value = datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None
        return value.isoformat()
    return None


def candidate_rank(timestamp: str, *, published_at: str | None) -> int:
    captured = _timestamp_datetime(timestamp)
    if not published_at:
        return int(timestamp)
    published = datetime.fromisoformat(published_at)
    difference = int((captured - published).total_seconds())
    if difference >= 0:
        return difference
    return abs(difference) + 20 * 365 * 24 * 60 * 60


def _write_manifest_row(
    handle,
    *,
    spec: ArchiveSourceSpec,
    canonical_url: str,
    published_at: str | None,
    candidates: list[dict],
) -> None:
    row = {
        "formatVersion": MANIFEST_FORMAT_VERSION,
        "publisher": spec.publisher,
        "canonicalUrl": canonical_url,
        **({"publishedAt": published_at} if published_at else {}),
        "candidates": candidates,
    }
    handle.write(
        json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _timestamp_datetime(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def _spec_fingerprint(
    spec: ArchiveSourceSpec,
    *,
    from_year: int,
    to_year: int,
) -> str:
    value = json.dumps(
        {
            "publisher": spec.publisher,
            "fromYear": from_year,
            "toYear": to_year,
            "patterns": spec.expanded_wayback_patterns(
                from_year=from_year,
                to_year=to_year,
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
