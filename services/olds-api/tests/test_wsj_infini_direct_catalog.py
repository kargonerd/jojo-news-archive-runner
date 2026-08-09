from __future__ import annotations

import sqlite3
from pathlib import Path

import pyarrow as pa

from jojo_olds_api import wsj_infini_direct_catalog as catalog
from jojo_olds_api.archive_sources import archive_source_spec
from jojo_olds_api.wayback_manifest import (
    discovery_summary,
    initialize_discovery_schema,
)
from jojo_olds_api.wsj_infini_catalog import initialize_wsj_infini_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BUILD_TOOL = (
    REPOSITORY_ROOT
    / "services"
    / "olds-api"
    / "tools"
    / "build_wayback_manifest.py"
)


def test_scan_accepts_only_strict_wsj_origin_rows(monkeypatch):
    valid_url = (
        "http://www.wsj.com/articles/"
        "is-indias-war-on-cash-paying-off-1483518944"
    )
    table = pa.table(
        {
            "url": [
                valid_url,
                valid_url,
                "https://example.com/articles/not-wsj-1483518944",
                "https://www.wsj.com/articles/too-short-1483518944",
                "https://www.wsj.com/articles/wrong-year-1515054944",
                "https://www.wsj.com/video/not-an-article",
            ],
            "url_hostname": [
                "www.wsj.com",
                "online.wsj.com",
                "example.com",
                "www.wsj.com",
                "www.wsj.com",
                "www.wsj.com",
            ],
            "warc_filename": [
                "CC-NEWS-20170104084927-00052.warc.gz"
            ]
            * 6,
            "publish_date": [
                "2017-01-04",
                "2017-01-04",
                "2017-01-04",
                "2017-01-04",
                "2017-01-04",
                "2017-01-04",
            ],
            "title": [
                "Is India’s War on Cash Paying Off?",
                "Metadata hostname does not match source URL",
                "An unrelated source article headline",
                "A valid looking but short article",
                "A URL timestamp from another year",
                "A rejected WSJ video page title",
            ],
            "text_length": [449, 900, 900, 299, 900, 900],
            "language": ["eng_Latn"] * 6,
        }
    )

    class OpenFile:
        def open(self):
            return self

        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    import fsspec
    import pyarrow.parquet as pq

    monkeypatch.setattr(fsspec, "open", lambda *_args, **_kwargs: OpenFile())
    monkeypatch.setattr(pq, "read_table", lambda *_args, **_kwargs: table)

    rows = catalog._scan_parquet_file(
        "data/year=2017/month=01/part-test.parquet",
        year=2017,
    )

    assert len(rows) == 1
    assert rows[0]["canonicalUrl"] == valid_url.replace("http://", "https://")
    assert rows[0]["publishedAt"] == "2017-01-04"
    assert rows[0]["textLength"] == 449


def test_direct_catalog_is_bounded_resumable_and_merges_urls(monkeypatch):
    connection = sqlite3.connect(":memory:")
    initialize_discovery_schema(
        connection,
        spec=archive_source_spec("wsj"),
        from_year=2017,
        to_year=2017,
        collapse="urlkey",
    )
    initialize_wsj_infini_schema(
        connection,
        from_year=2017,
        to_year=2017,
    )
    files = [
        ("data/year=2017/month=01/part-a.parquet", 100),
        ("data/year=2017/month=01/part-b.parquet", 200),
    ]
    monkeypatch.setattr(
        catalog,
        "_list_year_parquet_files",
        lambda *_args, **_kwargs: files,
    )

    def scan(path: str, *, year: int):
        suffix = "a" if path.endswith("a.parquet") else "b"
        return [
            {
                "canonicalUrl": f"https://www.wsj.com/articles/test-{suffix}-1483518944",
                "sourceUrl": f"http://www.wsj.com/articles/test-{suffix}-1483518944",
                "publishedAt": "2017-01-04",
                "expectedHeadline": f"A complete WSJ test article {suffix}",
                "textLength": 500,
                "warcFilename": "CC-NEWS-20170104084927-00052.warc.gz",
                "parquetRowIndex": 3,
            }
        ]

    monkeypatch.setattr(catalog, "_scan_parquet_file", scan)

    first = catalog.process_wsj_infini_direct_catalog(
        connection,
        from_year=2017,
        to_year=2017,
        http_client=object(),
        maximum_files=1,
        workers=1,
        target_articles=2,
    )
    second = catalog.process_wsj_infini_direct_catalog(
        connection,
        from_year=2017,
        to_year=2017,
        http_client=object(),
        maximum_files=1,
        workers=1,
        target_articles=2,
    )

    assert first["listedFiles"] == 2
    assert first["attemptedFiles"] == 1
    assert first["articles"] == 1
    assert first["shouldContinue"] is True
    assert second["listedFiles"] == 0
    assert second["attemptedFiles"] == 1
    assert second["articles"] == 2
    assert second["shouldContinue"] is False
    assert connection.execute(
        "SELECT COUNT(*) FROM wsj_infini_articles"
    ).fetchone()[0] == 2
    summary = catalog.wsj_infini_direct_summary(connection)
    assert summary is not None
    assert summary["years"]["2017"]["status"] == "complete"
    assert summary["years"]["2017"]["articles"] == 2
    combined = discovery_summary(connection)
    assert combined["wsjInfiniDirect"] == summary
    assert combined["shouldContinue"] is True


def test_build_tool_bounds_direct_scan_to_ten_files_per_discovery_page():
    tool = BUILD_TOOL.read_text(encoding="utf-8")

    assert "maximum_files=max(1, args.max_pages or 5) * 10" in tool
    assert "workers=8" in tool
    assert "if wsj_infini_direct_should_continue(connection):" in tool
    assert '"status": "deferred-for-direct-catalog"' in tool
