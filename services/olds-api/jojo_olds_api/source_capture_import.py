from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .parser_validation import ensure_parser_validation_plan
from .raw_archive_capture import (
    initialize_capture_schema,
    load_capture_manifest,
)


SOURCE_CAPTURE_COLUMNS = (
    "canonical_url",
    "selected_candidate_json",
    "final_url",
    "http_status",
    "content_type",
    "quality_score",
    "quality_signals_json",
    "raw_path",
    "raw_sha256",
    "raw_bytes",
    "stored_bytes",
    "retrieved_at",
)


def import_selected_source_captures(
    *,
    source_connection: sqlite3.Connection,
    target_connection: sqlite3.Connection,
    manifest_path: Path,
    publisher: str,
    sample_year: int,
    target_per_year: int = 500,
    maximum_record_attempts: int = 3,
) -> dict[str, object]:
    initialize_capture_schema(
        target_connection,
        publisher=publisher,
        authorization_reference="user-provided-authorization",
    )
    manifest_result = load_capture_manifest(
        target_connection,
        manifest_path=manifest_path,
        publisher=publisher,
    )
    plan = ensure_parser_validation_plan(
        target_connection,
        publisher=publisher,
        from_year=sample_year,
        to_year=sample_year,
        target_per_year=target_per_year,
        maximum_record_attempts=maximum_record_attempts,
    )
    selected_urls = [
        str(row[0])
        for row in target_connection.execute(
            """
            SELECT sample.canonical_url
            FROM parser_validation_samples AS sample
            JOIN captures AS capture
              ON capture.canonical_url=sample.canonical_url
            WHERE sample.sample_year=?
              AND capture.status!='complete'
            ORDER BY sample.sample_priority, sample.canonical_url
            """,
            (sample_year,),
        )
    ]
    source_rows: dict[str, tuple] = {}
    for offset in range(0, len(selected_urls), 500):
        batch = selected_urls[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in source_connection.execute(
            f"""
            SELECT {", ".join(SOURCE_CAPTURE_COLUMNS)}
            FROM captures
            WHERE status='complete'
              AND canonical_url IN ({placeholders})
            """,
            batch,
        ):
            source_rows[str(row[0])] = row

    imported_paths: list[str] = []
    imported = 0
    now = datetime.now(timezone.utc).isoformat()
    with target_connection:
        for canonical_url in selected_urls:
            row = source_rows.get(canonical_url)
            if row is None:
                continue
            values = dict(zip(SOURCE_CAPTURE_COLUMNS, row, strict=True))
            raw_path = values["raw_path"]
            raw_sha256 = values["raw_sha256"]
            if (
                not isinstance(raw_path, str)
                or not raw_path.startswith("objects/")
                or ".." in Path(raw_path).parts
                or not isinstance(raw_sha256, str)
                or len(raw_sha256) != 64
            ):
                continue
            target_connection.execute(
                """
                UPDATE captures
                SET status='complete',
                    selected_candidate_json=?,
                    final_url=?,
                    http_status=?,
                    content_type=?,
                    quality_score=?,
                    quality_signals_json=?,
                    raw_path=?,
                    raw_sha256=?,
                    raw_bytes=?,
                    stored_bytes=?,
                    record_path=NULL,
                    last_error=NULL,
                    retrieved_at=?,
                    updated_at=?
                WHERE canonical_url=?
                  AND status!='complete'
                """,
                (
                    values["selected_candidate_json"],
                    values["final_url"],
                    values["http_status"],
                    values["content_type"],
                    values["quality_score"],
                    values["quality_signals_json"],
                    raw_path,
                    raw_sha256,
                    values["raw_bytes"],
                    values["stored_bytes"],
                    values["retrieved_at"],
                    now,
                    canonical_url,
                ),
            )
            if target_connection.execute(
                "SELECT changes()"
            ).fetchone()[0]:
                imported += 1
                imported_paths.append(raw_path)
    return {
        "publisher": publisher,
        "sampleYear": sample_year,
        "selectedIncomplete": len(selected_urls),
        "sourceMatches": len(source_rows),
        "imported": imported,
        "rawPaths": sorted(set(imported_paths)),
        "manifest": manifest_result,
        "plan": plan,
    }
