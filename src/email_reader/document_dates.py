"""Conservative, local hints from explicitly labelled document dates.

These are printed values, not assertions of current coverage or validity.
Never infer dates from filenames, email timestamps, or unlabelled numbers.
"""

from __future__ import annotations

import re
from datetime import datetime

_DATE = re.compile(
    r"(?:[A-Za-z]+\s+\d{1,2},?\s+\d{4}|"
    r"\d{4}年\d{1,2}月\d{1,2}日|"
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4})"
)
_LABEL = re.compile(
    r"^(?P<label>effective\s+date|expiration\s+date|expiry\s+date|"
    r"issue\s+date|date\s+issued|生效日期|到期日期|签发日期)\s*[:：]?\s*",
    re.IGNORECASE,
)
_NAMES = {
    "effective date": "Effective date",
    "expiration date": "Expiration date",
    "expiry date": "Expiration date",
    "issue date": "Issue date",
    "date issued": "Issue date",
    "生效日期": "Effective date",
    "到期日期": "Expiration date",
    "签发日期": "Issue date",
}


def _valid_date(value: str) -> bool:
    # Numeric locale-ambiguous dates stay verbatim; accept only if at least
    # one common reading is a real calendar date. Do not choose a reading.
    formats = (
        "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
        "%Y年%m月%d日", "%Y-%m-%d", "%m-%d-%Y", "%d-%m-%Y",
        "%m/%d/%Y", "%d/%m/%Y",
    )
    for date_format in formats:
        try:
            datetime.strptime(value, date_format)
            return True
        except ValueError:
            pass
    return False


def _standalone_pairs(lines: list[str]) -> dict[int, int]:
    """Infer one consistent direction only within closed alternating fields.

    PDF extraction can put VALUES before LABELS. A sequence date/label/date/
    label is backward; label/date/label/date is forward. An extra date at either
    boundary makes the sequence ambiguous, so withhold rather than shift fields.
    """
    types = []
    for line in lines:
        text = line.strip()
        label = _LABEL.match(text)
        types.append("D" if _DATE.fullmatch(text) else (
            "L" if label and not text[label.end():].strip() else ""
        ))
    pairs = {}
    index = 0
    while index < len(lines):
        end = index
        while end < len(lines) and types[end]:
            end += 1
        run = types[index:end]
        if (
            len(run) >= 2 and len(run) % 2 == 0
            and all(run[i] != run[i + 1] for i in range(len(run) - 1))
            and (run[0] == "L" or len(run) >= 4)
        ):
            direction = 1 if run[0] == "L" else -1
            for position in range(index, end):
                if types[position] == "L":
                    pairs[position] = position + direction
        index = end + 1
    return pairs


def printed_document_dates(chunks: list[dict]) -> list[dict]:
    """Return non-conflicting label/value hints, each with exact provenance.

    Recognize same-line or consistently ordered standalone date/label fields. Some
    extracted PDF footers put the effective date immediately BEFORE its label
    and the application timestamp AFTER it; only recognize that specific
    timestamp-shaped layout rather than treating every preceding number as a
    labelled value. Page/chunk boundaries are never crossed.
    """
    found: dict[str, dict[str, dict]] = {}
    for chunk in chunks:
        lines = chunk["content"].splitlines(keepends=True)
        pairs = _standalone_pairs(lines)
        offset = 0
        for index, line in enumerate(lines):
            stripped = line.strip()
            label = _LABEL.match(stripped)
            # Value-before-label on one extracted line is explicit, not the
            # unsafe nearest-date guess used for unknown multi-column layouts.
            leading_date = _DATE.match(stripped)
            reverse_label = _LABEL.match(stripped[leading_date.end():].lstrip()) if leading_date else None
            if not label and reverse_label:
                tail = stripped[leading_date.end():].lstrip()[reverse_label.end():].strip()
                if not tail or re.fullmatch(
                    r",?\s*\d{2}-\d{2}-\d{4}/\d{1,2}:\d{2}(?:\s+[AP]M)?\s+[A-Z]+\s*Application date/time:\s*",
                    tail, re.IGNORECASE,
                ):
                    label = reverse_label
            if not label:
                offset += len(line)
                continue
            kind = _NAMES[" ".join(label["label"].lower().split())]
            suffix = stripped[label.end():].strip() if not reverse_label else ""
            value = ""
            span_start, span_end = offset, offset + len(line)
            if reverse_label:
                value = leading_date.group()
            elif _DATE.fullmatch(suffix):
                value = suffix
            elif not suffix and index in pairs:
                date_index = pairs[index]
                value = lines[date_index].strip()
                if date_index > index:
                    span_end += len(lines[date_index])
                else:
                    span_start -= len(lines[date_index])
            elif (
                kind == "Effective date" and index > 0
                and re.fullmatch(r",?\s*\d{2}-\d{2}-\d{4}/\d{1,2}:\d{2}(?:\s.*)?", suffix)
                and index + 1 < len(lines)
                and lines[index + 1].strip().lower().startswith("application date/time")
            ):
                candidate = lines[index - 1].strip()
                if _DATE.fullmatch(candidate):
                    value = candidate
                    span_start -= len(lines[index - 1])
                    span_end += len(lines[index + 1])
            if value and _valid_date(value):
                key = " ".join(value.lower().split()).replace(",", "")
                found.setdefault(kind, {}).setdefault(key, {
                    "label": kind,
                    "value": value,
                    "source_span": chunk["content"][span_start:span_end].strip(),
                    "source_id": chunk["id"],
                    "locator": chunk.get("locator", ""),
                    "app_link": "emailreader://source/" + chunk["id"],
                })
            offset += len(line)
    # Multiple conflicting printed values are not collapsed to a guessed date.
    return [
        next(iter(values.values())) for values in found.values() if len(values) == 1
    ]
