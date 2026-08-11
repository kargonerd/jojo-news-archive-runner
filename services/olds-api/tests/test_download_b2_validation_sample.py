from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from urllib.error import URLError

import pytest

from tools.download_b2_validation_sample import (
    authorize,
    download_url,
    safe_local_path,
)


def test_authorize_retries_transient_tls_failure(monkeypatch) -> None:
    payload = BytesIO(
        json.dumps(
            {
                "authorizationToken": "token",
                "apiInfo": {
                    "storageApi": {"downloadUrl": "https://download.example"}
                },
            }
        ).encode()
    )
    responses = iter([URLError("temporary TLS failure"), payload])

    def fake_urlopen(*_args, **_kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "tools.download_b2_validation_sample.urlopen", fake_urlopen
    )
    monkeypatch.setattr(
        "tools.download_b2_validation_sample.time.sleep", lambda _seconds: None
    )

    assert authorize("key-id", "application-key") == (
        "token",
        "https://download.example",
    )


def test_download_url_encodes_each_object_name_segment() -> None:
    assert download_url(
        "https://download.example",
        "private bucket",
        "news archive/object+a.gz",
    ) == (
        "https://download.example/file/private%20bucket/"
        "news%20archive/object%2Ba.gz"
    )


def test_safe_local_path_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe archive path"):
        safe_local_path(tmp_path, "objects/../secret")

    with pytest.raises(ValueError, match="unsafe archive path"):
        safe_local_path(tmp_path, "/absolute/object.gz")


def test_safe_local_path_maps_posix_object_path(tmp_path: Path) -> None:
    assert safe_local_path(tmp_path, "objects/ab/file.gz") == (
        tmp_path / "objects" / "ab" / "file.gz"
    )
