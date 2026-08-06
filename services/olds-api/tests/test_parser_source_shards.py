import pytest

from jojo_olds_api.parser_source_shards import parser_source_manifest_shard


@pytest.mark.parametrize(
    ("publisher", "year", "expected"),
    [
        ("ap", 2010, "ap/2010-2015/sitemap-wayback"),
        ("bloomberg", 2015, "bloomberg/2010-2015/sitemap-wayback"),
        ("ft", 2016, "ft/2016-2026/sitemap-wayback"),
        ("nyt", 2026, "nyt/2016-2026/sitemap-wayback"),
        ("wsj", 2014, "wsj/2010-2015/wayback-urlkey"),
        ("wsj", 2020, "wsj/2016-2026/wayback-urlkey"),
        ("reuters", 2015, "reuters/2010-2015/wayback-urlkey"),
        ("reuters", 2016, "reuters/2016-2020/wayback-urlkey"),
        (
            "reuters",
            2021,
            "reuters/2021-2026/reuters-sitemap-wayback",
        ),
        ("axios", 2014, "axios/2010-2015/wayback"),
        ("npr", 2020, "npr/2016-2026/wayback"),
        ("nikkei", 2012, "nikkei/2010-2015/wayback"),
        ("zaobao", 2024, "zaobao/2016-2026/wayback"),
        ("aljazeera", 2018, "aljazeera/2016-2026/wayback"),
        ("scmp", 2015, "scmp/2010-2015/wayback"),
        ("caixin", 2021, "caixin/2016-2026/wayback"),
    ],
)
def test_parser_source_manifest_shard(publisher, year, expected):
    assert parser_source_manifest_shard(publisher, year) == expected


@pytest.mark.parametrize(
    ("publisher", "year"),
    [("unknown", 2020), ("nyt", 2009), ("nyt", 2027)],
)
def test_parser_source_manifest_shard_rejects_unsupported_cells(
    publisher,
    year,
):
    with pytest.raises(ValueError):
        parser_source_manifest_shard(publisher, year)
