"""Coverage of half-open forecast intervals, compared as UTC instants."""
from datetime import timezone


def utc(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Forecast timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def covers(begin, end, intervals):
    begin, end = utc(begin), utc(end)
    if end <= begin:
        return False
    cursor = begin
    for start, stop in sorted((utc(a), utc(b)) for a, b in intervals):
        if stop <= start or stop <= cursor:
            continue
        if start > cursor:
            return False
        cursor = max(cursor, stop)
        if cursor >= end:
            return True
    return False
