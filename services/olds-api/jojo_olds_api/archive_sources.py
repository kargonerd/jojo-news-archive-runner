from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class ArchiveSourceSpec:
    publisher: str
    canonical_host: str
    wayback_patterns: tuple[str, ...]
    accepted_path_patterns: tuple[re.Pattern[str], ...]
    rejected_path_patterns: tuple[re.Pattern[str], ...] = ()

    def expanded_wayback_patterns(
        self,
        *,
        from_year: int,
        to_year: int,
    ) -> tuple[str, ...]:
        result: list[str] = []
        for pattern in self.wayback_patterns:
            if "{year}" in pattern:
                result.extend(
                    pattern.format(year=year)
                    for year in range(from_year, to_year + 1)
                )
            else:
                result.append(pattern)
        return tuple(result)


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


_SLUG_PREFIXES = tuple("abcdefghijklmnopqrstuvwxyz0123456789")


ARCHIVE_SOURCE_SPECS = {
    "ap": ArchiveSourceSpec(
        publisher="ap",
        canonical_host="apnews.com",
        wayback_patterns=(
            "apnews.com/article/*",
            "apnews.com/*",
        ),
        accepted_path_patterns=_patterns(
            r"^/article/",
            r"^/[a-f0-9]{24,}$",
            r"^/.+-[a-f0-9]{24,}$",
        ),
        rejected_path_patterns=_patterns(
            r"^/(?:hub|video|videos|search|press-releases|newsletters)(?:/|$)",
        ),
    ),
    "wsj": ArchiveSourceSpec(
        publisher="wsj",
        canonical_host="www.wsj.com",
        wayback_patterns=tuple(
            f"www.wsj.com/articles/{prefix}*"
            for prefix in _SLUG_PREFIXES
        )
        + (
            "online.wsj.com/article/*",
        ),
        accepted_path_patterns=_patterns(
            r"^/articles/",
            r"^/article/",
            r"^/news/.+",
            r"^/(?:[a-z0-9-]+/)+[a-z0-9-]+-[0-9a-f]{8}$",
        ),
        rejected_path_patterns=_patterns(
            r"/(?:video|podcasts?|newsletters?|livecoverage)(?:/|$)",
        ),
    ),
    "bloomberg": ArchiveSourceSpec(
        publisher="bloomberg",
        canonical_host="www.bloomberg.com",
        wayback_patterns=(
            "www.bloomberg.com/news/articles/*",
            "www.bloomberg.com/opinion/articles/*",
            "www.bloomberg.com/features/*",
        ),
        accepted_path_patterns=_patterns(
            r"^/news/articles/",
            r"^/opinion/articles/",
            r"^/features/",
        ),
    ),
    "nyt": ArchiveSourceSpec(
        publisher="nyt",
        canonical_host="www.nytimes.com",
        wayback_patterns=(
            "www.nytimes.com/{year}/*",
            "nytimes.com/{year}/*",
        ),
        accepted_path_patterns=_patterns(
            r"^/20\d{2}/\d{2}/\d{2}/",
            r"^/interactive/20\d{2}/",
        ),
        rejected_path_patterns=_patterns(
            r"/(?:video|podcasts?|crosswords?|games|wirecutter)(?:/|$)",
        ),
    ),
    "reuters": ArchiveSourceSpec(
        publisher="reuters",
        canonical_host="www.reuters.com",
        wayback_patterns=tuple(
            f"www.reuters.com/article/{prefix}*"
            for prefix in _SLUG_PREFIXES
        ),
        accepted_path_patterns=_patterns(
            r"^/article/",
            (
                r"^/(?:world|business|markets|technology|legal|sports|"
                r"lifestyle|science|fact-check|breakingviews|"
                r"investigates)/.+"
            ),
        ),
        rejected_path_patterns=_patterns(
            r"/(?:video|pictures|graphics)(?:/|$)",
        ),
    ),
    "ft": ArchiveSourceSpec(
        publisher="ft",
        canonical_host="www.ft.com",
        wayback_patterns=(
            "www.ft.com/content/*",
            "ft.com/content/*",
        ),
        accepted_path_patterns=_patterns(
            r"^/content/[0-9a-f-]{20,}$",
        ),
    ),
}


def archive_source_spec(publisher: str) -> ArchiveSourceSpec:
    try:
        return ARCHIVE_SOURCE_SPECS[publisher]
    except KeyError as exc:
        supported = ", ".join(sorted(ARCHIVE_SOURCE_SPECS))
        raise ValueError(
            f"unsupported publisher {publisher!r}; expected one of: {supported}"
        ) from exc


def normalize_article_url(
    spec: ArchiveSourceSpec,
    value: str,
) -> str | None:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    hostname = parsed.hostname.casefold()
    allowed_hosts = {
        spec.canonical_host,
        spec.canonical_host.removeprefix("www."),
        f"www.{spec.canonical_host.removeprefix('www.')}",
    }
    if spec.publisher == "wsj":
        allowed_hosts.add("online.wsj.com")
    if hostname not in allowed_hosts:
        return None
    path = re.sub(r"/+", "/", parsed.path or "/")
    if any(pattern.search(path) for pattern in spec.rejected_path_patterns):
        return None
    if not any(pattern.search(path) for pattern in spec.accepted_path_patterns):
        return None
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(("https", spec.canonical_host, path, "", ""))
