from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3


TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "revalidate_parser_samples.py"
)
SPEC = importlib.util.spec_from_file_location(
    "revalidate_parser_samples_tool",
    TOOL_PATH,
)
assert SPEC is not None and SPEC.loader is not None
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


def test_forced_replay_candidates_reports_missing_raw_objects(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY
        );
        CREATE TABLE parser_validation_samples (
            canonical_url TEXT PRIMARY KEY,
            sample_priority INTEGER NOT NULL
        );
        CREATE TABLE captures (
            canonical_url TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            raw_path TEXT
        );
        INSERT INTO parser_validation_results VALUES
            ('https://example.com/present'),
            ('https://example.com/missing'),
            ('https://example.com/no-path');
        INSERT INTO parser_validation_samples VALUES
            ('https://example.com/present', 1),
            ('https://example.com/missing', 2),
            ('https://example.com/no-path', 3);
        INSERT INTO captures VALUES
            ('https://example.com/present', 'complete', 'objects/present.html'),
            ('https://example.com/missing', 'complete', 'objects/missing.html'),
            ('https://example.com/no-path', 'complete', NULL);
        """
    )
    present = tmp_path / "objects" / "present.html"
    present.parent.mkdir()
    present.write_text("archive", encoding="utf-8")

    replayable, missing = TOOL.forced_replay_candidates(
        connection,
        archive_root=tmp_path,
        maximum=500,
    )

    assert replayable == ["https://example.com/present"]
    assert missing == [
        ("https://example.com/missing", "objects/missing.html"),
        (
            "https://example.com/no-path",
            "<missing raw_path for https://example.com/no-path>",
        ),
    ]


def test_requeue_missing_validation_capture_resets_capture_and_result() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE parser_validation_results (
            canonical_url TEXT PRIMARY KEY
        );
        CREATE TABLE captures (
            canonical_url TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT INTO parser_validation_results VALUES
            ('https://example.com/missing');
        INSERT INTO captures VALUES
            ('https://example.com/missing', 'complete', 2, NULL, 'old');
        """
    )

    TOOL.requeue_missing_validation_capture(
        connection,
        canonical_url="https://example.com/missing",
    )

    assert connection.execute(
        "SELECT status, attempts, last_error FROM captures"
    ).fetchone() == (
        "pending",
        0,
        "raw quality policy rejected stored capture: "
        "validation-raw-object-missing",
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM parser_validation_results"
    ).fetchone() == (0,)
