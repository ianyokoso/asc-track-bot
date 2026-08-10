"""Pure validation helpers for cohort track schedule configuration."""

from datetime import datetime
import re

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
_WEEKDAYS = {'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'}


def _iso_date(value):
    raw = str(value or '').strip()
    if not raw or not _DATE_RE.match(raw):
        return None
    try:
        datetime.strptime(raw, '%Y-%m-%d')
    except ValueError:
        return None
    return raw


def validate_track_schedules(value):
    """Validate and normalize track schedules, returning ``(value, error)``."""
    if not isinstance(value, list):
        return None, 'trackSchedules must be a list'

    def fail(index, field, detail):
        return None, f'trackSchedules[{index}].{field} {detail}'

    def bounded_int(raw, minimum, maximum):
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        return raw if minimum <= raw <= maximum else None

    cleaned = []
    seen_track_ids = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return fail(index, '', 'must be an object')

        track_id = str(item.get('trackId') or '').strip()
        track_name = str(item.get('trackName') or '').strip()
        weekday = str(item.get('weekday') or '').strip().upper()
        session_time = str(item.get('sessionTime') or '').strip()
        if not track_id:
            return fail(index, 'trackId', 'is required')
        if track_id in seen_track_ids:
            return fail(index, 'trackId', 'must be unique')
        seen_track_ids.add(track_id)
        if not track_name:
            return fail(index, 'trackName', 'is required')
        if weekday not in _WEEKDAYS:
            return fail(index, 'weekday', 'must be MON..SUN')
        if not _TIME_RE.match(session_time):
            return fail(index, 'sessionTime', 'must be HH:MM')

        first_raw = item.get('firstSessionDate')
        if first_raw in (None, ''):
            first_session_date = None
        else:
            first_session_date = _iso_date(first_raw)
            if not first_session_date:
                return fail(index, 'firstSessionDate', 'must be YYYY-MM-DD or empty')

        session_count = bounded_int(item.get('sessionCount', 4), 1, 12)
        if session_count is None:
            return fail(index, 'sessionCount', 'must be an integer from 1 to 12')
        days_before = bounded_int(item.get('announcementDaysBefore', 1), 0, 14)
        if days_before is None:
            return fail(index, 'announcementDaysBefore', 'must be an integer from 0 to 14')
        announcement_time = str(item.get('announcementTime', '18:00') or '').strip()
        if not _TIME_RE.match(announcement_time):
            return fail(index, 'announcementTime', 'must be HH:MM')

        raw_exceptions = item.get('exceptions', [])
        if not isinstance(raw_exceptions, list):
            return fail(index, 'exceptions', 'must be a list')
        exceptions = []
        seen_exception_weeks = set()
        for exception_index, exception in enumerate(raw_exceptions):
            prefix = f'exceptions[{exception_index}]'
            if not isinstance(exception, dict):
                return fail(index, prefix, 'must be an object')
            week = bounded_int(exception.get('week'), 1, 12)
            if week is None:
                return fail(index, f'{prefix}.week', 'must be an integer from 1 to 12')
            if week > session_count:
                return fail(index, f'{prefix}.week', 'must not exceed sessionCount')
            if week in seen_exception_weeks:
                return fail(index, f'{prefix}.week', 'must be unique')
            seen_exception_weeks.add(week)
            date = _iso_date(exception.get('date'))
            if not date:
                return fail(index, f'{prefix}.date', 'must be YYYY-MM-DD')
            exception_time_raw = exception.get('sessionTime')
            if exception_time_raw in (None, ''):
                exception_time = None
            else:
                exception_time = str(exception_time_raw).strip()
                if not _TIME_RE.match(exception_time):
                    return fail(index, f'{prefix}.sessionTime', 'must be HH:MM or empty')
            exceptions.append({'week': week, 'date': date, 'sessionTime': exception_time})

        cleaned.append({
            'trackId': track_id[:120],
            'trackName': track_name[:120],
            'weekday': weekday,
            'sessionTime': session_time,
            'firstSessionDate': first_session_date,
            'sessionCount': session_count,
            'announcementDaysBefore': days_before,
            'announcementTime': announcement_time,
            'exceptions': exceptions,
        })
    return cleaned, None
