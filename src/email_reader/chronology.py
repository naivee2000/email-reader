"""Check explicit between-message ordering against retrieved source metadata.

This is a narrow factual guard, not a general answer-quality verifier. Dates
inside messages/documents are deliberately not treated as sent timestamps.
"""
from __future__ import annotations

import re
from datetime import datetime

from email_reader.contracts import Answer
from email_reader.search import SearchHit

_ORDERING = re.compile(
    r"\b(?:earlier|later|subsequent|previous|latest|newest|oldest)\b"
    r"[^.!?\n]{0,60}\b(?:emails?|messages?|replies|reply)\b"
    r"|(?:较早|较晚|更早|更晚|后续|随后|最新|最早)[^。！？\n]{0,25}(?:邮件|回信)",
    re.IGNORECASE,
)


def message_order_errors(answer: Answer, hits: list[SearchHit]) -> list[str]:
    """Return private correction feedback; callers must never log this content."""
    sources = {hit.chunk_id: hit for hit in hits}
    errors: list[str] = []
    covered: list[tuple[int, int, str]] = []
    for claim in answer.message_order_claims:
        start = answer.text.find(claim.span) if claim.span.strip() else -1
        if start < 0:
            errors.append("An ordering claim's span is missing from the answer.")
            continue
        # Cover all repeated exact spans rather than trusting just the first.
        covered.extend((match.start(), match.end(), claim.relation) for match in
                       re.finditer(re.escape(claim.span), answer.text))
        pair = [sources.get(claim.source_id), sources.get(claim.reference_source_id)]
        if any(hit is None or hit.citation.source_type != "message" for hit in pair):
            errors.append(f"Ordering span {claim.span!r} must reference two retrieved message sources.")
            continue
        if any(identifier not in answer.source_ids for identifier in
               (claim.source_id, claim.reference_source_id)):
            errors.append(f"Ordering span {claim.span!r} must cite both message sources.")
            continue
        try:
            dates = [datetime.fromisoformat(hit.citation.sent_at) for hit in pair]
        except (TypeError, ValueError):
            dates = []
        if len(dates) != 2 or any(date.utcoffset() is None for date in dates):
            errors.append(f"Ordering span {claim.span!r} has missing or timezone-ambiguous timestamps; omit it.")
            continue
        correct = dates[0] < dates[1] if claim.relation == "earlier" else dates[0] > dates[1]
        if not correct:
            errors.append(
                f"Ordering span {claim.span!r} conflicts with sent_at: "
                f"{claim.source_id}={pair[0].citation.sent_at}; "
                f"{claim.reference_source_id}={pair[1].citation.sent_at}."
            )
    for match in _ORDERING.finditer(answer.text):
        relations = {relation for start, end, relation in covered
                     if start <= match.start() and end >= match.end()}
        expected = "earlier" if re.match(r"earlier|previous|较早|更早", match.group(), re.IGNORECASE) else "later"
        if not relations:
            errors.append(f"Unverified message-ordering phrase {match.group()!r}; provide a claim or omit ordering.")
        elif re.search(r"\b(?:latest|newest|oldest)\b|最新|最早", match.group(), re.IGNORECASE):
            errors.append("A pairwise claim cannot verify a superlative across all messages; use explicit dates instead.")
        elif relations != {expected}:
            errors.append(f"Ordering phrase {match.group()!r} does not agree with its structured relation.")
    return errors
