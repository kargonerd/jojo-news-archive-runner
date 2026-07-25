from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx
from dateutil.parser import isoparse

from .archive_sources import (
    ArchiveSourceSpec,
    archive_source_spec,
    normalize_article_url,
)
from .bloomberg_archive_download import GlobalRateLimiter
from .wayback_manifest import (
    MANIFEST_FORMAT_VERSION,
    infer_published_at,
    with_current_year_live_fallback,
)


SITEMAP_DISCOVERY_VERSION = "jojo-sitemap-discovery/1"
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class SitemapSource:
    publisher: str
    index_url: str
    child_pattern: re.Pattern[str]


SITEMAP_SOURCES = {
    "ap": SitemapSource(
        publisher="ap",
        index_url="https://apnews.com/ap-sitemap.xml",
        child_pattern=re.compile(r"ap-sitemap-(20\d{2})(\d{2})\.xml$"),
    ),
    "bloomberg": SitemapSource(
        publisher="bloomberg",
        index_url="https://www.bloomberg.com/sitemaps/news/index.xml",
        child_pattern=re.compile(r"/(20\d{2})-(\d{1,2})\.xml$"),
    ),
    "nyt": SitemapSource(
        publisher="nyt",
        index_url="https://www.nytimes.com/sitemaps/new/sitemap.xml.gz",
        child_pattern=re.compile(r"sitemap-(20\d{2})-(\d{2})\.xml\.gz$"),
    ),
    "ft": SitemapSource(
        publisher="ft",
        index_url="https://www.ft.com/sitemaps/index.xml",
        child_pattern=re.compile(r"archive-(20\d{2})-(\d{1,2})\.xml$"),
    ),
}


