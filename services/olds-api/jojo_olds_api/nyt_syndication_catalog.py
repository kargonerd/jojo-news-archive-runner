from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .archive_sources import archive_source_spec, normalize_article_url
from .wayback_manifest import infer_published_at


SCHEMA_VERSION = "jojo-nyt-syndication-catalog/1"
DEFAULT_ENDPOINT = (
    "https://www.hawaiitribune-herald.com/wp-json/wp/v2/posts"
)
DEFAULT_TAG_ID = 768
PAGE_SIZE = 100
MAXIMUM_RESPONSE_BYTES = 10_000_000


def initialize_nyt_syndication_schema(
    connection: sqlite3.Connection,
    *,
    from_year: int,
    to_year: int,
    endpoint: str = DEFAULT_ENDPOINT,
    tag_id: int = DEFAULT_TAG_ID,
) -> None:
    if from_year > to_year:
        raise ValueError("from_year must not exceed to_year")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS nyt_syndication_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS nyt_syndication_queries (
            year INTEGER NOT NULL,
            page INTEGER NOT NULL,
            request_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            rows_seen INTEGER NOT NULL DEFAULT 0,
            rows_accepted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(year, page)
        );

        CREATE TABLE IF NOT EXISTS nyt_syndication_articles (
            canonical_url TEXT PRIMARY KEY,
            published_at TEXT NOT NULL,
            syndicated_url TEXT NOT NULL,
            partner_published_at TEXT,
            source_endpoint TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_nyt_syndication_articles_published
        ON nyt_syndication_articles(published_at);
        """
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "endpoint": endpoint,
                "tagId": tag_id,
                "fromYear": from_year,
                "toYear": to_year,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    existing = connection.execute(
        """
        SELECT value
        FROM nyt_syndication_metadata
        WHERE key='fingerprint'
        """
    ).fetchone()
    if existing and existing[0] != fingerprint:
        raise ValueError(
            "NYT syndication state belongs to a different source or window"
        )
    connection.executemany(
        """
        INSERT INTO nyt_syndication_metadata(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        {
            "schema_version": SCHEMA_VERSION,
            "endpoint": endpoint,
            "tag_id": str(tag_id),
            "from_year": str(from_year),
            "to_year": str(to_year),
            "fingerprint": fingerprint,
        }.items(),
    )
    now = _now_iso()
    connection.executemany(
        """
        INSERT OR IGNORE INTO nyt_syndication_queries(
            year,
            page,
            request_url,
            updated_at
        ) VALUES (?, 1, ?, ?)
        """,
        (
            (
                year,
                nyt_syndication_page_url(
                    year=year,
                    page=1,
                    endpoint=endpoint,
                    tag_id=tag_id,
                ),
                now,
            )
            for year in range(from_year, to_year + 1)
        ),
    )
    connection.commit()


def nyt_syndication_page_url(
    *,
    year: int,
    page: int,
    endpoint: str = DEFAULT_ENDPOINT,
    tag_id: int = DEFAULT_TAG_ID,
) -> str:
    if page < 1:
        raise ValueError("page must be positive")
    return endpoint + "?" + urlencode(
        {
            "tags": tag_id,
            "after": f"{year:04d}-01-01T00:00:00",
            "before": f"{year + 1:04d}-01-01T00:00:00",
            "per_page": PAGE_SIZE,
            "page": page,
            "orderby": "date",
            "order": "asc",
            "_fields": "date,date_gmt,link,title,content",
        }
    )


def next_nyt_syndication_query(
    connection: sqlite3.Connection,
) -> tuple[int, int, str] | None:
    row = connection.execute(
        """
        SELECT year, page, request_url
        FROM nyt_syndication_queries
        WHERE status != 'complete'
        ORDER BY year DESC, page
        LIMIT 1
        """
    ).fetchone()
    return (int(row[0]), int(row[1]), str(row[2])) if row else None


def record_nyt_syndication_page(
    connection: sqlite3.Connection,
    *,
    year: int,
    page: int,
    request_url: str,
    content: bytes,
    total_pages: int,
    endpoint: str = DEFAULT_ENDPOINT,
    tag_id: int = DEFAULT_TAG_ID,
) -> dict[str, int]:
    if total_pages < 0:
        raise ValueError("total_pages must not be negative")
    payload = json.loads(content)
    if not isinstance(payload, list):
        raise ValueError("NYT syndication page must be a JSON list")
    accepted: list[tuple[str, str, str, str | None, str, str]] = []
    for row in payload:
        parsed = parse_nyt_syndication_post(
            row,
            source_endpoint=endpoint,
        )
        if parsed is None:
            continue
        canonical_url, published_at, syndicated_url, partner_published_at = (
            parsed
        )
        if not published_at.startswith(f"{year:04d}-"):
            continue
        accepted.append(
            (
                canonical_url,
                published_at,
                syndicated_url,
                partner_published_at,
                endpoint,
                _now_iso(),
            )
        )

    now = _now_iso()
    with connection:
        connection.executemany(
            """
            INSERT INTO nyt_syndication_articles(
                canonical_url,
                published_at,
                syndicated_url,
                partner_published_at,
                source_endpoint,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                published_at=excluded.published_at,
                syndicated_url=excluded.syndicated_url,
                partner_published_at=excluded.partner_published_at,
                source_endpoint=excluded.source_endpoint,
                updated_at=excluded.updated_at
            """,
            accepted,
        )
        connection.execute(
            """
            UPDATE nyt_syndication_queries
            SET status='complete',
                rows_seen=?,
                rows_accepted=?,
                updated_at=?
            WHERE year=? AND page=? AND request_url=?
            """,
            (
                len(payload),
                len(accepted),
                now,
                year,
                page,
                request_url,
            ),
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO nyt_syndication_queries(
                year,
                page,
                request_url,
                updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (
                    year,
                    next_page,
                    nyt_syndication_page_url(
                        year=year,
                        page=next_page,
                        endpoint=endpoint,
                        tag_id=tag_id,
                    ),
                    now,
                )
                for next_page in range(2, total_pages + 1)
            ),
        )
    return {
        "seen": len(payload),
        "accepted": len(accepted),
        "totalPages": total_pages,
    }


def parse_nyt_syndication_post(
    value: object,
    *,
    source_endpoint: str = DEFAULT_ENDPOINT,
) -> tuple[str, str, str, str | None] | None:
    if not isinstance(value, dict):
        return None
    syndicated_url = value.get("link")
    content_value = value.get("content")
    if (
        not isinstance(syndicated_url, str)
        or not isinstance(content_value, dict)
    ):
        return None
    rendered = content_value.get("rendered")
    if not isinstance(rendered, str):
        return None
    partner_published_at = value.get("date_gmt") or value.get("date")
    if not isinstance(partner_published_at, str):
        partner_published_at = None
    partner_date = _parse_datetime(partner_published_at)

    publisher_spec = archive_source_spec("nyt")
    candidates: list[tuple[int, int, str, str]] = []
    soup = BeautifulSoup(rendered, "html.parser")
    for position, anchor in enumerate(soup.select("a[href]")):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        canonical_url = normalize_article_url(publisher_spec, href)
        if not canonical_url:
            continue
        published_at = infer_published_at(canonical_url)
        if published_at is None:
            continue
        published_date = _parse_datetime(published_at)
        if (
            partner_date is not None
            and published_date is not None
            and abs((partner_date.date() - published_date.date()).days) > 2
        ):
            continue
        context = anchor.parent.get_text(" ", strip=True).casefold()
        source_priority = (
            0
            if "originally appeared" in context
            else 1
            if "new york times" in anchor.get_text(" ", strip=True).casefold()
            else 2
        )
        candidates.append(
            (
                source_priority,
                -position,
                canonical_url,
                published_at,
            )
        )
    if not candidates:
        return None
    _, _, canonical_url, published_at = min(candidates)
    return (
        canonical_url,
        published_at,
        syndicated_url,
        partner_published_at,
    )


def nyt_syndication_articles(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str]]:
    if not _table_exists(connection, "nyt_syndication_articles"):
        return {}
    return {
        str(canonical_url): (str(published_at), str(syndicated_url))
        for canonical_url, published_at, syndicated_url in connection.execute(
            """
            SELECT canonical_url, published_at, syndicated_url
            FROM nyt_syndication_articles
            ORDER BY canonical_url
            """
        )
    }


def nyt_syndication_summary(
    connection: sqlite3.Connection,
) -> dict[str, object]:
    if not _table_exists(connection, "nyt_syndication_queries"):
        return {
            "queriesByStatus": {},
            "articles": 0,
            "shouldContinue": False,
        }
    counts = dict(
        connection.execute(
            """
            SELECT status, COUNT(*)
            FROM nyt_syndication_queries
            GROUP BY status
            """
        ).fetchall()
    )
    articles = int(
        connection.execute(
            "SELECT COUNT(*) FROM nyt_syndication_articles"
        ).fetchone()[0]
    )
    return {
        "queriesByStatus": counts,
        "articles": articles,
        "shouldContinue": any(
            status != "complete" and count > 0
            for status, count in counts.items()
        ),
    }


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name=?
            """,
            (name,),
        ).fetchone()
        is not None
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
