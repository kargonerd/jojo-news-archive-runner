from __future__ import annotations


SUPPORTED_YEARS = range(2010, 2027)

# A source shard is only a validation candidate when the publisher existed
# and the configured public archive can plausibly contain its own articles.
_PUBLISHER_MINIMUM_YEARS = {
    "axios": 2017,
}


def parser_source_manifest_shard(publisher: str, year: int) -> str:
    if year not in SUPPORTED_YEARS:
        raise ValueError("parser validation year must be between 2010 and 2026")
    minimum_year = _PUBLISHER_MINIMUM_YEARS.get(publisher)
    if minimum_year is not None and year < minimum_year:
        raise ValueError(
            f"{publisher} validation is unavailable before {minimum_year}"
        )
    if publisher == "reuters":
        if year <= 2015:
            return "reuters/2010-2015/wayback-urlkey"
        if year <= 2020:
            return "reuters/2016-2020/wayback-urlkey"
        return "reuters/2021-2026/reuters-sitemap-wayback"
    if publisher in {"ap", "bloomberg", "ft", "nyt"}:
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return f"{publisher}/{window}/sitemap-wayback"
    if publisher == "wsj":
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return f"wsj/{window}/wayback-urlkey"
    if publisher == "axios":
        return "axios/2017-2026/wayback-urlkey"
    if publisher in {"npr", "nikkei", "zaobao", "aljazeera", "scmp", "caixin"}:
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return f"{publisher}/{window}/wayback-urlkey"
    raise ValueError(f"unsupported parser publisher: {publisher}")
