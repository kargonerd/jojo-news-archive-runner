from __future__ import annotations


SUPPORTED_YEARS = range(2010, 2027)

# A source shard is only a validation candidate when the publisher existed
# and the configured public archive can plausibly contain its own articles.
_PUBLISHER_MINIMUM_YEARS = {
    "axios": 2017,
    "zaobao": 2016,
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
    if publisher == "aljazeera":
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return f"aljazeera/{window}/sitemap-wayback"
    if publisher == "zaobao":
        return "zaobao/2016-2026/sitemap-wayback"
    if publisher == "wsj":
        window = "2010-2015" if year <= 2015 else "2016-2026"
        # The URL-key shard is a compact pre-index.  The replay manifest has
        # materially broader coverage for current-era WSJ years and remains
        # the canonical source root for any newly captured raw object.
        if year >= 2016:
            return f"wsj/{window}/wayback"
        return f"wsj/{window}/wayback-urlkey"
    if publisher == "axios":
        return "axios/2017-2026/wayback-urlkey"
    if publisher in {"npr", "nikkei", "scmp", "caixin"}:
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return f"{publisher}/{window}/wayback-urlkey"
    raise ValueError(f"unsupported parser publisher: {publisher}")


def parser_supplemental_manifest_shards(
    publisher: str,
    year: int,
) -> tuple[str, ...]:
    """Return catalog-only sources merged into a validation cell."""
    # Validate the cell and keep its supported-year semantics aligned with
    # parser_source_manifest_shard before deriving supplemental paths.
    parser_source_manifest_shard(publisher, year)
    if publisher == "ap" and year <= 2015:
        return ("ap/2010-2015/legacy-archive",)
    if publisher == "reuters":
        window = (
            "2010-2015"
            if year <= 2015
            else "2016-2020"
            if year <= 2020
            else "2021-2026"
        )
        return (f"reuters/{window}/commoncrawl-prefix",)
    if publisher in {"npr", "caixin"}:
        return (f"{publisher}/{year}-{year}/commoncrawl-prefix",)
    if publisher == "axios":
        return ("axios/2017-2026/sitemap-wayback",)
    if publisher == "nikkei":
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return (f"nikkei/{window}/commoncrawl-prefix",)
    if publisher == "wsj":
        # The early URL-key catalog is thin for several years (notably
        # 2010, 2011, and 2013).  Keep Common Crawl as an independent
        # catalog-only supplement so those years can be reopened when the
        # primary Wayback shard cannot supply 800 distinct articles.
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return (f"wsj/{window}/commoncrawl-prefix",)
    if publisher == "ft":
        # FT's early Wayback windows are sparse even when the replay
        # checkpoint has a few hundred usable URLs.  Keep a publisher-level
        # Common Crawl catalog available so those years can be reopened
        # before a formal 800-row run exhausts its candidates.
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return (f"ft/{window}/commoncrawl-prefix",)
    if publisher == "aljazeera":
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return (f"aljazeera/{window}/commoncrawl-prefix",)
    if publisher == "scmp":
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return (f"scmp/{window}/commoncrawl-prefix",)
    return ()
