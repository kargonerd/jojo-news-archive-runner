from __future__ import annotations

from datetime import datetime, timezone
from collections import Counter
import gzip
import hashlib
import heapq
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .archive_sources import (
    archive_source_spec,
    article_url_publication_year,
    normalize_article_url,
)
from .news_models import ArticleStatus, ContentType, RawCapture
from .news_parser import parse_article
from .publisher_specs import publisher_spec


SCHEMA_VERSION = "jojo-parser-validation/1"
DEFAULT_SEED = "jojo-parser-validation-v1"
HOLDOUT_SEED = "jojo-parser-holdout-v1"
MINIMUM_COMPLETE_RATE = 0.95
MINIMUM_QA_PASS_RATE = 0.95
_PAYWALL_PHRASES = (
    "subscribe to read",
    "subscribe to continue",
    "sign in to continue",
    "already a subscriber",
    "unlock this article",
)


def initialize_parser_validation_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS parser_validation_config (
            sample_year INTEGER PRIMARY KEY,
            target_size INTEGER NOT NULL,
            seed TEXT NOT NULL,
            parser_version TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS parser_validation_samples (
            canonical_url TEXT PRIMARY KEY,
            sample_year INTEGER NOT NULL,
            sample_priority TEXT NOT NULL,
            selected_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_parser_validation_samples_year
            ON parser_validation_samples(sample_year, sample_priority);

        CREATE TABLE IF NOT EXISTS parser_validation_results (
            canonical_url TEXT PRIMARY KEY,
            publisher TEXT NOT NULL,
            sample_year INTEGER NOT NULL,
            parser_version TEXT,
            extraction_status TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'article',
            qa_pass INTEGER NOT NULL,
            body_characters INTEGER NOT NULL DEFAULT 0,
            block_count INTEGER NOT NULL DEFAULT 0,
            images_referenced INTEGER NOT NULL DEFAULT 0,
            images_selected INTEGER NOT NULL DEFAULT 0,
            duplicate_text_blocks INTEGER NOT NULL DEFAULT 0,
            headline_present INTEGER NOT NULL DEFAULT 0,
            published_at_present INTEGER NOT NULL DEFAULT 0,
            source_link_preserved INTEGER NOT NULL DEFAULT 0,
            warnings_json TEXT NOT NULL,
            issues_json TEXT NOT NULL,
            error TEXT,
            parsed_at TEXT NOT NULL,
            source_raw_sha256 TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_parser_validation_results_year
            ON parser_validation_results(sample_year, qa_pass);

        CREATE TABLE IF NOT EXISTS parser_validation_exclusions (
            canonical_url TEXT PRIMARY KEY,
            source_cohort TEXT NOT NULL,
            excluded_at TEXT NOT NULL
        );
        """
    )
    config_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(parser_validation_config)"
        ).fetchall()
    }
    if "parser_version" not in config_columns:
        connection.execute(
            """
            ALTER TABLE parser_validation_config
            ADD COLUMN parser_version TEXT NOT NULL DEFAULT ''
            """
        )
    result_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(parser_validation_results)"
        ).fetchall()
    }
    if "content_type" not in result_columns:
        connection.execute(
            """
            ALTER TABLE parser_validation_results
            ADD COLUMN content_type TEXT NOT NULL DEFAULT 'article'
            """
        )
    if "source_raw_sha256" not in result_columns:
        connection.execute(
            """
            ALTER TABLE parser_validation_results
            ADD COLUMN source_raw_sha256 TEXT
            """
        )
    captures_exist = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='captures'
        """
    ).fetchone()
    if captures_exist is not None:
        connection.execute(
            """
            DELETE FROM parser_validation_results
            WHERE source_raw_sha256 IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM captures AS current_capture
                WHERE current_capture.canonical_url =
                    parser_validation_results.canonical_url
                  AND current_capture.status='complete'
                  AND current_capture.raw_sha256 IS NOT NULL
              )
              AND NOT EXISTS (
                SELECT 1
                FROM captures AS capture
                WHERE capture.canonical_url =
                    parser_validation_results.canonical_url
                  AND capture.raw_sha256 =
                    parser_validation_results.source_raw_sha256
              )
            """
        )
    connection.commit()


def ensure_parser_validation_plan(
    connection: sqlite3.Connection,
    *,
    publisher: str,
    from_year: int,
    to_year: int,
    target_per_year: int,
    maximum_record_attempts: int,
    reserve_per_year: int | None = None,
    seed: str = DEFAULT_SEED,
) -> dict[str, object]:
    if from_year > to_year:
        raise ValueError("from_year must not exceed to_year")
    if target_per_year < 1:
        raise ValueError("target_per_year must be positive")
    if maximum_record_attempts < 1:
        raise ValueError("maximum_record_attempts must be positive")
    reserve = (
        reserve_per_year
        if reserve_per_year is not None
        else max(100, target_per_year // 2)
    )
    if reserve < 0:
        raise ValueError("reserve_per_year must not be negative")

    initialize_parser_validation_schema(connection)
    source_spec = archive_source_spec(publisher)
    invalid_urls = [
        str(row[0])
        for row in connection.execute(
            """
            SELECT canonical_url, sample_year
            FROM parser_validation_samples
            WHERE sample_year >= ? AND sample_year <= ?
            """,
            (from_year, to_year),
        )
        if (
            normalize_article_url(source_spec, str(row[0])) is None
            or (
                (
                    embedded_year := article_url_publication_year(
                        source_spec,
                        str(row[0]),
                    )
                )
                is not None
                and embedded_year != int(row[1])
            )
        )
    ]
    if invalid_urls:
        connection.executemany(
            """
            DELETE FROM parser_validation_results
            WHERE canonical_url=?
            """,
            ((url,) for url in invalid_urls),
        )
        connection.executemany(
            """
            DELETE FROM parser_validation_samples
            WHERE canonical_url=?
            """,
            ((url,) for url in invalid_urls),
        )
    now = _now_iso()
    current_parser_version = publisher_spec(publisher).parser_version
    previous_versions = {
        int(year): str(parser_version)
        for year, parser_version in connection.execute(
            """
            SELECT sample_year, parser_version
            FROM parser_validation_config
            WHERE sample_year >= ? AND sample_year <= ?
            """,
            (from_year, to_year),
        )
    }
    refreshed_years = {
        year
        for year in range(from_year, to_year + 1)
        if (
            publisher in {"nyt", "wsj"}
            and year in previous_versions
            and previous_versions[year] != current_parser_version
        )
    }
    if refreshed_years:
        placeholders = ",".join("?" for _ in refreshed_years)
        connection.execute(
            f"""
            DELETE FROM parser_validation_samples
            WHERE sample_year IN ({placeholders})
            """,
            sorted(refreshed_years),
        )
    connection.executemany(
        """
        INSERT INTO parser_validation_config(
            sample_year, target_size, seed, parser_version, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(sample_year) DO UPDATE SET
            target_size=excluded.target_size,
            seed=excluded.seed,
            parser_version=excluded.parser_version,
            updated_at=excluded.updated_at
        """,
        (
            (
                year,
                target_per_year,
                seed,
                current_parser_version,
                now,
            )
            for year in range(from_year, to_year + 1)
        ),
    )

    years: dict[str, dict[str, int]] = {}
    for year in range(from_year, to_year + 1):
        start = f"{year:04d}-01-01"
        end = f"{year + 1:04d}-01-01"
        available = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM captures
                WHERE published_at >= ? AND published_at < ?
                """,
                (start, end),
            ).fetchone()[0]
        )
        evaluated = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM parser_validation_results
                WHERE sample_year=? AND parser_version=?
                """,
                (year, current_parser_version),
            ).fetchone()[0]
        )
        actionable = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM parser_validation_samples AS sample
                JOIN captures AS capture
                  ON capture.canonical_url=sample.canonical_url
                LEFT JOIN parser_validation_results AS result
                  ON result.canonical_url=sample.canonical_url
                 AND result.parser_version=?
                WHERE sample.sample_year=?
                  AND result.canonical_url IS NULL
                  AND (
                    (
                      capture.status='complete'
                      AND capture.raw_path IS NOT NULL
                    )
                    OR
                    capture.status='pending'
                    OR (
                      capture.status='error'
                      AND capture.attempts < ?
                    )
                  )
                """,
                (
                    current_parser_version,
                    year,
                    maximum_record_attempts,
                ),
            ).fetchone()[0]
        )
        actionable_before_planning = actionable
        completed_actionable = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM parser_validation_samples AS sample
                JOIN captures AS capture
                  ON capture.canonical_url=sample.canonical_url
                LEFT JOIN parser_validation_results AS result
                  ON result.canonical_url=sample.canonical_url
                 AND result.parser_version=?
                WHERE sample.sample_year=?
                  AND result.canonical_url IS NULL
                  AND capture.status='complete'
                  AND capture.raw_path IS NOT NULL
                """,
                (current_parser_version, year),
            ).fetchone()[0]
        )
        completed_needed = max(
            0,
            target_per_year - evaluated - completed_actionable,
        )
        completed_selected = _select_additional_samples(
            connection,
            publisher=publisher,
            year=year,
            limit=completed_needed,
            seed=seed,
            completed_only=True,
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO parser_validation_samples(
                canonical_url, sample_year, sample_priority, selected_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                (canonical_url, year, priority, now)
                for priority, canonical_url in completed_selected
            ),
        )
        actionable += len(completed_selected)
        desired_actionable = max(0, target_per_year - evaluated) + reserve
        direct_selected: list[tuple[str, str]] = []
        if publisher == "ft":
            existing_direct = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM parser_validation_samples AS sample
                    JOIN captures AS capture
                      ON capture.canonical_url=sample.canonical_url
                    WHERE sample.sample_year=?
                      AND capture.candidates_json
                          LIKE '%"provider":"infini-news"%'
                    """,
                    (year,),
                ).fetchone()[0]
            )
            direct_selected = _select_additional_samples(
                connection,
                publisher=publisher,
                year=year,
                limit=max(0, desired_actionable - existing_direct),
                seed=seed,
                completed_only=False,
                direct_provider="infini-news",
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO parser_validation_samples(
                    canonical_url,
                    sample_year,
                    sample_priority,
                    selected_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    (canonical_url, year, priority, now)
                    for priority, canonical_url in direct_selected
                ),
            )
            actionable += len(direct_selected)
        add_count = max(0, desired_actionable - actionable)
        if publisher == "nyt" and add_count:
            direct_selected = _select_additional_samples(
                connection,
                publisher=publisher,
                year=year,
                limit=add_count,
                seed=seed,
                completed_only=False,
                direct_provider="other",
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO parser_validation_samples(
                    canonical_url,
                    sample_year,
                    sample_priority,
                    selected_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    (canonical_url, year, priority, now)
                    for priority, canonical_url in direct_selected
                ),
            )
            actionable += len(direct_selected)
            add_count -= len(direct_selected)
        selected = _select_additional_samples(
            connection,
            publisher=publisher,
            year=year,
            limit=add_count,
            seed=seed,
            completed_only=False,
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO parser_validation_samples(
                canonical_url, sample_year, sample_priority, selected_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                (canonical_url, year, priority, now)
                for priority, canonical_url in selected
            ),
        )
        years[str(year)] = {
            "available": available,
            "evaluated": evaluated,
            "actionableBeforePlanning": actionable_before_planning,
            "refreshedForParserVersion": int(year in refreshed_years),
            "addedCompletedToPlan": len(completed_selected),
            "addedDirectToPlan": len(direct_selected),
            "addedToPlan": (
                len(completed_selected)
                + len(direct_selected)
                + len(selected)
            ),
        }
    connection.commit()
    return {
        "formatVersion": SCHEMA_VERSION,
        "publisher": publisher,
        "parserVersion": current_parser_version,
        "targetPerYear": target_per_year,
        "reservePerYear": reserve,
        "years": years,
    }


def pending_parser_validation_urls(
    connection: sqlite3.Connection,
    *,
    maximum: int | None,
    maximum_record_attempts: int,
) -> list[str]:
    initialize_parser_validation_schema(connection)
    query = """
        WITH active_years AS (
            SELECT
                config.sample_year,
                config.target_size,
                config.parser_version
            FROM parser_validation_config AS config
            LEFT JOIN parser_validation_results AS result
              ON result.sample_year=config.sample_year
             AND result.parser_version=config.parser_version
            GROUP BY
                config.sample_year,
                config.target_size,
                config.parser_version
            HAVING COUNT(result.canonical_url) < config.target_size
        ),
        ranked AS (
            SELECT
                sample.canonical_url,
                sample.sample_year,
                ROW_NUMBER() OVER (
                    PARTITION BY sample.sample_year
                    ORDER BY
                        CASE capture.status
                            WHEN 'pending' THEN 0
                            ELSE 1
                        END,
                        CASE
                            WHEN capture.candidates_json
                                 LIKE '%"provider":"infini-news"%'
                            THEN 0
                            WHEN capture.candidates_json
                                 LIKE '%"provider":"other"%'
                            THEN 1
                            ELSE 2
                        END,
                        sample.sample_priority
                ) AS sample_rank
            FROM parser_validation_samples AS sample
            JOIN active_years
              ON active_years.sample_year=sample.sample_year
            JOIN captures AS capture
              ON capture.canonical_url=sample.canonical_url
            LEFT JOIN parser_validation_results AS result
              ON result.canonical_url=sample.canonical_url
             AND result.parser_version=active_years.parser_version
            WHERE result.canonical_url IS NULL
              AND (
                capture.status='pending'
                OR (
                    capture.status='error'
                    AND capture.attempts < ?
                )
              )
        )
        SELECT canonical_url
        FROM ranked
        ORDER BY sample_rank, sample_year
    """
    parameters: list[object] = [maximum_record_attempts]
    if maximum is not None:
        query += " LIMIT ?"
        parameters.append(maximum)
    return [
        str(row[0])
        for row in connection.execute(query, parameters).fetchall()
    ]


def pending_completed_parser_validation_files(
    connection: sqlite3.Connection,
    *,
    maximum: int | None,
) -> list[tuple[str, str]]:
    initialize_parser_validation_schema(connection)
    query = """
        WITH active_years AS (
            SELECT
                config.sample_year,
                config.target_size,
                config.parser_version
            FROM parser_validation_config AS config
            LEFT JOIN parser_validation_results AS result
              ON result.sample_year=config.sample_year
             AND result.parser_version=config.parser_version
            GROUP BY
                config.sample_year,
                config.target_size,
                config.parser_version
            HAVING COUNT(result.canonical_url) < config.target_size
        ),
        ranked AS (
            SELECT
                sample.canonical_url,
                capture.raw_path,
                sample.sample_year,
                ROW_NUMBER() OVER (
                    PARTITION BY sample.sample_year
                    ORDER BY sample.sample_priority
                ) AS sample_rank
            FROM parser_validation_samples AS sample
            JOIN active_years
              ON active_years.sample_year=sample.sample_year
            JOIN captures AS capture
              ON capture.canonical_url=sample.canonical_url
            LEFT JOIN parser_validation_results AS result
              ON result.canonical_url=sample.canonical_url
             AND result.parser_version=active_years.parser_version
            WHERE result.canonical_url IS NULL
              AND capture.status='complete'
              AND capture.raw_path IS NOT NULL
        )
        SELECT canonical_url, raw_path
        FROM ranked
        ORDER BY sample_rank, sample_year
    """
    parameters: list[object] = []
    if maximum is not None:
        query += " LIMIT ?"
        parameters.append(maximum)
    return [
        (str(row[0]), str(row[1]))
        for row in connection.execute(query, parameters).fetchall()
    ]


def failed_completed_parser_validation_files(
    connection: sqlite3.Connection,
    *,
    maximum: int | None,
) -> list[tuple[str, str]]:
    initialize_parser_validation_schema(connection)
    query = """
        SELECT
            result.canonical_url,
            capture.raw_path
        FROM parser_validation_results AS result
        JOIN parser_validation_config AS config
          ON config.sample_year=result.sample_year
         AND config.parser_version=result.parser_version
        JOIN captures AS capture
          ON capture.canonical_url=result.canonical_url
        WHERE result.qa_pass=0
          AND capture.status='complete'
          AND capture.raw_path IS NOT NULL
        ORDER BY result.sample_year, result.canonical_url
    """
    parameters: list[object] = []
    if maximum is not None:
        query += " LIMIT ?"
        parameters.append(maximum)
    return [
        (str(row[0]), str(row[1]))
        for row in connection.execute(query, parameters).fetchall()
    ]


def is_parser_validation_sample(
    connection: sqlite3.Connection,
    canonical_url: str,
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM parser_validation_samples
            WHERE canonical_url=?
            """,
            (canonical_url,),
        ).fetchone()
        is not None
    )


