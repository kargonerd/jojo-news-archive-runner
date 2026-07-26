from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable
from xml.etree import ElementTree

import httpx

from .archive_sources import ArchiveSourceSpec, normalize_article_url
from .bloomberg_archive_download import GlobalRateLimiter


CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
DISCOVERY_SCHEMA_VERSION = "jojo-wayback-discovery/1"
MANIFEST_FORMAT_VERSION = "jojo-capture-manifest/1"
WSJ_BLUESKY_ENDPOINT = (
    "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
)
WSJ_BLUESKY_START_YEAR = 2024
WSJ_CATALOG_TARGET_PER_YEAR = 750
WSJ_GOOGLE_NEWS_YEARS = (2023, 2024)
WSJ_GOOGLE_NEWS_MINIMUM_CATALOG = 750
WSJ_GOOGLE_NEWS_MAXIMUM_DECODES = 25
GOOGLE_NEWS_RSS_ENDPOINT = "https://news.google.com/rss/search"
GOOGLE_NEWS_DECODE_ENDPOINT = (
    "https://news.google.com/_/DotsSplashUi/data/batchexecute"
)
WSJ_RSS_ENDPOINTS = (
    "https://feeds.content.dowjones.io/public/rss/RSSOpinion",
    "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
    "https://feeds.content.dowjones.io/public/rss/RSSWSJD",
    "https://feeds.content.dowjones.io/public/rss/RSSLifestyle",
    "https://feeds.content.dowjones.io/public/rss/RSSUSnews",
    "https://feeds.content.dowjones.io/public/rss/socialpoliticsfeed",
    "https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
    "https://feeds.content.dowjones.io/public/rss/RSSArtsCulture",
    "https://feeds.content.dowjones.io/public/rss/latestnewsrealestate",
    "https://feeds.content.dowjones.io/public/rss/RSSPersonalFinance",
    "https://feeds.content.dowjones.io/public/rss/socialhealth",
    "https://feeds.content.dowjones.io/public/rss/RSSStyle",
    "https://feeds.content.dowjones.io/public/rss/rsssportsfeed",
)
PARSER_VALIDATION_CATALOG_MINIMUM_PER_YEAR = 750


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
        collapse: str = "digest",
        client: httpx.Client | None = None,
    ) -> None:
        if collapse not in {"digest", "urlkey"}:
            raise ValueError("collapse must be 'digest' or 'urlkey'")
        self.rate_limiter = GlobalRateLimiter(minimum_interval)
        self.timeout = timeout
        self.attempts = attempts
        self.page_limit = page_limit
        self.collapse = collapse
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
            ("collapse", self.collapse),
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
    collapse: str = "digest",
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
    fingerprint = _spec_fingerprint(
        spec,
        from_year=from_year,
        to_year=to_year,
        collapse=collapse,
    )
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
        "collapse": collapse,
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


def initialize_wsj_bluesky_schema(
    connection: sqlite3.Connection,
) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS wsj_bluesky_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            cursor TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            pages INTEGER NOT NULL DEFAULT 0,
            posts_seen INTEGER NOT NULL DEFAULT 0,
            urls_accepted INTEGER NOT NULL DEFAULT 0,
            oldest_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wsj_bluesky_articles (
            canonical_url TEXT PRIMARY KEY,
            published_at TEXT NOT NULL,
            post_uri TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO wsj_bluesky_state(
            singleton,
            updated_at
        ) VALUES (1, ?)
        """,
        (_now_iso(),),
    )
    connection.execute(
        """
        UPDATE wsj_bluesky_state
        SET status='running',
            last_error='interrupted before completion',
            updated_at=?
        WHERE singleton=1 AND status='processing'
        """,
        (_now_iso(),),
    )
    connection.commit()


def initialize_wsj_rss_schema(
    connection: sqlite3.Connection,
) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS wsj_rss_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            polls INTEGER NOT NULL DEFAULT 0,
            feeds_checked INTEGER NOT NULL DEFAULT 0,
            items_seen INTEGER NOT NULL DEFAULT 0,
            urls_accepted INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wsj_rss_articles (
            canonical_url TEXT PRIMARY KEY,
            published_at TEXT NOT NULL,
            feed_url TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO wsj_rss_state(
            singleton,
            updated_at
        ) VALUES (1, ?)
        """,
        (_now_iso(),),
    )
    connection.commit()


