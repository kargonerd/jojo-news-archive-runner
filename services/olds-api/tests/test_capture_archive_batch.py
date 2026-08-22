from pathlib import Path
import sqlite3

from tools.capture_archive_batch import _record_validation_if_selected


def test_non_validation_capture_does_not_query_validation_tables(
    tmp_path: Path,
):
    connection = sqlite3.connect(":memory:")

    result = _record_validation_if_selected(
        connection,
        validation_plan=None,
        capture=object(),
        canonical_url="https://www.nikkei.com/article/DGXLASFS15H2P",
        validation_target_reached=False,
        archive_root=tmp_path,
    )

    assert result is None