class SitemapClient:
    def __init__(
        self,
        *,
        minimum_interval: float = 1.0,
        timeout: float = 90.0,
        attempts: int = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self.rate_limiter = GlobalRateLimiter(minimum_interval)
        self.attempts = attempts
        self._provided_client = client
        self._client = client or httpx.Client(
            headers={
                "User-Agent": (
                    "JOJO-News-Archive-Research/0.1 "
                    "(authorized nonprofit academic archive)"
                )
            },
            follow_redirects=True,
            timeout=timeout,
        )

    def close(self) -> None:
        if self._provided_client is None:
            self._client.close()

    def fetch_xml(self, url: str) -> bytes:
        last_status: int | None = None
        for attempt in range(self.attempts):
            self.rate_limiter.wait()
            try:
                response = self._client.get(url)
                last_status = response.status_code
                if response.status_code in RETRYABLE_STATUS_CODES:
                    raise RuntimeError(f"retryable HTTP {response.status_code}")
                response.raise_for_status()
                content = response.content
                if content[:2] == b"\x1f\x8b":
                    content = gzip.decompress(content)
                return content
            except (httpx.HTTPError, OSError, RuntimeError):
                if attempt + 1 >= self.attempts:
                    break
                time.sleep(min(30.0, 2.0**attempt))
        raise RuntimeError(
            f"sitemap request failed after {self.attempts} attempts"
            + (f" (last HTTP status {last_status})" if last_status else "")
        )


def sitemap_source(publisher: str) -> SitemapSource:
    try:
        return SITEMAP_SOURCES[publisher]
    except KeyError as exc:
        supported = ", ".join(sorted(SITEMAP_SOURCES))
        raise ValueError(
            f"publisher {publisher!r} has no historical sitemap adapter; "
            f"expected one of: {supported}"
        ) from exc


def parse_sitemap_index(
    content: bytes,
    *,
    source: SitemapSource,
    from_year: int,
    to_year: int,
) -> list[tuple[str, int, int]]:
    root = ET.fromstring(content)
    result: list[tuple[str, int, int]] = []
    for node in root.iter():
        if _local_name(node.tag) != "loc" or not node.text:
            continue
        url = node.text.strip()
        match = source.child_pattern.search(urlsplit(url).path)
        if not match:
            continue
        year = int(match.group(1))
        month = int(match.group(2))
        if from_year <= year <= to_year and 1 <= month <= 12:
            result.append((url, year, month))
    return sorted(set(result), key=lambda value: (value[1], value[2], value[0]))


def parse_url_sitemap(content: bytes) -> list[tuple[str, str | None]]:
    root = ET.fromstring(content)
    result: list[tuple[str, str | None]] = []
    for entry in root:
        fields = {
            _local_name(child.tag): (child.text or "").strip()
            for child in entry
        }
        url = fields.get("loc")
        if url:
            result.append((url, fields.get("lastmod") or None))
    return result


def initialize_sitemap_schema(
    connection: sqlite3.Connection,
    *,
    source: SitemapSource,
    from_year: int,
    to_year: int,
    sitemap_index: bytes,
) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        CREATE TABLE IF NOT EXISTS sitemap_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sitemap_queries (
            sitemap_url TEXT PRIMARY KEY,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            rows_seen INTEGER NOT NULL DEFAULT 0,
            rows_accepted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sitemap_articles (
            canonical_url TEXT PRIMARY KEY,
            published_at TEXT,
            source_sitemap TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "publisher": source.publisher,
                "indexUrl": source.index_url,
                "fromYear": from_year,
                "toYear": to_year,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    existing = connection.execute(
        "SELECT value FROM sitemap_metadata WHERE key='fingerprint'"
    ).fetchone()
    if existing and existing[0] != fingerprint:
        raise ValueError(
            "sitemap state belongs to a different publisher or date window"
        )
    connection.executemany(
        """
        INSERT INTO sitemap_metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        {
            "schema_version": SITEMAP_DISCOVERY_VERSION,
            "publisher": source.publisher,
            "from_year": str(from_year),
            "to_year": str(to_year),
            "fingerprint": fingerprint,
        }.items(),
    )
    children = parse_sitemap_index(
        sitemap_index,
        source=source,
        from_year=from_year,
        to_year=to_year,
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO sitemap_queries(
            sitemap_url,
            year,
            month,
            updated_at
        ) VALUES (?, ?, ?, ?)
        """,
        [(url, year, month, _now_iso()) for url, year, month in children],
    )
    connection.commit()


def next_sitemap_query(
    connection: sqlite3.Connection,
) -> tuple[str, int, int] | None:
    row = connection.execute(
        """
        SELECT sitemap_url, year, month
        FROM sitemap_queries
        WHERE status != 'complete'
        ORDER BY year, month, sitemap_url
        LIMIT 1
        """
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def record_sitemap(
    connection: sqlite3.Connection,
    *,
    publisher_spec: ArchiveSourceSpec,
    sitemap_url: str,
    year: int,
    month: int,
    content: bytes,
) -> dict[str, int]:
    entries = parse_url_sitemap(content)
    rows: list[tuple[str, str | None, str, str]] = []
    for original_url, last_modified in entries:
        canonical_url = normalize_article_url(publisher_spec, original_url)
        if not canonical_url:
            continue
        published_at = infer_published_at(canonical_url)
        if not published_at:
            published_at = _published_from_sitemap(
                last_modified,
                year=year,
                month=month,
            )
        rows.append(
            (
                canonical_url,
                published_at,
                sitemap_url,
                _now_iso(),
            )
        )
    with connection:
        before = connection.total_changes
        connection.executemany(
            """
            INSERT INTO sitemap_articles(
                canonical_url,
                published_at,
                source_sitemap,
                updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                published_at=COALESCE(
                    sitemap_articles.published_at,
                    excluded.published_at
                ),
                source_sitemap=excluded.source_sitemap,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        accepted = connection.total_changes - before
        connection.execute(
            """
            UPDATE sitemap_queries
            SET status='complete',
                rows_seen=?,
                rows_accepted=?,
                updated_at=?
            WHERE sitemap_url=?
            """,
            (len(entries), accepted, _now_iso(), sitemap_url),
        )
    return {"seen": len(entries), "accepted": accepted}


def export_sitemap_manifest(
    connection: sqlite3.Connection,
    *,
    publisher: str,
    destination: Path,
    from_year: int,
    to_year: int,
) -> dict[str, object]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    opener = gzip.open if destination.suffix == ".gz" else open
    articles = 0
    candidates = 0
    with opener(temporary, "wt", encoding="utf-8") as handle:
        for canonical_url, published_at in connection.execute(
            """
            SELECT canonical_url, published_at
            FROM sitemap_articles
            ORDER BY canonical_url
            """
        ):
            candidate_rows = sitemap_wayback_candidates(
                publisher,
                canonical_url,
                published_at=published_at,
            )
            row = {
                "formatVersion": MANIFEST_FORMAT_VERSION,
                "publisher": publisher,
                "canonicalUrl": canonical_url,
                **({"publishedAt": published_at} if published_at else {}),
                "candidates": candidate_rows,
            }
            handle.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            articles += 1
            candidates += len(candidate_rows)
    temporary.replace(destination)
    incomplete = connection.execute(
        "SELECT COUNT(*) FROM sitemap_queries WHERE status != 'complete'"
    ).fetchone()[0]
    return {
        "publisher": publisher,
        "fromYear": from_year,
        "toYear": to_year,
        "complete": incomplete == 0,
        "shouldContinue": incomplete > 0,
        "remainingSitemaps": incomplete,
        "articles": articles,
        "candidates": candidates,
        "manifest": str(destination),
    }


def sitemap_summary(connection: sqlite3.Connection) -> dict[str, object]:
    counts = dict(
        connection.execute(
            "SELECT status, COUNT(*) FROM sitemap_queries GROUP BY status"
        ).fetchall()
    )
    totals = connection.execute(
        """
        SELECT
            COALESCE(SUM(rows_seen), 0),
            COALESCE(SUM(rows_accepted), 0)
        FROM sitemap_queries
        """
    ).fetchone()
    articles = connection.execute(
        "SELECT COUNT(*) FROM sitemap_articles"
    ).fetchone()[0]
    return {
        "sitemapsByStatus": counts,
        "rowsSeen": int(totals[0]),
        "rowsAccepted": int(totals[1]),
        "articles": int(articles),
        "shouldContinue": any(
            status != "complete" and count > 0
            for status, count in counts.items()
        ),
    }


def wayback_candidates(
    canonical_url: str,
    *,
    published_at: str | None,
) -> list[dict[str, object]]:
    if published_at:
        try:
            base = isoparse(published_at)
        except (TypeError, ValueError, OverflowError):
            base = None
    else:
        base = None
    if base is None:
        return [
            {
                "provider": "wayback",
                "snapshotUrl": (
                    "https://web.archive.org/web/2id_/" + canonical_url
                ),
            }
        ]
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    result: list[dict[str, object]] = []
    for delta in (timedelta(days=1), timedelta(days=7), timedelta(days=30)):
        requested = (base + delta).astimezone(timezone.utc)
        timestamp = requested.strftime("%Y%m%d%H%M%S")
        result.append(
            {
                "provider": "wayback",
                "snapshotUrl": (
                    f"https://web.archive.org/web/{timestamp}id_/"
                    f"{canonical_url}"
                ),
            }
        )
    return result


def sitemap_wayback_candidates(
    publisher: str,
    canonical_url: str,
    *,
    published_at: str | None,
) -> list[dict[str, object]]:
    source_urls: list[str] = []
    parsed = urlsplit(canonical_url)
    if (
        publisher == "ft"
        and parsed.hostname in {"ft.com", "www.ft.com"}
        and parsed.path.startswith("/content/")
    ):
        source_urls.append(f"https://amp.ft.com{parsed.path}")
    source_urls.append(canonical_url)

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for source_url in source_urls:
        for candidate in wayback_candidates(
            source_url,
            published_at=published_at,
        ):
            snapshot_url = str(candidate["snapshotUrl"])
            if snapshot_url in seen:
                continue
            seen.add(snapshot_url)
            result.append(candidate)
    if publisher == "ap":
        result.append(
            {
                "provider": "live-origin",
                "snapshotUrl": canonical_url,
            }
        )
        return result
    return with_current_year_live_fallback(
        result,
        canonical_url=canonical_url,
        published_at=published_at,
    )


def _published_from_sitemap(
    value: str | None,
    *,
    year: int,
    month: int,
) -> str:
    if value:
        try:
            parsed = isoparse(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            # Archive sitemap lastmod values can be refreshed long after
            # publication. Accept them only when they match the sitemap month.
            if parsed.year == year and parsed.month == month:
                return parsed.isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime(year, month, 15, 12, tzinfo=timezone.utc).isoformat()


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
