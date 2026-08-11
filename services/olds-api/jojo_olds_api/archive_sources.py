from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit


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
        wayback_patterns=tuple(
            f"hosted.ap.org/dynamic/stories/{prefix.upper()}/*"
            for prefix in _SLUG_PREFIXES
        ) + (
            "apnews.com/article/*",
            "apnews.com/*",
        ),
        accepted_path_patterns=_patterns(
            r"^/article/",
            r"^/[a-f0-9]{24,}$",
            r"^/.+-[a-f0-9]{24,}$",
            r"^/dynamic/stories/[a-z0-9]/[a-z0-9_-]+$",
        ),
        rejected_path_patterns=_patterns(
            r"^/(?:hub|video|videos|search|press-releases|newsletters)(?:/|$)",
        ),
        alternate_hosts=(
            "hosted.ap.org",
            "hosted2.ap.org",
            "bigstory.ap.org",
        ),
        preserve_normalized_hosts=("bigstory.ap.org",),
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
        # Internet Archive indexes the bare and www hosts as distinct URL
        # keys.  Normalize both to www below, but query both so articles that
        # were only captured under npr.org are not omitted from discovery.
        wayback_patterns=(
            "www.npr.org/{year}/*",
            "npr.org/{year}/*",
        ),
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
        # URL-key CDX pages also contain every intermediate prefix and static
        # asset requested below an article URL. A real Nikkei article uses a
        # long alphanumeric story id; accepting the directory alone inflated
        # the catalog with `/article/D`, JavaScript files, and similar keys.
        accepted_path_patterns=_patterns(
            r"^/article/(?:[A-Z]{8}\d{5}|[A-Z]{6}\d{7}|"
            r"[A-Z0-9_]{15,})/?$",
        ),
    ),
    "zaobao": ArchiveSourceSpec(
        publisher="zaobao",
        canonical_host="www.zaobao.com.sg",
        # CDX treats the URL argument as a prefix; an infix wildcard such as
        # `* /story*` is not a recursive path glob and returns no captures.
        # Historical articles are published below /news/<section>/storyYYYY….
        wayback_patterns=("www.zaobao.com.sg/news/*",),
        # Official monthly sitemaps include realtime, news, lifestyle and
        # nested special-report desks. Their shared invariant is a dated
        # story id, not a fixed section depth.
        accepted_path_patterns=_patterns(
            r"^/(?:[a-z0-9-]+/)+story20\d{6}-\d+$"
        ),
        rejected_path_patterns=_patterns(r"^/(?:zvideos|podcast)(?:/|$)"),
    ),
    "aljazeera": ArchiveSourceSpec(
        publisher="aljazeera",
        canonical_host="www.aljazeera.com",
        wayback_patterns=(
            "www.aljazeera.com/news/{year}/*",
            "www.aljazeera.com/features/{year}/*",
            "www.aljazeera.com/opinions/{year}/*",
        ),
        # The official article sitemap contains many editorial desks beyond
        # news/features/opinions (for example economy, sports and
        # investigations). A dated one- or two-level section path plus a
        # non-empty slug is the stable canonical article shape.
        accepted_path_patterns=_patterns(
            r"^/(?:[a-z0-9-]+/){1,2}20\d{2}/"
            r"\d{1,2}/\d{1,2}/[^/]+$",
            # Al Jazeera's pre-migration CMS used compact numeric story ids
            # below a year/month path, for example
            # `/news/2010/02/2010212134228827506.html`.  The id starts with
            # the publication year but has no separate day path component.
            # Requiring the numeric year prefix keeps malformed nested URL
            # keys and ordinary HTML assets outside the article catalog.
            r"^/(?:[a-z0-9-]+/){1,2}20\d{2}/\d{2}/"
            r"20\d{6,}\.html$",
        ),
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
    if (
        spec.publisher == "nikkei"
        and path.startswith("/article/article/")
    ):
        # Three legacy CDX keys repeat the article directory but otherwise
        # contain a valid Nikkei story id. Normalize them instead of either
        # losing the article or preserving a non-canonical duplicate.
        path = "/article/" + path.removeprefix("/article/article/")
    if spec.publisher == "npr":
        # CDX indexes occasionally contain scraper-added line endings or a
        # trailing assignment marker. Neither can be part of NPR's article
        # slug, and leaving them in place prevents timemap fallback from
        # finding captures for the real canonical URL.
        path = re.sub(
            r"(?i)(?:%(?:0[0-9a-f]|7f))+$",
            "",
            path,
        ).rstrip("=")
    if spec.publisher == "caixin":
        # Legacy magazine articles split long stories into numbered pages and
        # expose an ``_all`` full-text view. They are representations of one
        # article, not independent stories.
        path = re.sub(
            r"_(?:all|\d+)(\.html)$",
            r"\1",
            path,
            flags=re.IGNORECASE,
        )
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
    if spec.publisher == "ap" and hostname in {
        "hosted.ap.org",
        "hosted2.ap.org",
    }:
        published = ap_hosted_publication_datetime(value)
        if published is None:
            return None
        return urlunsplit(
            (
                "https",
                "hosted.ap.org",
                path,
                "CTIME=" + published.strftime("%Y-%m-%d-%H-%M-%S"),
                "",
            )
        )
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
    if normalized is None:
        return None
    path = urlsplit(normalized).path
    if spec.publisher == "wsj":
        published = wsj_article_publication_datetime(normalized)
        return published.year if published is not None else None
    if spec.publisher == "ap":
        published = ap_hosted_publication_datetime(normalized)
        return published.year if published is not None else None
    if spec.publisher == "nikkei":
        # Legacy Nikkei article IDs encode the publication year and month in
        # segments such as R10C13A9 (2013-09) or Z20C11A4 (2011-04).
        # Wayback may replay a later generic/member page whose visible date is
        # the capture year, so use this stable identifier to reject misplaced
        # validation rows. Newer opaque IDs intentionally return no year.
        match = re.search(
            r"[A-Z]\d{2}C(\d{2})A(?:1[0-2]|[1-9])",
            path,
            flags=re.IGNORECASE,
        )
        return 2000 + int(match.group(1)) if match is not None else None
    if spec.publisher != "reuters":
        return None
    if not path.startswith("/article/"):
        return None
    matches = re.findall(
        r"((?:19|20)\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])",
        path,
    )
    return int(matches[-1]) if matches else None


def ap_hosted_publication_datetime(value: str) -> datetime | None:
    """Parse the story-revision timestamp used by legacy Hosted AP URLs."""
    parsed = urlsplit(value.strip())
    if (parsed.hostname or "").casefold() not in {
        "hosted.ap.org",
        "hosted2.ap.org",
    }:
        return None
    ctime = next(
        (
            query_value
            for key, query_value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if key.casefold() == "ctime"
        ),
        "",
    )
    match = re.fullmatch(
        r"((?:19|20)\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})",
        ctime,
    )
    if match is None:
        return None
    try:
        return datetime(
            *(int(value) for value in match.groups()),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def wsj_article_publication_datetime(value: str) -> datetime | None:
    normalized = normalize_article_url(archive_source_spec("wsj"), value)
    if normalized is None:
        return None
    match = re.search(
        r"/articles/[^/?#]+-(\d{10,12})$",
        urlsplit(normalized).path,
    )
    if match is None:
        return None
    raw_identifier = match.group(1)
    if len(raw_identifier) == 10:
        raw_epoch = raw_identifier
    elif len(raw_identifier) == 11 and raw_identifier.startswith("1"):
        raw_epoch = raw_identifier[1:]
    elif len(raw_identifier) == 12:
        raw_epoch = raw_identifier[2:]
    else:
        return None
    published = datetime.fromtimestamp(int(raw_epoch), tz=timezone.utc)
    if not 2008 <= published.year <= 2038:
        return None
    return published.replace(hour=0, minute=0, second=0, microsecond=0)
