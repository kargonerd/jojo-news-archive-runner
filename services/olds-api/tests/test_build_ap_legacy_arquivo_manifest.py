from __future__ import annotations

import httpx

from jojo_olds_api.ap_legacy_catalog import build_ap_hosted_manifest_rows
from tools import build_ap_legacy_arquivo_manifest as tool


def test_recovers_missing_ctime_from_hosted_ap_page_metadata():
    original = (
        "http://hosted.ap.org/dynamic/stories/A/AFR_POL_SUDAN_SPGL-"
        "?SITE=AP&SECTION=HOME&TEMPLATE=DEFAULT"
    )
    rows = [
        {
            "url": original,
            "timestamp": "20110111223516",
            "status": "200",
            "mime": "text/html",
            "digest": "SAME-CONTENT",
            "length": "80000",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "arquivo.pt/noFrame/replay/20110111223516/" in str(
            request.url
        )
        return httpx.Response(
            200,
            content=b"""
            <table class="ap-story-table hnews hentry item">
              <tr><td>
                <div class="timestamp updated"
                     title="2011-01-11T1023Z"></div>
                <span class="headline entry-title">
                  Reportes de violencia y muertos por enfrentamientos
                </span>
                <span class="entry-content">
                  <p>This archived AP story has enough substantive article
                  text for the recovery guard to reject navigation and error
                  shells while accepting a real legacy story page.</p>
                </span>
              </td></tr>
            </table>
            """,
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        recovered, metrics = tool.recover_missing_ctime_rows(
            rows,
            client,
            workers=2,
            attempts=1,
        )

    assert metrics == {
        "missingCtimeRows": 1,
        "recoveryGroups": 1,
        "recoveredGroups": 1,
        "recoveredRows": 1,
        "recoveryFailures": 0,
    }
    assert recovered[0]["canonicalUrl"].endswith(
        "?CTIME=2011-01-11-10-23-00"
    )
    assert recovered[0]["expectedHeadline"].startswith(
        "Reportes de violencia"
    )

    manifest, manifest_metrics = build_ap_hosted_manifest_rows(
        recovered,
        from_year=2011,
        to_year=2011,
    )
    assert manifest_metrics["articles"] == 1
    assert manifest[0]["canonicalUrl"] == (
        "https://hosted.ap.org/dynamic/stories/A/AFR_POL_SUDAN_SPGL-"
        "?CTIME=2011-01-11-10-23-00"
    )
    assert manifest[0]["candidates"][0]["expectedHeadline"].startswith(
        "Reportes de violencia"
    )
