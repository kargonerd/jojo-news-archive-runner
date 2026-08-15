from __future__ import annotations


# QA rules are versioned independently from the body parser. Changing body
# extraction rotates to a zero-overlap cohort through parser_version; changing
# only a QA rule replays the same sample against the new policy.
_QA_POLICY_REVISIONS = {
    # Archived Al Jazeera LiveBlog shells often contain only the closing
    # notice; exclude those non-recoverable dynamic packages from the article
    # cohort while retaining their raw captures and content type.
    "aljazeera": 1,
    "axios": 4,
    "caixin": 1,
    # Exclude legacy NYT admin-package pages whose archive snapshot contains
    # only a teaser and no recoverable article body.
    "nyt": 2,
    "wsj": 1,
}


def qa_policy_revision(publisher: str) -> int:
    return _QA_POLICY_REVISIONS.get(publisher, 0)
