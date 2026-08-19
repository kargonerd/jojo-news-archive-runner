from __future__ import annotations


# QA rules are versioned independently from the body parser. Changing body
# extraction rotates to a zero-overlap cohort through parser_version; changing
# only a QA rule replays the same sample against the new policy.
_QA_POLICY_REVISIONS = {
    # Archived Al Jazeera LiveBlog shells often contain only the closing
    # notice; exclude those non-recoverable dynamic packages from the article
    # cohort while retaining their raw captures and content type.
    "aljazeera": 3,
    # FT Wayback captures can be subscription-only shells whose document
    # title is exactly "Subscribe to read | Financial Times". Exclude the
    # navigation/upsell chrome from the article denominator while retaining
    # the raw capture for provenance.
    "ft": 1,
    "axios": 5,
    "caixin": 1,
    # SCMP access shells and image-only slideshow packages have no
    # recoverable article body; retain raw records but exclude them from
    # article QA denominators.
    "scmp": 3,
    # Zaobao's sitemap includes interactive packages and horse-racing result
    # desks.  These records are useful raw captures but are not recoverable
    # text-news articles for the parser cohort.
    "zaobao": 1,
    # Exclude legacy NYT admin-package pages whose archive snapshot contains
    # only a teaser and no recoverable article body.
    "nyt": 2,
    # NPR's legacy audio-only pages can retain metadata and a player while
    # exposing no recoverable article body. Keep those captures, but exclude
    # them from the text-article QA denominator.
    "npr": 1,
    # WSJ Infini-News captures can preserve a media-only "Article Not
    # Supported" shell and related subscription chrome without the article
    # body. Keep those raw records but exclude them from text-article QA.
    "wsj": 2,
}


def qa_policy_revision(publisher: str) -> int:
    return _QA_POLICY_REVISIONS.get(publisher, 0)
