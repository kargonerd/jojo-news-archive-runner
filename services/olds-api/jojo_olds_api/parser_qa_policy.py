from __future__ import annotations


# QA rules are versioned independently from the body parser. Changing body
# extraction rotates to a zero-overlap cohort through parser_version; changing
# only a QA rule replays the same sample against the new policy.
_QA_POLICY_REVISIONS = {
    "axios": 1,
    "wsj": 1,
}


def qa_policy_revision(publisher: str) -> int:
    return _QA_POLICY_REVISIONS.get(publisher, 0)
