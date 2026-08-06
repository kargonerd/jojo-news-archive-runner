from __future__ import annotations


SUPPORTED_YEARS = range(2010, 2027)


def parser_source_manifest_shard(publisher: str, year: int) -> str:
    if year not in SUPPORTED_YEARS:
        raise ValueError("parser validation year must be between 2010 and 2026")
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
    if publisher in {"axios", "npr", "nikkei", "zaobao", "aljazeera", "scmp", "caixin"}:
        window = "2010-2015" if year <= 2015 else "2016-2026"
        return f"{publisher}/{window}/wayback-urlkey"
    raise ValueError(f"unsupported parser publisher: {publisher}")
