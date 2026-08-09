from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import hashlib
import re
import sqlite3
from typing import Iterable
from urllib.parse import quote, urlsplit

from .archive_sources import (
    archive_source_spec,
    normalize_article_url,
    wsj_article_publication_datetime,
)
INFINI_DATASET = "ruggsea/infini-news-corpus"
HUGGING_FACE_TREE_ENDPOINT = (
    "https://huggingface.co/api/datasets/"
    f"{INFINI_DATASET}/tree/main"
)
HUGGING_FACE_RESOLVE_ENDPOINT = (
    "https://huggingface.co/datasets/"
    f"{INFINI_DATASET}/resolve/main"
)
WSJ_INFINI_DIRECT_FIRST_YEAR = 2016
WSJ_INFINI_DIRECT_LAST_YEAR = 2018
WSJ_INFINI_DIRECT_TARGET_PER_YEAR = 1_600
DEFAULT_MAXIMUM_FILES_PER_RUN = 50
DEFAULT_SCAN_WORKERS = 8
MINIMUM_TEXT_CHARACTERS = 300
_WSJ_HOSTS = {"wsj.com", "www.wsj.com", "online.wsj.com"}
_SIGNIFICANT_TOKEN_RE = re.compile(r"[a-z0-9]+")


def initialize_wsj_infini_direct_schema(
    connection: sqlite3.Connection,
    *,
    from_year: int,
    to_year: int,
) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS wsj_infini_direct_years (
            source_year INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            file_count INTEGER,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wsj_infini_direct_files (
            source_year INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            byte_count INTEGER NOT NULL,
            scan_priority TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source_year, file_path)
        );

        CREATE INDEX IF NOT EXISTS idx_wsj_infini_direct_file_scan
            ON wsj_infini_direct_files(
                source_year,
                status,
                scan_priority
            );

        CREATE TABLE IF NOT EXISTS wsj_infini_direct_articles (
            canonical_url TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            published_at TEXT NOT NULL,
            expected_headline TEXT NOT NULL,
            source_year INTEGER NOT NULL,
            text_length INTEGER NOT NULL,
            warc_filename TEXT NOT NULL,
            parquet_path TEXT NOT NULL,
            parquet_row_index INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_wsj_infini_direct_article_year
            ON wsj_infini_direct_articles(source_year, canonical_url);
        """
    )
    first_year = max(from_year, WSJ_INFINI_DIRECT_FIRST_YEAR)
    last_year = min(to_year, WSJ_INFINI_DIRECT_LAST_YEAR)
    if first_year <= last_year:
        now = _now_iso()
        connection.executemany(
            """
            INSERT OR IGNORE INTO wsj_infini_direct_years(
                source_year,
                updated_at
            ) VALUES (?, ?)
            """,
            ((year, now) for year in range(first_year, last_year + 1)),
        )
    connection.commit()


def process_wsj_infini_direct_catalog(
    connection: sqlite3.Connection,
    *,
    from_year: int,
    to_year: int,
    http_client,
    maximum_files: int = DEFAULT_MAXIMUM_FILES_PER_RUN,
    workers: int = DEFAULT_SCAN_WORKERS,
    target_articles: int = WSJ_INFINI_DIRECT_TARGET_PER_YEAR,
) -> dict[str, object]:
    if maximum_files < 1:
        raise ValueError("maximum_files must be positive")
    if workers < 1:
        raise ValueError("workers must be positive")
    if target_articles < 1:
        raise ValueError("target_articles must be positive")
    initialize_wsj_infini_direct_schema(
        connection,
        from_year=from_year,
        to_year=to_year,
    )
    row = connection.execute(
        """
        SELECT source_year
        FROM wsj_infini_direct_years
        WHERE status != 'complete'
        ORDER BY source_year
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return {
            "year": None,
            "listedFiles": 0,
            "attemptedFiles": 0,
            "acceptedRows": 0,
            "newArticles": 0,
            "errors": [],
            "shouldContinue": False,
        }
    year = int(row[0])
    listed_files = 0
    if not connection.execute(
        """
        SELECT 1
        FROM wsj_infini_direct_files
        WHERE source_year=?
        LIMIT 1
        """,
        (year,),
    ).fetchone():
        try:
            files = _list_year_parquet_files(http_client, year=year)
            if not files:
                raise ValueError("Infini-News year has no Parquet files")
            _store_file_catalog(connection, year=year, files=files)
            listed_files = len(files)
            with connection:
                connection.execute(
                    """
                    UPDATE wsj_infini_direct_years
                    SET status='scanning',
                        file_count=?,
                        last_error=NULL,
                        updated_at=?
                    WHERE source_year=?
                    """,
                    (len(files), _now_iso(), year),
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with connection:
                connection.execute(
                    """
                    UPDATE wsj_infini_direct_years
                    SET last_error=?, updated_at=?
                    WHERE source_year=?
                    """,
                    (error, _now_iso(), year),
                )
            return {
                "year": year,
                "listedFiles": 0,
                "attemptedFiles": 0,
                "acceptedRows": 0,
                "newArticles": 0,
                "errors": [error],
                "shouldContinue": True,
            }

    before = _article_count(connection, year)
    scan = _scan_pending_files(
        connection,
        year=year,
        maximum_files=maximum_files,
        workers=workers,
        target_articles=target_articles,
    )
    after = _article_count(connection, year)
    retryable = _retryable_file_count(connection, year)
    if after >= target_articles or retryable == 0:
        with connection:
            connection.execute(
                """
                UPDATE wsj_infini_direct_years
                SET status='complete', last_error=NULL, updated_at=?
                WHERE source_year=?
                """,
                (_now_iso(), year),
            )
    return {
        "year": year,
        "listedFiles": listed_files,
        "attemptedFiles": int(scan["attempted"]),
        "acceptedRows": int(scan["accepted"]),
        "newArticles": after - before,
        "articles": after,
        "targetArticles": target_articles,
        "retryableFiles": retryable,
        "errors": list(scan["errors"]),
        "shouldContinue": wsj_infini_direct_should_continue(connection),
    }


def wsj_infini_direct_should_continue(
    connection: sqlite3.Connection,
) -> bool:
    if not _table_exists(connection, "wsj_infini_direct_years"):
        return False
    return connection.execute(
        """
        SELECT 1
        FROM wsj_infini_direct_years
        WHERE status != 'complete'
        LIMIT 1
        """
    ).fetchone() is not None


def wsj_infini_direct_summary(
    connection: sqlite3.Connection,
) -> dict[str, object] | None:
    if not _table_exists(connection, "wsj_infini_direct_years"):
        return None
    years: dict[str, object] = {}
    for year, status, file_count, last_error in connection.execute(
        """
        SELECT source_year, status, file_count, last_error
        FROM wsj_infini_direct_years
        ORDER BY source_year
        """
    ):
        file_status = {
            str(item_status): int(count)
            for item_status, count in connection.execute(
                """
                SELECT status, COUNT(*)
                FROM wsj_infini_direct_files
                WHERE source_year=?
                GROUP BY status
                """,
                (year,),
            )
        }
        years[str(year)] = {
            "status": str(status),
            "files": int(file_count or 0),
            "filesByStatus": file_status,
            "articles": _article_count(connection, int(year)),
            "targetArticles": WSJ_INFINI_DIRECT_TARGET_PER_YEAR,
            "lastError": last_error,
        }
    return {
        "years": years,
        "shouldContinue": wsj_infini_direct_should_continue(connection),
    }


def _store_file_catalog(
    connection: sqlite3.Connection,
    *,
    year: int,
    files: Iterable[tuple[str, int]],
) -> None:
    now = _now_iso()
    with connection:
        connection.executemany(
            """
            INSERT INTO wsj_infini_direct_files(
                source_year,
                file_path,
                byte_count,
                scan_priority,
                updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_year, file_path) DO UPDATE SET
                byte_count=excluded.byte_count,
                updated_at=excluded.updated_at
            """,
            (
                (
                    year,
                    path,
                    byte_count,
                    hashlib.sha256(
                        f"wsj-infini-direct-v1\0{year}\0{path}".encode()
                    ).hexdigest(),
                    now,
                )
                for path, byte_count in files
            ),
        )


def _list_year_parquet_files(
    http_client,
    *,
    year: int,
) -> list[tuple[str, int]]:
    files: dict[str, int] = {}
    for month in range(1, 13):
        tree_path = f"data/year={year}/month={month:02d}"
        url = HUGGING_FACE_TREE_ENDPOINT + "/" + quote(
            tree_path,
            safe="",
        )
        while url:
            response = http_client.get(
                url,
                params={
                    "recursive": "false",
                    "expand": "false",
                    "limit": 1000,
                }
                if "cursor=" not in url
                else None,
            )
            if response.status_code == 404:
                break
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Hugging Face tree response is invalid")
            for entry in payload:
                if (
                    isinstance(entry, dict)
                    and entry.get("type") == "file"
                    and str(entry.get("path") or "").endswith(".parquet")
                ):
                    path = str(entry["path"])
                    files[path] = int(entry.get("size") or 0)
            next_link = response.links.get("next")
            url = (
                str(next_link.get("url"))
                if isinstance(next_link, dict) and next_link.get("url")
                else ""
            )
    return sorted(files.items())


def _scan_pending_files(
    connection: sqlite3.Connection,
    *,
    year: int,
    maximum_files: int,
    workers: int,
    target_articles: int,
) -> dict[str, object]:
    rows = connection.execute(
        """
        SELECT file_path
        FROM wsj_infini_direct_files
        WHERE source_year=?
          AND (
            status='pending'
            OR (status='error' AND attempts < 3)
          )
        ORDER BY scan_priority
        LIMIT ?
        """,
        (year, maximum_files),
    ).fetchall()
    attempted = 0
    accepted = 0
    errors: list[str] = []
    batch_size = workers * 2
    for batch_start in range(0, len(rows), batch_size):
        if _article_count(connection, year) >= target_articles:
            break
        batch = [
            str(row[0])
            for row in rows[batch_start : batch_start + batch_size]
        ]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_scan_parquet_file, path, year=year): path
                for path in batch
            }
            for future in as_completed(futures):
                path = futures[future]
                attempted += 1
                try:
                    articles = future.result()
                    accepted += len(articles)
                    _store_scanned_articles(
                        connection,
                        year=year,
                        path=path,
                        articles=articles,
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    errors.append(f"{path}: {error}")
                    with connection:
                        connection.execute(
                            """
                            UPDATE wsj_infini_direct_files
                            SET status='error',
                                attempts=attempts+1,
                                last_error=?,
                                updated_at=?
                            WHERE source_year=? AND file_path=?
                            """,
                            (error, _now_iso(), year, path),
                        )
    return {
        "attempted": attempted,
        "accepted": accepted,
        "errors": errors[:20],
    }


def _scan_parquet_file(
    path: str,
    *,
    year: int,
) -> list[dict[str, object]]:
    import fsspec
    import pyarrow.parquet as pq

    columns = (
        "url",
        "url_hostname",
        "warc_filename",
        "publish_date",
        "title",
        "text_length",
        "language",
    )
    with fsspec.open(
        _resolve_url(path),
        "rb",
        block_size=5 * 1024 * 1024,
        cache_type="readahead",
    ).open() as handle:
        values = pq.read_table(handle, columns=list(columns)).to_pydict()
    accepted: list[dict[str, object]] = []
    spec = archive_source_spec("wsj")
    for row_index, raw_url in enumerate(values["url"]):
        source_url = str(raw_url or "").strip()
        hostname = str(
            values["url_hostname"][row_index] or ""
        ).casefold().rstrip(".")
        if hostname not in _WSJ_HOSTS:
            continue
        parsed_hostname = (
            urlsplit(source_url).hostname or ""
        ).casefold().rstrip(".")
        if parsed_hostname != hostname:
            continue
        canonical_url = normalize_article_url(spec, source_url)
        if canonical_url is None:
            continue
        published = _parse_publish_date(values["publish_date"][row_index])
        if published is None or published.year != year:
            continue
        url_published = wsj_article_publication_datetime(canonical_url)
        if url_published is not None and url_published.year != year:
            continue
        headline = " ".join(str(values["title"][row_index] or "").split())
        if len(_SIGNIFICANT_TOKEN_RE.findall(headline.casefold())) < 4:
            continue
        text_length = _optional_int(values["text_length"][row_index])
        if text_length is None or text_length < MINIMUM_TEXT_CHARACTERS:
            continue
        language = str(values["language"][row_index] or "").casefold()
        if language and not language.startswith("eng"):
            continue
        warc_filename = str(values["warc_filename"][row_index] or "").strip()
        if (
            not warc_filename.startswith("CC-NEWS-")
            or not warc_filename.endswith(".warc.gz")
        ):
            continue
        accepted.append(
            {
                "canonicalUrl": canonical_url,
                "sourceUrl": source_url,
                "publishedAt": published.isoformat(),
                "expectedHeadline": headline,
                "textLength": text_length,
                "warcFilename": warc_filename,
                "parquetRowIndex": row_index,
            }
        )
    return accepted


def _store_scanned_articles(
    connection: sqlite3.Connection,
    *,
    year: int,
    path: str,
    articles: Iterable[dict[str, object]],
) -> None:
    rows = list(articles)
    now = _now_iso()
    with connection:
        connection.executemany(
            """
            INSERT INTO wsj_infini_direct_articles(
                canonical_url,
                source_url,
                published_at,
                expected_headline,
                source_year,
                text_length,
                warc_filename,
                parquet_path,
                parquet_row_index,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                source_url=excluded.source_url,
                published_at=excluded.published_at,
                expected_headline=excluded.expected_headline,
                text_length=excluded.text_length,
                warc_filename=excluded.warc_filename,
                parquet_path=excluded.parquet_path,
                parquet_row_index=excluded.parquet_row_index,
                updated_at=excluded.updated_at
            """,
            (
                (
                    row["canonicalUrl"],
                    row["sourceUrl"],
                    row["publishedAt"],
                    row["expectedHeadline"],
                    year,
                    row["textLength"],
                    row["warcFilename"],
                    path,
                    row["parquetRowIndex"],
                    now,
                )
                for row in rows
            ),
        )
        connection.executemany(
            """
            INSERT INTO wsj_infini_articles(
                canonical_url,
                published_at,
                expected_headline,
                source_year,
                query_id,
                document_index,
                warc_source,
                updated_at
            ) VALUES (?, ?, ?, ?, 'direct-hostname', NULL, ?, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                published_at=MIN(
                    wsj_infini_articles.published_at,
                    excluded.published_at
                ),
                expected_headline=excluded.expected_headline,
                warc_source=excluded.warc_source,
                updated_at=excluded.updated_at
            """,
            (
                (
                    row["canonicalUrl"],
                    row["publishedAt"],
                    row["expectedHeadline"],
                    year,
                    row["warcFilename"],
                    now,
                )
                for row in rows
            ),
        )
        connection.execute(
            """
            UPDATE wsj_infini_direct_files
            SET status='complete',
                attempts=attempts+1,
                accepted_count=?,
                last_error=NULL,
                updated_at=?
            WHERE source_year=? AND file_path=?
            """,
            (len(rows), now, year, path),
        )


def _retryable_file_count(connection: sqlite3.Connection, year: int) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM wsj_infini_direct_files
            WHERE source_year=?
              AND (
                status='pending'
                OR (status='error' AND attempts < 3)
              )
            """,
            (year,),
        ).fetchone()[0]
    )


def _resolve_url(path: str) -> str:
    return HUGGING_FACE_RESOLVE_ENDPOINT + "/" + quote(path, safe="/=")


def _parse_publish_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _article_count(connection: sqlite3.Connection, year: int) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM wsj_infini_direct_articles
            WHERE source_year=?
            """,
            (year,),
        ).fetchone()[0]
    )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
