"""
court_fetch.py — One-click fetch of the SD Superior Court calendar, filtered
to one attorney. Ported from calendar-check's backend/src/routes/fetch.js +
its isTargetAttorney() filter (clientCaseMapper.js).
"""

import html
import re

import requests

COURT_URL_TEMPLATE = "https://www.sandiego.courts.ca.gov/scripts/seekcalendar.pl?z=portal&g=&c=&j=&p=&a={query}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_TAG_STRIP_STEPS = [
    (re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL), ""),
    (re.compile(r"<style[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL), ""),
    (re.compile(r"<br\s*/?>", re.IGNORECASE), "\n"),
    (re.compile(r"</tr>", re.IGNORECASE), "\n"),
    (re.compile(r"</td>", re.IGNORECASE), " "),
    (re.compile(r"</th>", re.IGNORECASE), " "),
    (re.compile(r"<[^>]+>"), ""),
]


def _extract_calendar_text(page_html: str) -> str:
    text = html.unescape(page_html)
    for pattern, replacement in _TAG_STRIP_STEPS:
        text = pattern.sub(replacement, text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_attorney_name(name: str) -> str:
    """Uppercase and drop middle initials, e.g. 'Heidi D. Collier, ESQ' -> 'HEIDI COLLIER, ESQ'."""
    if not name:
        return ""
    upper = name.upper()
    upper = re.sub(r"\s+[A-Z]\.\s+", " ", upper)
    return upper


def is_target_attorney(event_attorney: str, target_full_name: str) -> bool:
    """AND-logic word match: 'HEIDI COLLIER' matches 'HEIDI D. COLLIER, ESQ' but not just 'COLLIER'."""
    if not event_attorney or not target_full_name:
        return False
    normalized = _normalize_attorney_name(event_attorney)
    target_words = target_full_name.upper().strip().split()
    return all(word in normalized for word in target_words)


def fetch_court_calendar_text(last_name: str, target_full_name: str) -> tuple[str, dict]:
    """
    Fetches the court calendar search results for `last_name`, filters lines
    to those whose attorney field matches `target_full_name`, and returns
    (filtered_calendar_text, stats). Import this text the same way as
    pasted text via court_calendar.normalizer.parse_court_calendar_line().
    """
    from court_calendar.normalizer import parse_court_calendar_line

    url = COURT_URL_TEMPLATE.format(query=requests.utils.quote(last_name))
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Court website returned status {resp.status_code}")

    calendar_text = _extract_calendar_text(resp.text)
    lines = calendar_text.split("\n")

    header_end = next((i for i, line in enumerate(lines) if "--------" in line), -1)
    header_lines = lines[: header_end + 1] if header_end >= 0 else []

    all_events = [parse_court_calendar_line(line) for line in lines]
    all_events = [e for e in all_events if e]
    filtered_events = [e for e in all_events if is_target_attorney(e.attorney, target_full_name)]

    filtered_lines = [e.raw_line for e in filtered_events]
    filtered_text = "\n".join([*header_lines, "", *filtered_lines, ""])

    stats = {
        "total_events": len(all_events),
        "filtered_events": len(filtered_events),
        "excluded_events": len(all_events) - len(filtered_events),
    }
    return filtered_text, stats
