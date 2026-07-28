#!/usr/bin/env python3
"""Pre-index official FTChinese mirrors for parser-validation samples."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from jojo_olds_api.raw_archive_capture import (
    ArchiveClient,
    CaptureCandidate,
    CaptureProvider,
    ManifestItem,
    _discover_ft_headline_from_google_news,
    _discover_ftchinese_candidates,
    _fetch_syndication_search_results,
    _same_article_url,
    _validate_ft_syndication_response,
    ft_syndication_search_url,
)


@dataclass(frozen=True)
class Row:
    canonical_url: str
    published_at: str | None
    section: str | None
    candidates_json: str


def _discover(row: Row) -> tuple[str, CaptureCandidate | None]:
    item = ManifestItem(
        publisher="ft",
        canonical_url=row.canonical_url,
        published_at=row.published_at,
        section=row.section,
        candidates=(),
    )
    client = ArchiveClient(
        minimum_interval=0.0,
        timeout=8.0,
        attempts=1,
    )
    try:
        headline: str | None = None
        try:
            results = _fetch_syndication_search_results(
                item,
                archive_client=client,
                search_url=ft_syndication_search_url(item),
            )
            headline = next(
                (
                    title
                    for _, title, candidate_url in results
                    if title
                    and _same_article_url(
                        candidate_url,
                        item.canonical_url,
                    )
                ),
                None,
            )
        except Exception:
            pass
        if not headline:
            try:
                headline = _discover_ft_headline_from_google_news(
                    item,
                    archive_client=client,
                )
            except Exception:
                pass
        if not headline:
            return row.canonical_url, None
        try:
            candidates = _discover_ftchinese_candidates(
                archive_client=client,
                expected_headline=headline,
                attempts=1,
                timeout=8.0,
            )
        except Exception:
            candidates = ()
        if candidates:
            candidate = candidates[0]
            try:
                status, _, content, final_url = client.fetch(
                    candidate.snapshot_url,
                    maximum_bytes=2_000_000,
                )
                validated, _ = _validate_ft_syndication_response(
                    item,
                    expected_partner_url=candidate.snapshot_url,
                    expected_headline=headline,
                    content=content,
                    final_url=final_url,
                )
                if status != 200 or not validated:
                    candidates = ()
            except Exception:
                candidates = ()
        return (
            row.canonical_url,
            candidates[0] if candidates else None,
        )
    finally:
        client.close()


def enrich(
    state_path: Path,
    *,
    limit: int,
    workers: int,
) -> dict[str, int]:
    connection = sqlite3.connect(state_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ft_validation_mirror_discovery (
                canonical_url TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                candidate_url TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        # A pre-0.20 manifest refresh could remove a discovered candidate
        # before the live checkpoint. Requeue only those inconsistent rows;
        # successful post-0.20 candidates remain durable.
        connection.execute(
            """
            DELETE FROM ft_validation_mirror_discovery
            WHERE status='candidate'
              AND canonical_url IN (
                  SELECT d.canonical_url
                  FROM ft_validation_mirror_discovery AS d
                  JOIN captures AS c USING (canonical_url)
                  WHERE d.candidate_url IS NULL
                     OR instr(c.candidates_json, d.candidate_url)=0
              )
            """
        )
        rows = [
            Row(*row)
            for row in connection.execute(
                """
                SELECT c.canonical_url, c.published_at, c.section,
                       c.candidates_json
                FROM parser_validation_samples AS s
                JOIN captures AS c USING (canonical_url)
                LEFT JOIN parser_validation_results AS r
                  USING (canonical_url)
                LEFT JOIN ft_validation_mirror_discovery AS d
                  USING (canonical_url)
                WHERE r.canonical_url IS NULL
                  AND d.canonical_url IS NULL
                  AND c.status IN ('pending', 'error', 'downloading')
                  AND c.candidates_json NOT LIKE '%"provider":"other"%'
                ORDER BY s.sample_priority
                LIMIT ?
                """,
                (limit,),
            )
        ]
        by_url = {row.canonical_url: row for row in rows}
        discovered: list[tuple[str, CaptureCandidate]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers)
        ) as executor:
            for canonical_url, candidate in executor.map(
                _discover,
                rows,
            ):
                if candidate is not None:
                    discovered.append((canonical_url, candidate))
        now = datetime.now(UTC).isoformat()
        discovered_by_url = dict(discovered)
        for row in rows:
            candidate = discovered_by_url.get(row.canonical_url)
            connection.execute(
                """
                INSERT OR REPLACE INTO ft_validation_mirror_discovery (
                    canonical_url, status, candidate_url, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    row.canonical_url,
                    "candidate" if candidate is not None else "not-found",
                    candidate.snapshot_url if candidate is not None else None,
                    now,
                ),
            )
        for canonical_url, candidate in discovered:
            row = by_url[canonical_url]
            existing = json.loads(row.candidates_json)
            serialized = {
                "provider": CaptureProvider.OTHER.value,
                "snapshotUrl": candidate.snapshot_url,
                "expectedHeadline": candidate.expected_headline,
            }
            connection.execute(
                """
                UPDATE captures
                SET candidates_json = ?, status = 'pending',
                    attempts = 0, last_error = NULL, updated_at = ?
                WHERE canonical_url = ?
                """,
                (
                    json.dumps(
                        [serialized, *existing],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                    canonical_url,
                ),
            )
        connection.commit()
        return {
            "scanned": len(rows),
            "discovered": len(discovered),
        }
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    result = enrich(
        args.state,
        limit=max(1, args.limit),
        workers=max(1, args.workers),
    )
    print(json.dumps(result, sort_keys=True))
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"scanned={result['scanned']}\n")
            handle.write(f"discovered={result['discovered']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
