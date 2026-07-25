from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sqlite3


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "capture_action_state.py"
)
SPEC = spec_from_file_location("capture_action_state", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def create_state(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE captures(status TEXT NOT NULL, attempts INTEGER NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO captures(status, attempts) VALUES (?, ?)",
        [
            ("complete", 1),
            ("pending", 4),
            ("error", 2),
            ("error", 3),
        ],
    )
    connection.commit()
    connection.close()


def test_missing_state_starts_capture_chain(tmp_path: Path):
    result = MODULE.action_state(
        tmp_path / "missing.sqlite3",
        maximum_record_attempts=3,
    )
    assert result["shouldContinue"] is True
    assert result["retryErrors"] is False


def test_pending_precedes_error_retries(tmp_path: Path):
    state = tmp_path / "capture.sqlite3"
    create_state(state)
    result = MODULE.action_state(state, maximum_record_attempts=3)

    assert result["actionable"] == 2
    assert result["retryErrors"] is False
    assert result["terminalUnresolved"] == 1


def test_retry_errors_after_pending_finishes(tmp_path: Path):
    state = tmp_path / "capture.sqlite3"
    create_state(state)
    connection = sqlite3.connect(state)
    connection.execute("DELETE FROM captures WHERE status='pending'")
    connection.commit()
    connection.close()

    result = MODULE.action_state(state, maximum_record_attempts=3)

    assert result["retryErrors"] is True
    assert result["shouldContinue"] is True
