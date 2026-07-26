from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from jojo_olds_api.raw_archive_capture import (
    initialize_capture_schema,
    load_capture_manifest,
)
from jojo_olds_api.source_capture_import import (
    import_selected_source_captures,
)


def _write_manifest(path: Path) -> tuple[str, str]:
    urls = (
        "https://www.wsj.com/articles/source-import-one-123",
        "https://www.wsj.com/articles/source-import-two-456",
    )
    path.write_text(
        "".join(
            json.dumps(
                {
                    "publisher": "wsj",
                    "canonicalUrl": url,
                    "publishedAt": "2016-05-01T00:00:00Z",
                    "candidates": [
                        {
                            "provider": "wayback",
                            "snapshotUrl": (
                                "https://web.archive.org/web/"
                                f"20160502000000id_/{url}"
                            ),
                        }
                    ],
                }
            )
            + "\n"
            for url in urls
        ),
        encoding="utf-8",
    )
    return urls


def test_imports_only_selected_incomplete_source_captures(
    tmp_path: Path,
):
    manifest = tmp_path / "manifest.jsonl"
    first_url, second_url = _write_manifest(manifest)
    source = sqlite3.connect(":memory:")
    target = sqlite3.connect(":memory:")
    initialize_capture_schema(
        source,
        publisher="wsj",
        authorization_reference="authorization:test",
    )
    load_capture_manifest(
        source,
        manifest_path=manifest,
        publisher="wsj",
    )
    selected_candidate = json.dumps(
        {
            "provider": "wayback",
            "snapshotUrl": (
                "https://web.archive.org/web/20160502000000id_/"
                + first_url
            ),
        }
    )
    raw_sha256 = "a" * 64
    source.execute(
        """
        UPDATE captures
        SET status='complete',
            selected_candidate_json=?,
            final_url=?,
            http_status=200,
            content_type='text/html',
            quality_score=100,
            quality_signals_json='{"usable":true}',
            raw_path=?,
            raw_sha256=?,
            raw_bytes=2048,
            stored_bytes=512,
            retrieved_at='2026-07-26T00:00:00+00:00'
        WHERE canonical_url=?
        """,
        (
            selected_candidate,
            first_url,
            f"objects/{raw_sha256[:2]}/{raw_sha256}.html.gz",
            raw_sha256,
            first_url,
        ),
    )
    source.commit()

    result = import_selected_source_captures(
        source_connection=source,
        target_connection=target,
        manifest_path=manifest,
        publisher="wsj",
        sample_year=2016,
        target_per_year=1,
    )
    rows = target.execute(
        """
        SELECT canonical_url, status, raw_sha256, record_path
        FROM captures
        ORDER BY canonical_url
        """
    ).fetchall()

    assert result["imported"] == 1
    assert result["sourceMatches"] == 1
    assert result["rawPaths"] == [
        f"objects/{raw_sha256[:2]}/{raw_sha256}.html.gz"
    ]
    assert rows == [
        (first_url, "complete", raw_sha256, None),
        (second_url, "pending", None, None),
    ]
