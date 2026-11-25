"""Romanian banking days, by a fixed rule: no network, no list to keep up to date by hand.

A banking day is a weekday that is not a legal holiday (Codul muncii, art. 139). Holidays a later law added count
only from the year it took effect, so an older year never gets a day off it didn't have: a statement that skips a
day this rule calls a banking day is refused, never accepted."""

from __future__ import annotations

from datetime import date, timedelta

# (month, day, first year it is a legal holiday)
_FIXED = (
    (1, 1, 0),
    (1, 2, 0),
    (1, 6, 2024),  # Bobotează
    (1, 7, 2024),  # Sfântul Ioan
    (1, 24, 2017),  # Unirea Principatelor
    (5, 1, 0),
    (6, 1, 2017),  # Ziua Copilului
    (8, 15, 2009),  # Adormirea Maicii Domnului (Legea 202/2008)
    (11, 30, 2012),  # Sfântul Andrei
    (12, 1, 0),
    (12, 25, 0),
    (12, 26, 0),
)
# days from Orthodox Easter Sunday: Good Friday (from 2018), Easter Sunday and Monday, Rusalii Sunday and Monday
_MOVABLE = ((-2, 2018), (0, 0), (1, 0), (49, 2009), (50, 2009))
FIRST, LAST = 2009, 2099  # the years this rule knows; outside them only weekends are days off


def orthodox_easter(year: int) -> date:
    """Orthodox Easter Sunday (Meeus' Julian computus, moved to the Gregorian calendar; 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return date(year, month, day + 1) + timedelta(days=13)


def holidays(year: int) -> set[date]:
    if not FIRST <= year <= LAST:
        return set()
    easter = orthodox_easter(year)
    fixed = {date(year, m, d) for m, d, since in _FIXED if year >= since}
    return fixed | {easter + timedelta(days=n) for n, since in _MOVABLE if year >= since}


def banking_day(day: date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def none_between(first: date, until: date) -> bool:
    """No banking day from `first` up to, not including, `until`: a statement starting on `until` leaves out no day
    the bank could have booked a movement on."""
    return all(not banking_day(first + timedelta(days=n)) for n in range((until - first).days))
