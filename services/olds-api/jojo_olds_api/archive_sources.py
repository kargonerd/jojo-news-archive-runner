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
    alternate_hosts: tuple[str, ...] = ()
    preserve_normalized_hosts: tuple[str, ...] = ()

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
_NON_ARTICLE_FILE_SUFFIX_RE = re.compile(
    r"\.(?:avif|bmp|css|gif|ico|jpe?g|js|mjs|pdf|png|svg|webp)$",
    re.IGNORECASE,
)


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
            r"^/articles/[^/]*-crossword(?:-|$)",
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
            r"^/article/(?:comments|slideshow)(?:/|$)",
            r"%3c|%3e",
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
    "axios": ArchiveSourceSpec(
        publisher="axios",
        canonical_host="www.axios.com",
        wayback_patterns=(
            "axios.com/{year}/*",
            "www.axios.com/{year}/*",
            "axios.com/*/{year}/*",
            "www.axios.com/*/{year}/*",
        ),
        accepted_path_patterns=_patterns(r"^/(?:[^/]+/)?20\d{2}/"),
        rejected_path_patterns=_patterns(r"^/(?:newsletters?|signup|about)(?:/|$)"),
    ),
    "npr": ArchiveSourceSpec(
        publisher="npr",
        canonical_host="www.npr.org",
        wayback_patterns=("www.npr.org/{year}/*",),
        accepted_path_patterns=_patterns(r"^/20\d{2}/"),
        rejected_path_patterns=_patterns(
            r"^/(?:programs|podcasts?|music)(?:/|$)",
            r"/(?:election-\d{4}-.+-results|excerpt-[a-z0-9-]+|nprs?-toy-stories|makeover-photos)(?:/|$)",
        ),
    ),
    "nikkei": ArchiveSourceSpec(
        publisher="nikkei",
        canonical_host="www.nikkei.com",
        wayback_patterns=("www.nikkei.com/article/*",),
        accepted_path_patterns=_patterns(r"^/article/"),
    ),
    "zaobao": ArchiveSourceSpec(
        publisher="zaobao",
        canonical_host="www.zaobao.com.sg",
        wayback_patterns=("www.zaobao.com.sg/*/story*",),
        accepted_path_patterns=_patterns(r"^/[a-z-]+/[a-z-]+/story\d+"),
        rejected_path_patterns=_patterns(r"^/(?:zvideos|podcast|special)(?:/|$)"),
    ),
    "aljazeera": ArchiveSourceSpec(
        publisher="aljazeera",
        canonical_host="www.aljazeera.com",
        wayback_patterns=("www.aljazeera.com/{year}/*",),
        accepted_path_patterns=_patterns(r"^/(?:news|features|opinions)/20\d{2}/"),
        rejected_path_patterns=_patterns(r"^/(?:video|program|podcasts?)(?:/|$)"),
    ),
    "scmp": ArchiveSourceSpec(
        publisher="scmp",
        canonical_host="www.scmp.com",
        wayback_patterns=("www.scmp.com/article/*", "www.scmp.com/*/article/*"),
        accepted_path_patterns=_patterns(r"^/article/\d+", r"^/.+/article/\d+"),
        rejected_path_patterns=_patterns(r"^/(?:video|magazines)(?:/|$)"),
    ),
    "caixin": ArchiveSourceSpec(
        publisher="caixin",
        canonical_host="www.caixin.com",
        wayback_patterns=("www.caixin.com/*", "magazine.caixin.com/{year}/*"),
        accepted_path_patterns=_patterns(r"^/20\d{2}(?:[-/]|$)"),
        alternate_hosts=("magazine.caixin.com",),
        preserve_normalized_hosts=("magazine.caixin.com",),
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
    allowed_hosts.update(spec.alternate_hosts)
    if spec.publisher == "wsj":
        allowed_hosts.add("online.wsj.com")
    if hostname not in allowed_hosts:
        return None
    path = re.sub(r"/+", "/", parsed.path or "/")
    if spec.publisher == "reuters" and re.search(
        r"[|<>(){}]|%(?:28|29|3c|3e|7b|7c|7d)",
        path,
        re.IGNORECASE,
    ):
        return None
    if _NON_ARTICLE_FILE_SUFFIX_RE.search(path):
        return None
    if any(pattern.search(path) for pattern in spec.rejected_path_patterns):
        return None
    if not any(pattern.search(path) for pattern in spec.accepted_path_patterns):
        return None
    if path != "/":
        path = path.rstrip("/")
    normalized_host = (
        hostname
        if hostname in spec.preserve_normalized_hosts
        else spec.canonical_host
    )
    return urlunsplit(("https", normalized_host, path, "", ""))


def article_url_publication_year(
    spec: ArchiveSourceSpec,
    value: str,
) -> int | None:
    normalized = normalize_article_url(spec, value)
    if normalized is None or spec.publisher != "reuters":
        return None
    path = urlsplit(normalized).path
    if not path.startswith("/article/"):
        return None
    matches = re.findall(
        r"((?:19|20)\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])",
        path,
    )
    return int(matches[-1]) if matches else None