def initialize_wsj_google_news_schema(
    connection: sqlite3.Connection,
) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS wsj_google_news_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            status TEXT NOT NULL DEFAULT 'pending',
            polls INTEGER NOT NULL DEFAULT 0,
            items_seen INTEGER NOT NULL DEFAULT 0,
            decodes_attempted INTEGER NOT NULL DEFAULT 0,
            urls_accepted INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wsj_google_news_articles (
            canonical_url TEXT PRIMARY KEY,
            published_at TEXT NOT NULL,
            google_news_url TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO wsj_google_news_state(
            singleton,
            updated_at
        ) VALUES (1, ?)
        """,
        (_now_iso(),),
    )
    connection.commit()


def wsj_google_news_should_continue(
    connection: sqlite3.Connection,
    *,
    from_year: int,
    to_year: int,
    minimum_catalog: int = WSJ_GOOGLE_NEWS_MINIMUM_CATALOG,
) -> bool:
    initialize_wsj_google_news_schema(connection)
    if (
        _wsj_google_news_target_year(
            connection,
            from_year=from_year,
            to_year=to_year,
            minimum_catalog=minimum_catalog,
        )
        is None
    ):
        with connection:
            connection.execute(
                """
                UPDATE wsj_google_news_state
                SET status='complete-target-met',
                    last_error=NULL,
                    updated_at=?
                WHERE singleton=1
                """,
                (_now_iso(),),
            )
        return False
    # A previous release may have persisted complete-target-met after filling
    # only one historical year. Reopen until every supported gap year has the
    # parser-QA reserve.
    return True


def process_wsj_google_news_feed(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    http_client: httpx.Client,
    from_year: int,
    to_year: int,
    maximum_decodes: int = WSJ_GOOGLE_NEWS_MAXIMUM_DECODES,
    minimum_catalog: int = WSJ_GOOGLE_NEWS_MINIMUM_CATALOG,
) -> dict[str, object]:
    if spec.publisher != "wsj":
        raise ValueError("Google News discovery is only supported for WSJ")
    if maximum_decodes < 1:
        raise ValueError("maximum_decodes must be positive")
    initialize_wsj_google_news_schema(connection)
    polls = int(
        connection.execute(
            "SELECT polls FROM wsj_google_news_state WHERE singleton=1"
        ).fetchone()[0]
    )
    target_year = _wsj_google_news_target_year(
        connection,
        from_year=from_year,
        to_year=to_year,
        minimum_catalog=minimum_catalog,
    )
    if target_year is None:
        return {
            "status": "complete-target-met",
            "targetYear": None,
            "itemsSeen": 0,
            "decodesAttempted": 0,
            "accepted": 0,
            "catalogCount": None,
            "errors": [],
        }
    month = polls % 12 + 1
    window_start = f"{target_year:04d}-{month:02d}-01"
    if month == 12:
        window_end = f"{target_year + 1:04d}-01-01"
    else:
        window_end = f"{target_year:04d}-{month + 1:02d}-01"
    query = (
        "site:wsj.com/articles "
        f"after:{window_start} before:{window_end}"
    )
    response = http_client.get(
        GOOGLE_NEWS_RSS_ENDPOINT,
        params={
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        },
    )
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    items = root.findall("./channel/item")
    rows: list[tuple[str, str, str, str]] = []
    errors: list[str] = []
    decodes_attempted = 0
    for item in items:
        if decodes_attempted >= maximum_decodes:
            break
        published_at = _parse_rss_datetime(item.findtext("pubDate"))
        if (
            published_at is None
            or published_at.year != target_year
            or not from_year <= published_at.year <= to_year
        ):
            continue
        google_news_url = (item.findtext("link") or "").strip()
        if not google_news_url:
            continue
        decodes_attempted += 1
        try:
            original_url = _decode_google_news_url(
                http_client,
                google_news_url,
            )
            canonical_url = normalize_article_url(spec, original_url)
            if canonical_url is None:
                raise ValueError("decoded URL is not a WSJ article")
            rows.append(
                (
                    canonical_url,
                    published_at.isoformat(),
                    google_news_url,
                    _now_iso(),
                )
            )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    with connection:
        before = connection.total_changes
        connection.executemany(
            """
            INSERT INTO wsj_google_news_articles(
                canonical_url,
                published_at,
                google_news_url,
                updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                published_at=MIN(
                    wsj_google_news_articles.published_at,
                    excluded.published_at
                ),
                google_news_url=excluded.google_news_url,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        accepted = connection.total_changes - before
        catalog_count = wsj_catalog_count_for_year(
            connection,
            target_year,
        )
        status = (
            "complete-target-met"
            if _wsj_google_news_target_year(
                connection,
                from_year=from_year,
                to_year=to_year,
                minimum_catalog=minimum_catalog,
            )
            is None
            else "partial"
        )
        connection.execute(
            """
            UPDATE wsj_google_news_state
            SET status=?,
                polls=polls+1,
                items_seen=items_seen+?,
                decodes_attempted=decodes_attempted+?,
                urls_accepted=urls_accepted+?,
                last_error=?,
                updated_at=?
            WHERE singleton=1
            """,
            (
                status,
                len(items),
                decodes_attempted,
                accepted,
                "; ".join(errors[-5:]) if errors else None,
                _now_iso(),
            ),
        )
    return {
        "status": status,
        "targetYear": target_year,
        "itemsSeen": len(items),
        "decodesAttempted": decodes_attempted,
        "accepted": accepted,
        "catalogCount": catalog_count,
        "errors": errors,
    }


def _wsj_google_news_target_year(
    connection: sqlite3.Connection,
    *,
    from_year: int,
    to_year: int,
    minimum_catalog: int,
) -> int | None:
    years = [
        year
        for year in WSJ_GOOGLE_NEWS_YEARS
        if from_year <= year <= to_year
        and wsj_catalog_count_for_year(connection, year) < minimum_catalog
    ]
    if not years:
        return None
    return min(
        years,
        key=lambda year: (wsj_catalog_count_for_year(connection, year), year),
    )


def _decode_google_news_url(
    http_client: httpx.Client,
    google_news_url: str,
) -> str:
    parsed = httpx.URL(google_news_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.host != "news.google.com"
        or len(path_parts) < 2
        or path_parts[-2] not in {"articles", "read"}
    ):
        raise ValueError("invalid Google News article URL")
    article_id = path_parts[-1]
    parameter_response = http_client.get(
        f"https://news.google.com/rss/articles/{article_id}"
    )
    parameter_response.raise_for_status()
    signature_match = re.search(
        r'data-n-a-sg="([^"]+)"',
        parameter_response.text,
    )
    timestamp_match = re.search(
        r'data-n-a-ts="(\d+)"',
        parameter_response.text,
    )
    if signature_match is None or timestamp_match is None:
        raise ValueError("Google News decoding parameters are missing")
    descriptor = [
        "garturlreq",
        [
            [
                "X",
                "X",
                ["X", "X"],
                None,
                None,
                1,
                1,
                "US:en",
                None,
                1,
                None,
                None,
                None,
                None,
                None,
                0,
                1,
            ],
            "X",
            "X",
            1,
            [1, 1, 1],
            1,
            1,
            None,
            0,
            0,
            None,
            0,
        ],
        article_id,
        int(timestamp_match.group(1)),
        signature_match.group(1),
    ]
    request_payload = [
        [
            [
                "Fbv4je",
                json.dumps(
                    descriptor,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ]
        ]
    ]
    decode_response = http_client.post(
        GOOGLE_NEWS_DECODE_ENDPOINT,
        data={
            "f.req": json.dumps(
                request_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        },
        headers={
            "Origin": "https://news.google.com",
            "Referer": "https://news.google.com/",
        },
    )
    decode_response.raise_for_status()
    for chunk in decode_response.text.split("\n\n"):
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, list):
            continue
        for row in payload:
            if (
                not isinstance(row, list)
                or len(row) < 3
                or row[0] not in {"wrb.fr", "w779db"}
                or row[1] != "Fbv4je"
            ):
                continue
            inner = json.loads(row[2])
            if (
                isinstance(inner, list)
                and len(inner) >= 2
                and str(inner[1]).startswith(("http://", "https://"))
            ):
                return str(inner[1])
    raise ValueError("Google News decoded URL is missing")


def process_wsj_rss_feeds(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    http_client: httpx.Client,
    from_year: int,
    to_year: int,
    feed_urls: Iterable[str] = WSJ_RSS_ENDPOINTS,
) -> dict[str, object]:
    if spec.publisher != "wsj":
        raise ValueError("RSS discovery is only supported for WSJ")
    initialize_wsj_rss_schema(connection)
    rows: list[tuple[str, str, str, str]] = []
    feeds_checked = 0
    items_seen = 0
    errors: list[str] = []
    for feed_url in feed_urls:
        try:
            response = http_client.get(feed_url)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            items = root.findall("./channel/item")
            feeds_checked += 1
            items_seen += len(items)
            for item in items:
                original_url = (
                    (item.findtext("link") or "").strip()
                    or (item.findtext("guid") or "").strip()
                )
                canonical_url = normalize_article_url(spec, original_url)
                if canonical_url is None:
                    continue
                published_at = _parse_rss_datetime(
                    item.findtext("pubDate")
                )
                if published_at is None:
                    continue
                if not from_year <= published_at.year <= to_year:
                    continue
                rows.append(
                    (
                        canonical_url,
                        published_at.isoformat(),
                        feed_url,
                        _now_iso(),
                    )
                )
        except Exception as exc:
            errors.append(
                f"{feed_url}: {type(exc).__name__}: {exc}"
            )
    with connection:
        before = connection.total_changes
        connection.executemany(
            """
            INSERT OR IGNORE INTO wsj_rss_articles(
                canonical_url,
                published_at,
                feed_url,
                updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            rows,
        )
        accepted = connection.total_changes - before
        connection.execute(
            """
            UPDATE wsj_rss_state
            SET polls=polls+1,
                feeds_checked=feeds_checked+?,
                items_seen=items_seen+?,
                urls_accepted=urls_accepted+?,
                last_error=?,
                updated_at=?
            WHERE singleton=1
            """,
            (
                feeds_checked,
                items_seen,
                accepted,
                "; ".join(errors) if errors else None,
                _now_iso(),
            ),
        )
    return {
        "feedsChecked": feeds_checked,
        "itemsSeen": items_seen,
        "accepted": accepted,
        "errors": errors,
    }


def wsj_bluesky_should_continue(
    connection: sqlite3.Connection,
    *,
    from_year: int,
    to_year: int,
) -> bool:
    initialize_wsj_bluesky_schema(connection)
    first_year = max(from_year, WSJ_BLUESKY_START_YEAR)
    last_year = min(to_year, datetime.now(timezone.utc).year)
    if first_year > last_year:
        return False
    if all(
        wsj_catalog_count_for_year(connection, year)
        >= WSJ_CATALOG_TARGET_PER_YEAR
        for year in range(first_year, last_year + 1)
    ):
        with connection:
            connection.execute(
                """
                UPDATE wsj_bluesky_state
                SET status='complete-target-met',
                    last_error=NULL,
                    updated_at=?
                WHERE singleton=1
                """,
                (_now_iso(),),
            )
        return False
    status = connection.execute(
        "SELECT status FROM wsj_bluesky_state WHERE singleton=1"
    ).fetchone()[0]
    return not str(status).startswith("complete")


def process_wsj_bluesky_page(
    connection: sqlite3.Connection,
    *,
    spec: ArchiveSourceSpec,
    http_client: httpx.Client,
    from_year: int,
    to_year: int,
) -> dict[str, object]:
    if spec.publisher != "wsj":
        raise ValueError("Bluesky discovery is only supported for WSJ")
    initialize_wsj_bluesky_schema(connection)
    cursor = connection.execute(
        "SELECT cursor FROM wsj_bluesky_state WHERE singleton=1"
    ).fetchone()[0]
    with connection:
        connection.execute(
            """
            UPDATE wsj_bluesky_state
            SET status='processing',
                last_error=NULL,
                updated_at=?
            WHERE singleton=1
            """,
            (_now_iso(),),
        )
    try:
        parameters = {
            "actor": "wsj.com",
            "limit": "100",
            "filter": "posts_with_links",
        }
        if cursor:
            parameters["cursor"] = str(cursor)
        response = http_client.get(
            WSJ_BLUESKY_ENDPOINT,
            params=parameters,
        )
        response.raise_for_status()
        payload = response.json()
        feed = payload.get("feed")
        if not isinstance(feed, list):
            raise ValueError("WSJ Bluesky response has no feed list")
        next_cursor = payload.get("cursor")
        rows: list[tuple[str, str, str, str]] = []
        post_dates: list[datetime] = []
        for item in feed:
            if not isinstance(item, dict):
                continue
            post = item.get("post")
            if not isinstance(post, dict):
                continue
            record = post.get("record")
            if not isinstance(record, dict):
                continue
            created_at = _parse_iso_datetime(record.get("createdAt"))
            if created_at is None:
                continue
            post_dates.append(created_at)
            embed = post.get("embed")
            if not isinstance(embed, dict):
                continue
            external = embed.get("external")
            if not isinstance(external, dict):
                continue
            original_url = external.get("uri")
            if not isinstance(original_url, str):
                continue
            canonical_url = normalize_article_url(spec, original_url)
            if canonical_url is None:
                continue
            if not from_year <= created_at.year <= to_year:
                continue
            post_uri = str(post.get("uri") or "")
            rows.append(
                (
                    canonical_url,
                    created_at.isoformat(),
                    post_uri,
                    _now_iso(),
                )
            )
        with connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO wsj_bluesky_articles(
                    canonical_url,
                    published_at,
                    post_uri,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET
                    published_at=MIN(
                        wsj_bluesky_articles.published_at,
                        excluded.published_at
                    ),
                    post_uri=excluded.post_uri,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            accepted = connection.total_changes - before
            oldest = min(post_dates).isoformat() if post_dates else None
            first_year = max(from_year, WSJ_BLUESKY_START_YEAR)
            exhausted = (
                not feed
                or not next_cursor
                or (
                    bool(post_dates)
                    and min(post_dates).year < first_year
                )
            )
            status = "complete-history" if exhausted else "running"
            connection.execute(
                """
                UPDATE wsj_bluesky_state
                SET cursor=?,
                    status=?,
                    pages=pages+1,
                    posts_seen=posts_seen+?,
                    urls_accepted=urls_accepted+?,
                    oldest_at=COALESCE(?, oldest_at),
                    last_error=NULL,
                    updated_at=?
                WHERE singleton=1
                """,
                (
                    str(next_cursor) if next_cursor else None,
                    status,
                    len(feed),
                    accepted,
                    oldest,
                    _now_iso(),
                ),
            )
        target_met = not wsj_bluesky_should_continue(
            connection,
            from_year=from_year,
            to_year=to_year,
        )
        return {
            "status": (
                "complete-target-met"
                if target_met and not exhausted
                else status
            ),
            "seen": len(feed),
            "accepted": accepted,
            "oldestAt": oldest,
            "hasMore": not exhausted and not target_met,
        }
    except Exception as exc:
        with connection:
            connection.execute(
                """
                UPDATE wsj_bluesky_state
                SET status='error',
                    last_error=?,
                    updated_at=?
                WHERE singleton=1
                """,
                (f"{type(exc).__name__}: {exc}", _now_iso()),
            )
        raise


def wsj_catalog_count_for_year(
    connection: sqlite3.Connection,
    year: int,
) -> int:
    selects = [
        """
        SELECT canonical_url
        FROM candidates
        WHERE substr(published_at, 1, 4)=?
        """
    ]
    parameters: list[object] = [str(year)]
    for table in (
        "wsj_bluesky_articles",
        "wsj_rss_articles",
        "wsj_google_news_articles",
    ):
        if not _table_exists(connection, table):
            continue
        selects.append(
            f"""
            SELECT canonical_url
            FROM {table}
            WHERE substr(published_at, 1, 4)=?
            """
        )
        parameters.append(str(year))
    return int(
        connection.execute(
            f"""
            SELECT COUNT(DISTINCT canonical_url)
            FROM (
                {" UNION ".join(selects)}
            )
            """,
            parameters,
        ).fetchone()[0]
    )


def _wsj_external_articles(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for table in (
        "wsj_bluesky_articles",
        "wsj_rss_articles",
        "wsj_google_news_articles",
    ):
        if not _table_exists(connection, table):
            continue
        for canonical_url, published_at in connection.execute(
            f"""
            SELECT canonical_url, published_at
            FROM {table}
            ORDER BY canonical_url
            """
        ):
            previous = result.get(str(canonical_url))
            if previous is None or str(published_at) < previous:
                result[str(canonical_url)] = str(published_at)
    return result


def next_discovery_query(
    connection: sqlite3.Connection,
) -> tuple[str, str | None] | None:
    row = connection.execute(
        """
        SELECT pattern, resume_key
        FROM discovery_queries
        WHERE status != 'complete'
        ORDER BY
            CASE
                WHEN (
                    SELECT value
                    FROM discovery_metadata
                    WHERE key='collapse'
                )='urlkey'
                THEN pages
                ELSE 0
            END,
            rowid
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
        published_at = (
            infer_published_at(canonical_url)
            or _timestamp_datetime(capture.timestamp).isoformat()
        )
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
    capture_minimum_per_year: int = (
        PARSER_VALIDATION_CATALOG_MINIMUM_PER_YEAR
    ),
) -> dict[str, int | bool | str]:
    if capture_minimum_per_year < 1:
        raise ValueError("capture_minimum_per_year must be positive")
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
    external_articles = _wsj_external_articles(connection)
    written_urls: set[str] = set()
    with opener(temporary, "wt", encoding="utf-8") as handle:
        current_url: str | None = None
        current_published_at: str | None = None
        candidates: list[dict] = []
        for row in rows:
            canonical_url = row[0]
            if current_url is not None and canonical_url != current_url:
                if current_url in external_articles:
                    current_published_at = external_articles[current_url]
                    candidates = _merge_capture_candidates(
                        candidates,
                        _approximate_wayback_candidates(
                            current_url,
                            published_at=current_published_at,
                        ),
                    )
                _write_manifest_row(
                    handle,
                    spec=spec,
                    canonical_url=current_url,
                    published_at=current_published_at,
                    candidates=candidates,
                )
                article_count += 1
                candidate_count += len(candidates)
                written_urls.add(current_url)
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
            if current_url in external_articles:
                current_published_at = external_articles[current_url]
                candidates = _merge_capture_candidates(
                    candidates,
                    _approximate_wayback_candidates(
                        current_url,
                        published_at=current_published_at,
                    ),
                )
            _write_manifest_row(
                handle,
                spec=spec,
                canonical_url=current_url,
                published_at=current_published_at,
                candidates=candidates,
            )
            article_count += 1
            candidate_count += len(candidates)
            written_urls.add(current_url)
        for canonical_url, published_at in sorted(external_articles.items()):
            if canonical_url in written_urls:
                continue
            candidates = _approximate_wayback_candidates(
                canonical_url,
                published_at=published_at,
            )
            _write_manifest_row(
                handle,
                spec=spec,
                canonical_url=canonical_url,
                published_at=published_at,
                candidates=candidates,
            )
            article_count += 1
            candidate_count += len(candidates)
    temporary.replace(destination)
    incomplete = connection.execute(
        "SELECT COUNT(*) FROM discovery_queries WHERE status != 'complete'"
    ).fetchone()[0]
    if _table_exists(connection, "wsj_bluesky_state"):
        bluesky_status = connection.execute(
            "SELECT status FROM wsj_bluesky_state WHERE singleton=1"
        ).fetchone()[0]
        if not str(bluesky_status).startswith("complete"):
            incomplete += 1
    if _table_exists(connection, "wsj_google_news_state"):
        google_news_status = connection.execute(
            "SELECT status FROM wsj_google_news_state WHERE singleton=1"
        ).fetchone()[0]
        if not str(google_news_status).startswith("complete"):
            incomplete += 1
    year_counts = {
        str(year): wsj_catalog_count_for_year(connection, year)
        for year in range(from_year, to_year + 1)
    }
    return {
        "publisher": spec.publisher,
        "fromYear": from_year,
        "toYear": to_year,
        "complete": incomplete == 0,
        "captureReady": all(
            count >= capture_minimum_per_year
            for count in year_counts.values()
        ),
        "captureMinimumPerYear": capture_minimum_per_year,
        "yearCounts": year_counts,
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
    article_urls = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT canonical_url FROM candidates"
        )
    }
    article_urls.update(_wsj_external_articles(connection))
    result = {
        "queriesByStatus": query_counts,
        "pages": int(totals[0]),
        "rowsSeen": int(totals[1]),
        "rowsAccepted": int(totals[2]),
        "articles": len(article_urls),
        "shouldContinue": sum(
            count
            for status, count in query_counts.items()
            if status != "complete"
        )
        > 0,
    }
    if _table_exists(connection, "wsj_bluesky_state"):
        row = connection.execute(
            """
            SELECT
                status,
                pages,
                posts_seen,
                urls_accepted,
                oldest_at,
                last_error
            FROM wsj_bluesky_state
            WHERE singleton=1
            """
        ).fetchone()
        result["wsjBluesky"] = {
            "status": str(row[0]),
            "pages": int(row[1]),
            "postsSeen": int(row[2]),
            "urlsAccepted": int(row[3]),
            "oldestAt": row[4],
            "lastError": row[5],
        }
        result["shouldContinue"] = bool(result["shouldContinue"]) or not str(
            row[0]
        ).startswith("complete")
    if _table_exists(connection, "wsj_rss_state"):
        row = connection.execute(
            """
            SELECT
                polls,
                feeds_checked,
                items_seen,
                urls_accepted,
                last_error
            FROM wsj_rss_state
            WHERE singleton=1
            """
        ).fetchone()
        result["wsjRss"] = {
            "polls": int(row[0]),
            "feedsChecked": int(row[1]),
            "itemsSeen": int(row[2]),
            "urlsAccepted": int(row[3]),
            "lastError": row[4],
        }
    if _table_exists(connection, "wsj_google_news_state"):
        row = connection.execute(
            """
            SELECT
                status,
                polls,
                items_seen,
                decodes_attempted,
                urls_accepted,
                last_error
            FROM wsj_google_news_state
            WHERE singleton=1
            """
        ).fetchone()
        result["wsjGoogleNews"] = {
            "status": str(row[0]),
            "polls": int(row[1]),
            "itemsSeen": int(row[2]),
            "decodesAttempted": int(row[3]),
            "urlsAccepted": int(row[4]),
            "lastError": row[5],
        }
        result["shouldContinue"] = bool(result["shouldContinue"]) or not str(
            row[0]
        ).startswith("complete")
    return result


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
    candidates = with_current_year_live_fallback(
        candidates,
        canonical_url=canonical_url,
        published_at=published_at,
    )
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


def with_current_year_live_fallback(
    candidates: list[dict[str, object]],
    *,
    canonical_url: str,
    published_at: str | None,
) -> list[dict[str, object]]:
    if not published_at:
        return candidates
    try:
        published = datetime.fromisoformat(published_at)
    except (TypeError, ValueError, OverflowError):
        return candidates
    if published.year != datetime.now(timezone.utc).year:
        return candidates
    if any(
        candidate.get("provider") == "live-origin"
        for candidate in candidates
    ):
        return candidates
    return [
        *candidates,
        {
            "provider": "live-origin",
            "snapshotUrl": canonical_url,
        },
    ]


def _approximate_wayback_candidates(
    canonical_url: str,
    *,
    published_at: str,
) -> list[dict[str, object]]:
    published = _parse_iso_datetime(published_at)
    if published is None:
        return [
            {
                "provider": "wayback",
                "snapshotUrl": (
                    "https://web.archive.org/web/2id_/" + canonical_url
                ),
            }
        ]
    result: list[dict[str, object]] = []
    for delta in (timedelta(days=1), timedelta(days=7), timedelta(days=30)):
        timestamp = (published + delta).strftime("%Y%m%d%H%M%S")
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


def _merge_capture_candidates(
    first: list[dict],
    second: list[dict[str, object]],
) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for candidate in [*first, *second]:
        snapshot_url = str(candidate.get("snapshotUrl") or "")
        if not snapshot_url or snapshot_url in seen:
            continue
        seen.add(snapshot_url)
        result.append(candidate)
    return result


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_rss_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_datetime(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def _spec_fingerprint(
    spec: ArchiveSourceSpec,
    *,
    from_year: int,
    to_year: int,
    collapse: str = "digest",
) -> str:
    payload = {
        "publisher": spec.publisher,
        "fromYear": from_year,
        "toYear": to_year,
        "patterns": spec.expanded_wayback_patterns(
            from_year=from_year,
            to_year=to_year,
        ),
    }
    # Preserve the original digest-mode fingerprint so deployed checkpoints
    # remain resumable. Alternate collapse modes get isolated fingerprints and
    # B2 shards.
    if collapse != "digest":
        payload["collapse"] = collapse
    value = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
