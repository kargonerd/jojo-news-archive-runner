from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import heapq
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .news_models import ArticleStatus, RawCapture
from .news_parser import parse_article


SCHEMA_VERSION = "jojo-parser-validation/1"
DEFAULT_SEED = "jojo-parser-validation-v1"
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
            parsed_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_parser_validation_results_year
            ON parser_validation_results(sample_year, qa_pass);
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
    now = _now_iso()
    connection.executemany(
        """
        INSERT INTO parser_validation_config(
            sample_year, target_size, seed, updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(sample_year) DO UPDATE SET
            target_size=excluded.target_size,
            seed=excluded.seed,
            updated_at=excluded.updated_at
        """,
        (
            (year, target_per_year, seed, now)
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
                WHERE sample_year=?
                """,
                (year,),
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
                WHERE sample.sample_year=?
                  AND result.canonical_url IS NULL
                  AND (
                    capture.status='pending'
                    OR (
                      capture.status='error'
                      AND capture.attempts < ?
                    )
                  )
                """,
                (year, maximum_record_attempts),
            ).fetchone()[0]
        )
        desired_actionable = max(0, target_per_year - evaluated) + reserve
        add_count = max(0, desired_actionable - actionable)
        selected = _select_additional_samples(
            connection,
            publisher=publisher,
            year=year,
            limit=add_count,
            seed=seed,
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
            "actionableBeforePlanning": actionable,
            "addedToPlan": len(selected),
        }
    connection.commit()
    return {
        "formatVersion": SCHEMA_VERSION,
        "publisher": publisher,
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
                config.target_size
            FROM parser_validation_config AS config
            LEFT JOIN parser_validation_results AS result
              ON result.sample_year=config.sample_year
            GROUP BY config.sample_year, config.target_size
            HAVING COUNT(result.canonical_url) < config.target_size
        ),
        ranked AS (
            SELECT
                sample.canonical_url,
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
    parsed_at = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "canonical_url": capture.canonical_url,
        "publisher": capture.publisher,
        "sample_year": sample_year,
        "parser_version": None,
        "extraction_status": ArticleStatus.ERROR.value,
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
        text_blocks = [
            _normalize_text(block.text)
            for block in article.blocks
            if block.text and _normalize_text(block.text)
        ]
        duplicate_blocks = len(text_blocks) - len(set(text_blocks))
        issues: list[str] = []
        if article.quality.status != ArticleStatus.COMPLETE:
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
                parsed_at
            )
            VALUES (
                :canonical_url,
                :publisher,
                :sample_year,
                :parser_version,
                :extraction_status,
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
                :parsed_at
            )
            ON CONFLICT(canonical_url) DO UPDATE SET
                parser_version=excluded.parser_version,
                extraction_status=excluded.extraction_status,
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
                parsed_at=excluded.parsed_at
            """,
            values,
        )
    return {
        "sample": True,
        "year": sample_year,
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
        SELECT sample_year, target_size
        FROM parser_validation_config
        ORDER BY sample_year
        """
    ).fetchall()
    for sample_year, target_size in configs:
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
                COALESCE(SUM(duplicate_text_blocks > 0), 0)
            FROM parser_validation_results
            WHERE sample_year=?
            """,
            (sample_year,),
        ).fetchone()
        evaluated = int(row[0])
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
        }
    result["years"] = years
    if not configs:
        result["ready"] = False
    return result


def _select_additional_samples(
    connection: sqlite3.Connection,
    *,
    publisher: str,
    year: int,
    limit: int,
    seed: str,
) -> list[tuple[str, str]]:
    if limit <= 0:
        return []
    start = f"{year:04d}-01-01"
    end = f"{year + 1:04d}-01-01"
    selected: list[tuple[int, str, str]] = []
    rows: Iterable[tuple[str]] = connection.execute(
        """
        SELECT capture.canonical_url
        FROM captures AS capture
        LEFT JOIN parser_validation_samples AS sample
          ON sample.canonical_url=capture.canonical_url
        WHERE capture.published_at >= ?
          AND capture.published_at < ?
          AND capture.status != 'complete'
          AND sample.canonical_url IS NULL
        """,
        (start, end),
    )
    for (canonical_url,) in rows:
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
