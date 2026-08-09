from __future__ import annotations

from jojo_olds_api.ap_legacy_catalog import (
    ap_hosted_page_metadata,
    build_ap_hosted_manifest_rows,
)


def _row(
    original: str,
    *,
    timestamp: str,
    digest: str,
) -> dict[str, object]:
    return {
        "url": original,
        "timestamp": timestamp,
        "status": "200",
        "mime": "text/html",
        "digest": digest,
        "length": "84520",
    }


def test_builds_distinct_hosted_ap_revisions_and_deduplicates_sites():
    first = (
        "http://hosted.ap.org/dynamic/stories/A/AF_IVORY_COAST"
        "?SITE=AZPHG&SECTION=HOME&TEMPLATE=DEFAULT"
        "&CTIME=2011-01-11-12-21-19"
    )
    default_site = first.replace("SITE=AZPHG", "SITE=AP")
    second_revision = first.replace(
        "CTIME=2011-01-11-12-21-19",
        "CTIME=2011-01-13-16-03-00",
    )

    rows, metrics = build_ap_hosted_manifest_rows(
        [
            _row(first, timestamp="20110113114709", digest="A"),
            _row(default_site, timestamp="20110113114800", digest="B"),
            _row(second_revision, timestamp="20110116183019", digest="C"),
        ],
        from_year=2011,
        to_year=2011,
    )

    assert metrics["articles"] == 2
    assert rows[0]["canonicalUrl"] == (
        "https://hosted.ap.org/dynamic/stories/A/AF_IVORY_COAST"
        "?CTIME=2011-01-11-12-21-19"
    )
    assert rows[1]["canonicalUrl"].endswith(
        "?CTIME=2011-01-13-16-03-00"
    )
    assert rows[0]["publishedAt"] == "2011-01-11T12:21:19+00:00"
    assert len(rows[0]["candidates"]) == 2
    assert rows[0]["candidates"][0]["provider"] == "arquivo-pt"
    assert "SITE=AP" in rows[0]["candidates"][0]["snapshotUrl"]


def test_rejects_missing_ctime_wrong_year_and_non_html_rows():
    valid = (
        "http://hosted.ap.org/dynamic/stories/A/AF_IVORY_COAST"
        "?SITE=AP&CTIME=2011-01-11-12-21-19"
    )
    missing_ctime = valid.split("?", 1)[0] + "?SITE=AP"
    wrong_year = valid.replace("2011-", "2012-", 1)
    non_html = _row(valid, timestamp="20110113114709", digest="D")
    non_html["mime"] = "image/jpeg"

    rows, metrics = build_ap_hosted_manifest_rows(
        [
            _row(missing_ctime, timestamp="20110113114709", digest="A"),
            _row(wrong_year, timestamp="20120113114709", digest="B"),
            non_html,
        ],
        from_year=2011,
        to_year=2011,
    )

    assert rows == []
    assert metrics["rowsRejected"] == 3


def test_reads_identity_metadata_from_missing_ctime_story_page():
    result = ap_hosted_page_metadata(
        b"""
        <table class="ap-story-table hnews hentry item">
          <tr><td>
            <div class="timestamp updated" title="2011-01-16T1946Z"></div>
            <span class="headline entry-title">
              Gunbattles, food shortages temper Tunisians' joy
            </span>
            <span class="entry-content">
              <p>This complete Associated Press report contains enough
              substantive reporting to establish that the replay is an
              article rather than a front page, error shell, or redirect.</p>
            </span>
          </td></tr>
        </table>
        """
    )

    assert result is not None
    published_at, headline = result
    assert published_at.isoformat() == "2011-01-16T19:46:00+00:00"
    assert headline == "Gunbattles, food shortages temper Tunisians' joy"
