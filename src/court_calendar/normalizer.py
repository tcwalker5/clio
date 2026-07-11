"""
normalizer.py — Court calendar text parsing and purpose/department/party
normalization. Ported from calendar-check's backend/src/services/normalizer.js.

Two things changed in the port (see plan doc for rationale):
  - Attorney/staff assignment no longer comes from regex-matching a name in
    free text — the Clio calendar entry's actual calendar_owner_id/attendees
    are resolved against the staff directory (clio_users.py) instead.
  - Purpose extraction runs against a Clio calendar entry's summary/description
    fields instead of an Outlook event's subject/body — same regexes, different
    source text, since both are free-text fields staff type into.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

# Multi-word phrases checked first (must win over a shorter code match) —
# ported from calendar-check's normalizer.js `phraseRegex`.
PURPOSE_PHRASES = [
    "ORDER TO SHOW CAUSE", "TRIAL SETTING CONFERENCE", "TRIAL SETTING", "TRIAL READINESS",
    "SELF REPRESENTED", "REVIEW HEARING", "MANDATORY SETTLEMENT CONFERENCE",
    "MSC READINESS CONFERENCE", "MSC READINESS CONF", "MSC READINESS",
    "FAMILY RESOLUTION CONFERENCE", "REQUEST FOR ORDER", "CASE MANAGEMENT CONFERENCE",
    "EX PARTE HEARING", "EVIDENTIARY HEARING",
]

# Single codes — ported from normalizer.js `codeRegex`. Order matters: more
# specific codes (MSC-R, MSC-FAM) must precede their shorter prefix (MSC).
PURPOSE_CODES = [
    "FRC", "RFO", "MSC-R", "MSC-FAM", "MSC", "CSC", "TRIAL", "SRH", "DCSS", "FSD", "EPH",
    "TRC", "TSC", "OSC", "REV", "REVIEW", "CMC", "HOSC", "MED", "FCSS", "CCRC", "CCON",
    "PTR", "HRG", "S/C", "DVRO", "DV", "CONT", "CTN", "MIN", "RRC", "DR", "JUDG", "TRO",
]

_PHRASE_RE = re.compile("|".join(re.escape(p) for p in PURPOSE_PHRASES), re.IGNORECASE)
_CODE_RE = re.compile(r"\b(?:" + "|".join(re.escape(c) for c in PURPOSE_CODES) + r")\b", re.IGNORECASE)

_PARTY_BEFORE_PURPOSE_RE = re.compile(
    r"^([A-Z][A-Za-z\s,.'-]+?)\s*(?:\([^)]+\))?\s+(" + "|".join(re.escape(c) for c in PURPOSE_CODES) + r")\b",
)
_PARTY_AFTER_PURPOSE_RE = re.compile(
    r"(?:Court:|" + "|".join(re.escape(c) for c in PURPOSE_CODES) + r")\s*[-:]\s*(.+?)(?:\s*[-(]|$)",
)

_ROLE_PREFIX_RE = re.compile(r"^([RP])\)")

_COURTHOUSE_DEPT_RE = re.compile(r"^([A-Z]+)-?(\d+)$")


def normalize_dept(dept: str | None) -> str:
    """'N17' / 'n-17' / 'E-8' -> 'N-17' / 'E-08'. '704' (Central) stays as-is."""
    if not dept:
        return ""
    cleaned = dept.strip().upper()
    m = _COURTHOUSE_DEPT_RE.match(cleaned)
    if m:
        letters, digits = m.groups()
        padded = digits.zfill(2) if len(digits) == 1 else digits
        return f"{letters}-{padded}"
    if cleaned.isdigit():
        return cleaned
    return cleaned


def normalize_purpose(raw: str, mappings: dict[str, str]) -> str:
    """
    raw_pattern -> canonical_code lookup, mirroring normalizer.js normalizePurpose():
    exact match, then prefix match (truncated court-text forms), then reverse-prefix
    match, then fall back to the raw text if it's already a known code, else itself.
    `mappings` is {raw_pattern: canonical_code}, e.g. from purpose_mappings table.
    """
    if not raw:
        return ""
    upper = raw.strip().upper()

    if upper in mappings:
        return mappings[upper]
    for pattern, code in mappings.items():
        if upper.startswith(pattern):
            return code
    for pattern, code in mappings.items():
        if pattern.startswith(upper):
            return code
    if upper in mappings.values():
        return upper
    return raw.strip()


def initials(full_name: str | None) -> str:
    """'Dahann Bowers' -> 'DB'. Used for compact Staff/Attorney columns."""
    if not full_name:
        return ""
    return "".join(word[0].upper() for word in full_name.split() if word)


_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV"}


def judge_last_name(judge: str | None) -> str:
    """'CHARLES E. BELL JR' -> 'BELL'. Display-only; the full name stays in storage."""
    if not judge:
        return ""
    parts = judge.strip().split()
    while parts and parts[-1].rstrip(".").upper() in _NAME_SUFFIXES:
        parts.pop()
    return parts[-1] if parts else ""


def normalize_party_name(name: str | None) -> str:
    """Trim/uppercase, strip a leading R)/P) role marker, drop anything after a comma."""
    if not name:
        return ""
    cleaned = _ROLE_PREFIX_RE.sub("", name.strip()).strip().upper()
    return cleaned.split(",")[0].strip()


def party_names_match(a: str, b: str) -> bool:
    """
    Partial/substring match in both directions (handles hyphenated names like
    ORTEGA-GUACHENA matching ORTEGA) — ported from matcher.js's inline check.
    """
    if not a or not b:
        return False
    return a == b or a in b or b in a


def extract_all_purposes(text: str | None) -> list[str]:
    """
    Every purpose phrase/code found in free text (a Clio calendar entry's
    summary/description). Phrases are checked first so e.g. "TRIAL SETTING
    CONFERENCE" wins over a bare "TRIAL" match.
    """
    if not text:
        return []
    found = [m.group(0).upper() for m in _PHRASE_RE.finditer(text)]
    found += [m.group(0).upper() for m in _CODE_RE.finditer(text)]
    return found


_DEPT_IN_TEXT_RE = re.compile(r"\b([NESC])[-\s]?(\d{1,2})\b|\b(7\d{2})\b")


def extract_dept_from_text(text: str | None) -> str | None:
    """
    Best-effort department extraction from free text (e.g. a Clio calendar
    entry's summary mentioning "Dept/ E-7"). Returns None when the text
    doesn't clearly mention one — callers should treat that as "unknown",
    not "different", when comparing against a court event's department.
    """
    if not text:
        return None
    m = _DEPT_IN_TEXT_RE.search(text.upper())
    if not m:
        return None
    if m.group(3):
        return m.group(3)
    return normalize_dept(f"{m.group(1)}-{m.group(2)}")


def extract_party_name(text: str | None) -> str | None:
    """Best-effort party name extraction from free text, before/after the purpose code."""
    if not text:
        return None
    m = _PARTY_BEFORE_PURPOSE_RE.match(text.strip())
    if m:
        return m.group(1).strip().rstrip(",;- ").strip()
    m = _PARTY_AFTER_PURPOSE_RE.search(text)
    if m:
        return m.group(1).strip().rstrip(",;- ").strip()
    return None


@dataclass
class CourtEvent:
    date: str            # YYYY-MM-DD
    start_time: str       # HH:MM (24h)
    dt: datetime          # combined date+time, Pacific tz-aware
    dept_raw: str
    dept: str
    judge: str
    purpose_raw: str
    case_number: str
    party: str
    party_role: str | None  # 'P', 'R', or None
    attorney: str
    raw_line: str


_LINE_HAS_DATETIME_RE = re.compile(r"^\d{2}/\d{2}/\d{2}\s+\d{1,2}:\d{2}(AM|PM)", re.IGNORECASE)


def _convert_to_24h(time_str: str) -> str:
    return datetime.strptime(time_str.upper(), "%I:%M%p").strftime("%H:%M")


def parse_court_calendar_line(line: str) -> CourtEvent | None:
    """
    Fixed-width SD Superior Court calendar line parser — ported from
    normalizer.js parseCourtCalendarLine(). Column positions:
      0-9 date | 9-17 time | 17-24 dept | 24-28 "DM" | 28-58 judge |
      58-74 purpose | 74-104 case # | 105-133 party (role-prefixed) | 133+ attorney
    """
    if not _LINE_HAS_DATETIME_RE.match(line):
        return None

    padded = line.ljust(180)
    date_str = padded[0:9].strip()
    time_str = padded[9:17].strip()
    dept_raw = padded[17:24].strip()
    judge = padded[28:58].strip()
    purpose_raw = padded[58:74].strip()
    case_number = padded[74:104].strip()
    party = padded[105:133].strip()
    attorney = padded[133:].strip()

    role_match = _ROLE_PREFIX_RE.match(party)
    party_role = role_match.group(1) if role_match else None
    party_clean = _ROLE_PREFIX_RE.sub("", party).strip()

    month, day, year = date_str.split("/")
    full_year = f"20{year}"
    time_24 = _convert_to_24h(time_str)
    dt = datetime(
        int(full_year), int(month), int(day),
        int(time_24[:2]), int(time_24[3:]),
        tzinfo=PACIFIC,
    )

    return CourtEvent(
        date=f"{full_year}-{month.zfill(2)}-{day.zfill(2)}",
        start_time=time_24,
        dt=dt,
        dept_raw=dept_raw,
        dept=normalize_dept(dept_raw),
        judge=judge,
        purpose_raw=purpose_raw,
        case_number=case_number,
        party=party_clean,
        party_role=party_role,
        attorney=attorney,
        raw_line=line.strip(),
    )
