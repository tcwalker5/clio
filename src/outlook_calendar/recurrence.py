"""
recurrence.py — Translates a Microsoft Graph `recurrence.pattern` object into
the RFC-5545 RRULE string Clio's CalendarEntry.recurrence_rule expects.

Confirmed against real data, not guessed: Clio's OpenAPI spec only describes
this field as "Recurrence rule for expanding," with no format or example.
Verified directly by (1) having a human create a real recurring entry in
Clio's own UI ("repeats every 1 day") and reading back its recurrence_rule
("FREQ=DAILY;WKST=SU"), then (2) POSTing a translated relativeMonthly pattern
("third Thursday") as a disposable test entry and confirming Clio accepted
and echoed back an equivalent RRULE, then deleting it. Clio normalizes away
a default INTERVAL=1 in its echo — harmless, not a rejection.

All 35 real recurring series checked in Heidi's calendar use range.type
"noEnd" (genuinely open-ended in Outlook, not just simplified here) — so
this only handles the pattern, not range/UNTIL/COUNT. If a bounded series
shows up later, extend this rather than assume an UNTIL format works.
"""

_DAY_TO_RRULE = {
    "monday": "MO", "tuesday": "TU", "wednesday": "WE", "thursday": "TH",
    "friday": "FR", "saturday": "SA", "sunday": "SU",
}
_INDEX_TO_ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4, "last": -1}
_INDEX_TO_WORD = {"first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "last": "last"}
_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def pattern_to_rrule(pattern: dict) -> str:
    """Raises ValueError on a pattern type never seen in real data (safer
    than guessing at an untested RRULE shape). Omits INTERVAL when it's 1
    (the default) — confirmed Clio's own API does the same when echoing back
    a stored recurrence_rule (POSTed "INTERVAL=1;BYDAY=3TH..." came back as
    "BYDAY=3TH..." with no INTERVAL). Matching that canonical form matters
    for dedup: outlook_recurring_availability.py compares a freshly generated
    RRULE against what's already stored in Clio to decide whether a series
    was already imported, and INTERVAL=1 is the overwhelmingly common case."""
    t = pattern["type"]
    interval = pattern.get("interval", 1)
    interval_part = "" if interval == 1 else f"INTERVAL={interval};"
    wkst = _DAY_TO_RRULE.get(pattern.get("firstDayOfWeek", "sunday"), "SU")

    if t == "daily":
        return f"FREQ=DAILY;{interval_part}WKST={wkst}"
    if t == "weekly":
        days = ",".join(_DAY_TO_RRULE[d] for d in pattern["daysOfWeek"])
        return f"FREQ=WEEKLY;{interval_part}BYDAY={days};WKST={wkst}"
    if t == "absoluteMonthly":
        return f"FREQ=MONTHLY;{interval_part}BYMONTHDAY={pattern['dayOfMonth']};WKST={wkst}"
    if t == "relativeMonthly":
        ordinal = _INDEX_TO_ORDINAL[pattern["index"]]
        day = _DAY_TO_RRULE[pattern["daysOfWeek"][0]]
        return f"FREQ=MONTHLY;{interval_part}BYDAY={ordinal}{day};WKST={wkst}"
    if t == "absoluteYearly":
        return f"FREQ=YEARLY;{interval_part}BYMONTH={pattern['month']};BYMONTHDAY={pattern['dayOfMonth']};WKST={wkst}"
    if t == "relativeYearly":
        ordinal = _INDEX_TO_ORDINAL[pattern["index"]]
        day = _DAY_TO_RRULE[pattern["daysOfWeek"][0]]
        return f"FREQ=YEARLY;{interval_part}BYMONTH={pattern['month']};BYDAY={ordinal}{day};WKST={wkst}"
    raise ValueError(f"Unhandled Graph recurrence pattern type: {t!r} — never seen in real data, don't guess the RRULE.")


def describe_pattern(pattern: dict) -> str:
    """Human-readable summary for --dry-run output and the audit CSV, e.g.
    'every Tuesday', 'every 3rd Thursday of the month', 'every March 14'."""
    t = pattern["type"]
    interval = pattern.get("interval", 1)
    every = "every" if interval == 1 else f"every {interval}"

    if t == "daily":
        return f"{every} day"
    if t == "weekly":
        days = " and ".join(d.capitalize() for d in pattern["daysOfWeek"])
        return f"{every} week on {days}"
    if t == "absoluteMonthly":
        return f"{every} month on day {pattern['dayOfMonth']}"
    if t == "relativeMonthly":
        word = _INDEX_TO_WORD[pattern["index"]]
        day = pattern["daysOfWeek"][0].capitalize()
        return f"{every} month on the {word} {day}"
    if t == "absoluteYearly":
        month = _MONTH_NAMES[pattern["month"]]
        return f"{every} year on {month} {pattern['dayOfMonth']}"
    if t == "relativeYearly":
        word = _INDEX_TO_WORD[pattern["index"]]
        day = pattern["daysOfWeek"][0].capitalize()
        month = _MONTH_NAMES[pattern["month"]]
        return f"{every} year on the {word} {day} of {month}"
    return f"unrecognized pattern ({t})"
