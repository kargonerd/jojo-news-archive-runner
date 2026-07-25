from __future__ import annotations

from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sqlite3
from typing import Iterable

import httpx
from dateutil.parser import isoparse

from .archive_sources import archive_source_spec, normalize_article_url
from .bloomberg_archive_download import ArchiveClient
from .sitemap_manifest import parse_url_sitemap, wayback_candidates
from .wayback_manifest import (
    CDX_ENDPOINT,
    MANIFEST_FORMAT_VERSION,
    infer_published_at,
    parse_cdx_json,
)


REUTERS_SITEMAP_DISCOVERY_VERSION = "jojo-reuters-sitemap-discovery/1"
SITEMAP_CDX_PATTERN = (
    "www.reuters.com/arc/outboundfeeds/sitemap/*"
)
SITEMAP_ORIGINAL_FILTER = (
    r"original:.*outboundfeeds/sitemap/\?outputType=xml.*"
)


def discover_reuters_sitemap_captures(
    *,
    from_year: int,
    to_year: int,
    timeout: float = 90.0,
    client: httpx.Client | None = None,
) -> list[dict[str, object]]:
    provided = client is not None
    http_client = client or httpx.Client(
        headers={
            "User-Agent": (
                "JOJO-News-Archive-Research/0.1 "
                "(authorized nonprofit academic archive)"
            )
        },
        follow_redirects=True,
        timeout=timeout,
    )
    try:
        parameters: list[tuple[str, str]] = [
            ("url", SITEMAP_CDX_PATTERN),
            ("output", "json"),
            (
                "fl",
                "timestamp,original,mimetype,statuscode,digest,length",
            ),
            ("filter", "statuscode:200"),
            ("filter", SITEMAP_ORIGINAL_FILTER),
            ("collapse", "digest"),
            ("from", str(from_year)),
            ("to", str(to_year)),
            ("limit", "5000"),
            ("showResumeKey", "true"),
        ]
        response = http_client.get(CDX_ENDPOINT, params=parameters)
        response.raise_for_status()
        page = parse_cdx_json(response.text)
        if page.resume_key:
            raise RuntimeError(
                "Reuters sitemap CDX query exceeded 5,000 rows; "
                "resume-key pagination must be added"
            )
        return [
            {
                "timestamp": capture.timestamp,
                "originalUrl": html.unescape(capture.original),
                "digest": capture.digest or "",
                "byteCount": capture.length,
            }
            for capture in page.captures
        ]
    finally:
        if not provided:
            http_client.close()