def record_parser_validation(
    connection: sqlite3.Connection,
    *,
    capture: RawCapture,
    archive_root: Path,
) -> dict[str, object]:
    initialize_parser_validation_schema(connection)
    sample_row = connection.execute(
        """
        SELECT sample_year
        FROM parser_validation_samples
        WHERE canonical_url=?
        """,
        (capture.canonical_url,),
    ).fetchone()
    if sample_row is None:
        return {"sample": False}

    sample_year = int(sample_row[0])
    planned_year = sample_year
    parsed_at = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "canonical_url": capture.canonical_url,
        "publisher": capture.publisher,
        "sample_year": sample_year,
        "parser_version": None,
        "extraction_status": ArticleStatus.ERROR.value,
        "content_type": ContentType.ARTICLE.value,
        "qa_pass": 0,
        "body_characters": 0,
        "block_count": 0,
        "images_referenced": 0,
        "images_selected": 0,
        "duplicate_text_blocks": 0,
        "headline_present": 0,
        "published_at_present": 0,
        "source_link_preserved": 0,
        "warnings_json": "[]",
        "issues_json": '["parser-exception"]',
        "error": None,
        "parsed_at": parsed_at.isoformat(),
        "source_raw_sha256": capture.raw_html.sha256,
    }
    try:
        html_bytes = _read_capture_html(capture, archive_root)
        article = parse_article(
            html_bytes,
            publisher=capture.publisher,
            canonical_url=capture.canonical_url,
            raw_capture=capture,
            parsed_at=parsed_at,
        )
        if article.published_at is not None:
            sample_year = article.published_at.year
            values["sample_year"] = sample_year
            if sample_year != planned_year:
                connection.execute(
                    """
                    UPDATE parser_validation_samples
                    SET sample_year=?
                    WHERE canonical_url=?
                    """,
                    (sample_year, capture.canonical_url),
                )
        text_blocks = [
            _normalize_text(block.text)
            for block in article.blocks
            if block.text and _normalize_text(block.text)
        ]
        duplicate_blocks = len(text_blocks) - len(set(text_blocks))
        issues: list[str] = []
        nontext_content = article.content_type in {
            ContentType.INTERACTIVE,
            ContentType.VIDEO,
            ContentType.AUDIO,
            ContentType.GALLERY,
        }
        if (
            article.quality.status != ArticleStatus.COMPLETE
            and not nontext_content
        ):
            issues.append(f"extraction-{article.quality.status.value}")
        if not article.headline:
            issues.append("missing-headline")
        if not article.published_at:
            issues.append("missing-published-at")
        if article.canonical_url != capture.canonical_url:
            issues.append("source-link-mismatch")
        if duplicate_blocks:
            issues.append("duplicate-text-blocks")
        prefix = article.plain_text[:1_500].casefold()
        if (
            article.quality.body_characters < 1_000
            and any(phrase in prefix for phrase in _PAYWALL_PHRASES)
        ):
            issues.append("suspected-paywall-shell")
        values.update(
            {
                "parser_version": article.extraction.parser_version,
                "extraction_status": article.quality.status.value,
                "content_type": article.content_type.value,
                "qa_pass": int(not issues),
                "body_characters": article.quality.body_characters,
                "block_count": article.quality.block_count,
                "images_referenced": article.quality.images_referenced,
                "images_selected": article.quality.images_selected,
                "duplicate_text_blocks": duplicate_blocks,
                "headline_present": int(bool(article.headline)),
                "published_at_present": int(article.published_at is not None),
                "source_link_preserved": int(
                    article.canonical_url == capture.canonical_url
                ),
                "warnings_json": json.dumps(
                    article.quality.warnings,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "issues_json": json.dumps(
                    issues,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
    except Exception as exc:
        values["error"] = f"{type(exc).__name__}: {exc}"

    with connection:
        connection.execute(
            """
            INSERT INTO parser_validation_results(
                canonical_url,
                publisher,
                sample_year,
                parser_version,
                extraction_status,
                content_type,
                qa_pass,
                body_characters,
                block_count,
                images_referenced,
                images_selected,
                duplicate_text_blocks,
                headline_present,
                published_at_present,
                source_link_preserved,
                warnings_json,
                issues_json,
                error,
                parsed_at,
                source_raw_sha256
            )
            VALUES (
                :canonical_url,
                :publisher,
                :sample_year,
                :parser_version,
                :extraction_status,
                :content_type,
                :qa_pass,
                :body_characters,
                :block_count,
                :images_referenced,
                :images_selected,
                :duplicate_text_blocks,
                :headline_present,
                :published_at_present,
                :source_link_preserved,
                :warnings_json,
                :issues_json,
                :error,
                :parsed_at,
                :source_raw_sha256
            )
            ON CONFLICT(canonical_url) DO UPDATE SET
                parser_version=excluded.parser_version,
                extraction_status=excluded.extraction_status,
                content_type=excluded.content_type,
                qa_pass=excluded.qa_pass,
                body_characters=excluded.body_characters,
                block_count=excluded.block_count,
                images_referenced=excluded.images_referenced,
                images_selected=excluded.images_selected,
                duplicate_text_blocks=excluded.duplicate_text_blocks,
                headline_present=excluded.headline_present,
                published_at_present=excluded.published_at_present,
                source_link_preserved=excluded.source_link_preserved,
                warnings_json=excluded.warnings_json,
                issues_json=excluded.issues_json,
                error=excluded.error,
                parsed_at=excluded.parsed_at,
                source_raw_sha256=excluded.source_raw_sha256
            """,
            values,
        )
    return {
        "sample": True,
        "year": sample_year,
        "plannedYear": planned_year,
        "status": values["extraction_status"],
        "qaPass": bool(values["qa_pass"]),
        "issues": json.loads(str(values["issues_json"])),
        "error": values["error"],
    }


def parser_validation_summary(
    connection: sqlite3.Connection,
) -> dict[str, object]:
    initialize_parser_validation_schema(connection)
    result: dict[str, object] = {
        "formatVersion": SCHEMA_VERSION,
        "ready": True,
        "gates": {
            "minimumSamplesPerYear": "configured per year",
            "minimumCompleteRate": MINIMUM_COMPLETE_RATE,
            "minimumQaPassRate": MINIMUM_QA_PASS_RATE,
            "maximumParserErrors": 0,
        },
        "years": {},
    }
    years: dict[str, object] = {}
    configs = connection.execute(
        """
        SELECT sample_year, target_size, parser_version
        FROM parser_validation_config
        ORDER BY sample_year
        """
    ).fetchall()
    for sample_year, target_size, parser_version in configs:
        row = connection.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(qa_pass), 0),
                COALESCE(SUM(extraction_status='complete'), 0),
                COALESCE(SUM(extraction_status='partial'), 0),
                COALESCE(SUM(extraction_status='unsupported'), 0),
                COALESCE(SUM(extraction_status='error'), 0),
                COALESCE(AVG(body_characters), 0),
                COALESCE(SUM(headline_present=0), 0),
                COALESCE(SUM(published_at_present=0), 0),
                COALESCE(SUM(duplicate_text_blocks > 0), 0),
                COALESCE(SUM(images_referenced), 0),
                COALESCE(SUM(images_selected), 0),
                COALESCE(SUM(images_referenced > 0), 0),
                COALESCE(SUM(images_selected > 0), 0)
                ,
                COALESCE(
                    SUM(
                        content_type IN (
                            'interactive',
                            'video',
                            'audio',
                            'gallery'
                        )
                    ),
                    0
                )
            FROM parser_validation_results
            WHERE sample_year=? AND parser_version=?
            """,
            (sample_year, parser_version),
        ).fetchone()
        evaluated = int(row[0])
        planned = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM parser_validation_samples
                WHERE sample_year=?
                """,
                (sample_year,),
            ).fetchone()[0]
        )
        issue_counts: Counter[str] = Counter()
        failure_examples: list[dict[str, object]] = []
        failure_rows = connection.execute(
            """
            SELECT
                canonical_url,
                extraction_status,
                body_characters,
                issues_json,
                error
            FROM parser_validation_results
            WHERE sample_year=?
              AND parser_version=?
              AND qa_pass=0
            ORDER BY
                extraction_status='error' DESC,
                extraction_status='unsupported' DESC,
                body_characters,
                canonical_url
            """,
            (sample_year, parser_version),
        ).fetchall()
        for (
            canonical_url,
            extraction_status,
            body_characters,
            issues_json,
            error,
        ) in failure_rows:
            issues = json.loads(issues_json)
            issue_counts.update(str(issue) for issue in issues)
            if len(failure_examples) < 20:
                failure_examples.append(
                    {
                        "canonicalUrl": str(canonical_url),
                        "status": str(extraction_status),
                        "bodyCharacters": int(body_characters),
                        "issues": issues,
                        **({"error": str(error)} if error else {}),
                    }
                )
        target_reached = evaluated >= int(target_size)
        complete_rate = int(row[2]) / evaluated if evaluated else 0.0
        qa_pass_rate = int(row[1]) / evaluated if evaluated else 0.0
        year_ready = (
            target_reached
            and complete_rate >= MINIMUM_COMPLETE_RATE
            and qa_pass_rate >= MINIMUM_QA_PASS_RATE
            and int(row[5]) == 0
        )
        result["ready"] = bool(result["ready"]) and year_ready
        years[str(sample_year)] = {
            "target": int(target_size),
            "parserVersion": str(parser_version),
            "planned": planned,
            "evaluated": evaluated,
            "targetReached": target_reached,
            "qaPassed": int(row[1]),
            "qaPassRate": round(qa_pass_rate, 4),
            "complete": int(row[2]),
            "completeRate": round(complete_rate, 4),
            "partial": int(row[3]),
            "unsupported": int(row[4]),
            "errors": int(row[5]),
            "averageBodyCharacters": round(float(row[6]), 2),
            "missingHeadline": int(row[7]),
            "missingPublishedAt": int(row[8]),
            "articlesWithDuplicateBlocks": int(row[9]),
            "imagesReferenced": int(row[10]),
            "imagesSelected": int(row[11]),
            "articlesWithImagesReferenced": int(row[12]),
            "articlesWithImagesSelected": int(row[13]),
            "imageSelectionRate": round(
                int(row[11]) / int(row[10]) if int(row[10]) else 0.0,
                4,
            ),
            "nonTextContent": int(row[14]),
            "issueCounts": dict(sorted(issue_counts.items())),
            "failureExamples": failure_examples,
        }
    result["years"] = years
    if not configs:
        result["ready"] = False
    return result


def parser_validation_target_reached(
    connection: sqlite3.Connection,
) -> bool:
    initialize_parser_validation_schema(connection)
    rows = connection.execute(
        """
        SELECT
            config.target_size,
            COUNT(result.canonical_url)
        FROM parser_validation_config AS config
        LEFT JOIN parser_validation_results AS result
          ON result.sample_year=config.sample_year
         AND result.parser_version=config.parser_version
        GROUP BY
            config.sample_year,
            config.target_size,
            config.parser_version
        """
    ).fetchall()
    return bool(rows) and all(
        int(evaluated) >= int(target_size)
        for target_size, evaluated in rows
    )


def _select_additional_samples(
    connection: sqlite3.Connection,
    *,
    publisher: str,
    year: int,
    limit: int,
    seed: str,
    completed_only: bool,
    direct_provider: str | None = None,
) -> list[tuple[str, str]]:
    if limit <= 0:
        return []
    start = f"{year:04d}-01-01"
    end = f"{year + 1:04d}-01-01"
    selected: list[tuple[int, str, str]] = []
    completed_filter = (
        """
          AND capture.status='complete'
          AND capture.raw_path IS NOT NULL
        """
        if completed_only
        else ""
    )
    if direct_provider not in {None, "other", "infini-news"}:
        raise ValueError("unsupported direct capture provider")
    direct_filter = (
        "AND capture.candidates_json LIKE ?"
        if direct_provider is not None
        else ""
    )
    parameters: list[object] = [start, end]
    if direct_provider is not None:
        parameters.append(f'%"provider":"{direct_provider}"%')
    rows: Iterable[tuple[str]] = connection.execute(
        f"""
        SELECT capture.canonical_url
        FROM captures AS capture
        LEFT JOIN parser_validation_samples AS sample
          ON sample.canonical_url=capture.canonical_url
        WHERE capture.published_at >= ?
          AND capture.published_at < ?
          AND (
            capture.status != 'complete'
            OR capture.raw_path IS NOT NULL
          )
          {completed_filter}
          {direct_filter}
          AND sample.canonical_url IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM parser_validation_exclusions AS exclusion
            WHERE exclusion.canonical_url=capture.canonical_url
          )
        """,
        parameters,
    )
    source_spec = archive_source_spec(publisher)
    for (canonical_url,) in rows:
        if normalize_article_url(source_spec, str(canonical_url)) is None:
            continue
        priority = hashlib.sha256(
            f"{seed}\0{publisher}\0{year}\0{canonical_url}".encode("utf-8")
        ).hexdigest()
        numeric = int(priority, 16)
        candidate = (-numeric, str(canonical_url), priority)
        if len(selected) < limit:
            heapq.heappush(selected, candidate)
        elif numeric < -selected[0][0]:
            heapq.heapreplace(selected, candidate)
    return sorted(
        ((priority, canonical_url) for _, canonical_url, priority in selected),
        key=lambda item: item[0],
    )


def _read_capture_html(capture: RawCapture, archive_root: Path) -> bytes:
    raw_path = archive_root / capture.raw_html.path
    if capture.raw_html.content_encoding == "gzip":
        with gzip.open(raw_path, "rb") as handle:
            content = handle.read()
    else:
        content = raw_path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != capture.raw_html.sha256:
        raise ValueError(
            "raw HTML checksum mismatch: "
            f"expected {capture.raw_html.sha256}, got {actual}"
        )
    return content


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
