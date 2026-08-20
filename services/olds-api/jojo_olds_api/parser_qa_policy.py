from __future__ import annotations


# QA rules are versioned independently from the body parser. Changing body
# extraction rotates to a zero-overlap cohort through parser_version; changing
# only a QA rule replays the same sample against the new policy.
_QA_POLICY_REVISIONS = {
    # Archived Al Jazeera LiveBlog shells often contain only the closing
    # notice; exclude those non-recoverable dynamic packages from the article
    # cohort while retaining their raw captures and content type.
    # Short legacy Wayback teaser shells are screened from the text cohort.
    "aljazeera": 4,
    # FT Wayback captures can be subscription-only shells whose document
    # title is exactly "Subscribe to read | Financial Times". Exclude the
    # navigation/upsell chrome from the article denominator while retaining
    # the raw capture for provenance.
    "ft": 2,
    "axios": 5,
    "caixin": 1,
    # SCMP access shells, image-only slideshow packages, and archived live
    # pages whose client-rendered update stream is absent have no recoverable
    # article body; retain raw records but exclude them from article QA.
    "scmp": 5,
    # Zaobao's sitemap includes interactive packages, horse-racing result
    # desks, and legacy forum shells with no headline/body. These records are
    # useful raw captures but are not recoverable text-news articles for the
    # parser cohort.
    # A small set of Wayback packages retain only a video teaser, a shorts
    # video shell, or an empty special-report shell. Keep the raw capture,
    # but exclude it from the recoverable text-article denominator.
    "zaobao": 5,
    # Exclude legacy NYT admin-package pages, image-only editorial cartoons,
    # short live-blog shells, and Editors' Note placeholders whose archive
    # snapshot contains no recoverable article body.
    # Legacy NYT prose can contain the words "share this article" as an
    # editorial sentence; the generic interface detector now only treats an
    # exact standalone share-control block as noise.
    "nyt": 4,
    # NPR's legacy audio-only pages can retain metadata and a player while
    # exposing no recoverable article body. Keep those captures, but exclude
    # them from the text-article QA denominator.
    "npr": 1,
    # WSJ Infini-News captures can preserve a media-only "Article Not
    # Supported" shell and related subscription chrome without the article
    # body. Keep those raw records but exclude them from text-article QA.
    # WSJ article paragraphs can contain an inline, parenthesized newsletter
    # mention.  Only short standalone promo blocks count as interface noise.
    "wsj": 3,
    # Reuters press-release bodies can contain legitimate copyright language;
    # the interface-noise rule now limits legal-footer detection to short
    # standalone blocks.
    # Reuters syndicated pages can expose a standalone "Trending Stories"
    # label. The parser now removes that UI node before extraction.
    "reuters": 2,
}


def qa_policy_revision(publisher: str) -> int:
    return _QA_POLICY_REVISIONS.get(publisher, 0)