def initialize_reuters_sitemap_schema(
    connection: sqlite3.Connection,
    *,
    from_year: int,
    to_year: int,
    captures: Iterable[dict[str, object]],
) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS reuters_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reuters_sitemap_captures (
            snapshot_url TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            original_url TEXT NOT NULL,
            digest TEXT NOT NULL,
            byte_count INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            rows_seen INTEGER NOT NULL DEFAULT 0,
            rows_accepted INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reuters_articles (
            canonical_url TEXT PRIMARY KEY,
            published_at TEXT,
            source_snapshot_url TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    existing = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT key, value FROM reuters_metadata"
        )
    }
    if existing and (
        existing.get("from_year") != str(from_year)
        or existing.get("to_year") != str(to_year)
    ):
        raise ValueError(
            "Reuters sitemap state belongs to a different date window"
        )
    connection.executemany(
        """
        INSERT INTO reuters_metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        {
            "schema_version": REUTERS_SITEMAP_DISCOVERY_VERSION,
            "publisher": "reuters",
            "from_year": str(from_year),
            "to_year": str(to_year),
        }.items(),
    )
    rows = []
    for capture in captures:
        timestamp = str(capture["timestamp"])
        original_url = str(capture["originalUrl"])
        snapshot_url = (
            f"https://web.archive.org/web/{timestamp}id_/{original_url}"
        )
        rows.append(
            (
                snapshot_url,
                timestamp,
                original_url,
                str(capture.get("digest") or ""),
                capture.get("byteCount"),
                _now_iso(),
            )
        )
    connection.executemany(
        """
        INSERT OR IGNORE INTO reuters_sitemap_captures(
            snapshot_url,
            timestamp,
            original_url,
            digest,
            byte_count,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.execute(
        """
        UPDATE reuters_sitemap_captures
        SET status='pending',
            last_error='interrupted before completion',
            updated_at=?
        WHERE status='processing'
        """,
        (_now_iso(),),
    )
    connection.commit()


def pending_reuters_sitemaps(
    connection: sqlite3.Connection,
    *,
    maximum: int,
    maximum_attempts: int,
) -> list[tuple[str, int]]:
    return connection.execute(
        """
        SELECT snapshot_url, attempts
        FROM reuters_sitemap_captures
        WHERE status='pending'
           OR (status='error' AND attempts < ?)
        ORDER BY timestamp, original_url
        LIMIT ?
        """,
        (maximum_attempts, maximum),
    ).fetchall()


def process_reuters_sitemap(
    connection: sqlite3.Connection,
    *,
    snapshot_url: str,
    archive_client: ArchiveClient,
    from_year: int,
    to_year: int,
    maximum_bytes: int = 10 * 1024 * 1024,
) -> dict[str, object]:
    with connection:
        connection.execute(
            """
            UPDATE reuters_sitemap_captures
            SET status='processing',
                attempts=attempts+1,
                last_error=NULL,
                updated_at=?
            WHERE snapshot_url=?
            """,
            (_now_iso(), snapshot_url),
        )
    try:
        status, _, content, _ = archive_client.fetch(
            snapshot_url,
            maximum_bytes=maximum_bytes,
        )
        if status not in {200, 206}:
            raise RuntimeError(f"HTTP {status}")
        entries = parse_url_sitemap(content)
        rows: list[tuple[str, str | None, str, str]] = []
        publisher_spec = archive_source_spec("reuters")
        for original_url, last_modified in entries:
            canonical_url = normalize_article_url(
                publisher_spec,
                original_url,
            )
            if not canonical_url:
                continue
            published_at = infer_published_at(canonical_url)
            if not published_at:
                published_at = _valid_last_modified(
                    last_modified,
                    from_year=from_year,
                    to_year=to_year,
                )
            if not published_at:
                continue
            published_year = isoparse(published_at).year
            if not from_year <= published_year <= to_year:
                continue
            rows.append(
                (
                    canonical_url,
                    published_at,
                    snapshot_url,
                    _now_iso(),
                )
            )
        with connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO reuters_articles(
                    canonical_url,
                    published_at,
                    source_snapshot_url,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                    published_at=COALESCE(
                        reuters_articles.published_at,
                        excluded.published_at
                    ),
                    source_snapshot_url=excluded.source_snapshot_url,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            accepted = connection.total_changes - before
            connection.execute(
                """
                UPDATE reuters_sitemap_captures
                SET status='complete',
                    rows_seen=?,
                    rows_accepted=?,
                    updated_at=?
                WHERE snapshot_url=?
                """,
                (len(entries), accepted, _now_iso(), snapshot_url),
            )
        return {
            "status": "complete",
            "seen": len(entries),
            "accepted": accepted,
        }
    except Exception as exc:
        with connection:
            connection.execute(
                """
                UPDATE reuters_sitemap_captures
                SET status='error',
                    last_error=?,
                    updated_at=?
                WHERE snapshot_url=?
                """,
                (f"{type(exc).__name__}: {exc}", _now_iso(), snapshot_url),
            )
        return {
            "status": "error",
            "seen": 0,
            "accepted": 0,
        }


def export_reuters_manifest(
    connection: sqlite3.Connection,
    *,
    destination: Path,
    from_year: int,
    to_year: int,
    maximum_attempts: int,
) -> dict[str, object]:
    import gzip

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    opener = gzip.open if destination.suffix == ".gz" else open
    articles = 0
    candidates = 0
    with opener(temporary, "wt", encoding="utf-8") as handle:
        for canonical_url, published_at in connection.execute(
            """
            SELECT canonical_url, published_at
            FROM reuters_articles
            ORDER BY canonical_url
            """
        ):
            candidate_rows = wayback_candidates(
                canonical_url,
                published_at=published_at,
            )
            row = {
                "formatVersion": MANIFEST_FORMAT_VERSION,
                "publisher": "reuters",
                "canonicalUrl": canonical_url,
                "publishedAt": published_at,
                "candidates": candidate_rows,
            }
            handle.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            articles += 1
            candidates += len(candidate_rows)
    temporary.replace(destination)
    actionable = connection.execute(
        """
        SELECT COUNT(*)
        FROM reuters_sitemap_captures
        WHERE status='pending'
           OR (status='error' AND attempts < ?)
        """,
        (maximum_attempts,),
    ).fetchone()[0]
    terminal_errors = connection.execute(
        """
        SELECT COUNT(*)
        FROM reuters_sitemap_captures
        WHERE status='error' AND attempts >= ?
        """,
        (maximum_attempts,),
    ).fetchone()[0]
    return {
        "publisher": "reuters",
        "fromYear": from_year,
        "toYear": to_year,
        "complete": actionable == 0,
        "shouldContinue": actionable > 0,
        "remainingSitemaps": actionable,
        "terminalSitemapErrors": terminal_errors,
        "articles": articles,
        "candidates": candidates,
        "manifest": str(destination),
    }


def reuters_sitemap_summary(
    connection: sqlite3.Connection,
) -> dict[str, object]:
    statuses = dict(
        connection.execute(
            """
            SELECT status, COUNT(*)
            FROM reuters_sitemap_captures
            GROUP BY status
            """
        ).fetchall()
    )
    totals = connection.execute(
        """
        SELECT
            COALESCE(SUM(rows_seen), 0),
            COALESCE(SUM(rows_accepted), 0)
        FROM reuters_sitemap_captures
        """
    ).fetchone()
    articles = connection.execute(
        "SELECT COUNT(*) FROM reuters_articles"
    ).fetchone()[0]
    return {
        "sitemapsByStatus": statuses,
        "rowsSeen": int(totals[0]),
        "rowsAccepted": int(totals[1]),
        "articles": int(articles),
    }


def _valid_last_modified(
    value: str | None,
    *,
    from_year: int,
    to_year: int,
) -> str | None:
    if not value:
        return None
    try:
        parsed = isoparse(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not from_year <= parsed.year <= to_year:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
