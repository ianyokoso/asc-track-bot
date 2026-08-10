import unittest

from cohort_config_validation import validate_track_schedules as _validate_track_schedules


class ValidateTrackSchedulesTests(unittest.TestCase):
    def test_applies_defaults_and_normalizes_optional_values(self):
        schedules, error = _validate_track_schedules([
            {
                "trackId": " app_dev ",
                "trackName": " 앱 개발 트랙 ",
                "weekday": "MON",
                "sessionTime": "20:00",
                "firstSessionDate": "",
                "exceptions": [
                    {"week": 2, "date": "2026-08-18", "sessionTime": "19:30"},
                    {"week": 3, "date": "2026-08-25"},
                ],
            }
        ])

        self.assertIsNone(error)
        self.assertEqual(schedules, [{
            "trackId": "app_dev",
            "trackName": "앱 개발 트랙",
            "weekday": "MON",
            "sessionTime": "20:00",
            "firstSessionDate": None,
            "sessionCount": 4,
            "announcementDaysBefore": 1,
            "announcementTime": "18:00",
            "exceptions": [
                {"week": 2, "date": "2026-08-18", "sessionTime": "19:30"},
                {"week": 3, "date": "2026-08-25", "sessionTime": None},
            ],
        }])

    def test_accepts_contract_boundaries(self):
        schedules, error = _validate_track_schedules([{
            "trackId": "builder",
            "trackName": "빌더 트랙",
            "weekday": "SUN",
            "sessionTime": "00:00",
            "firstSessionDate": "2026-08-02",
            "sessionCount": 12,
            "announcementDaysBefore": 0,
            "announcementTime": "23:59",
            "exceptions": [{"week": 12, "date": "2026-10-18", "sessionTime": "00:00"}],
        }])
        self.assertIsNone(error)
        self.assertEqual(schedules[0]["sessionCount"], 12)
        self.assertEqual(schedules[0]["announcementDaysBefore"], 0)

    def test_rejects_non_list_and_invalid_required_fields(self):
        invalid_values = [
            ({}, "trackSchedules must be a list"),
            ([{"trackId": "", "trackName": "A", "weekday": "MON", "sessionTime": "20:00"}], "trackId"),
            ([{"trackId": "builder", "trackName": "", "weekday": "MON", "sessionTime": "20:00"}], "trackName"),
            ([{"trackId": "builder", "trackName": "빌더 트랙", "weekday": "FUNDAY", "sessionTime": "20:00"}], "weekday"),
            ([{"trackId": "builder", "trackName": "빌더 트랙", "weekday": "MON", "sessionTime": "24:00"}], "sessionTime"),
        ]
        for value, message in invalid_values:
            with self.subTest(value=value):
                schedules, error = _validate_track_schedules(value)
                self.assertIsNone(schedules)
                self.assertIn(message, error)

    def test_rejects_invalid_ranges_dates_and_exception_shape(self):
        base = {"trackId": "builder", "trackName": "빌더 트랙", "weekday": "TUE", "sessionTime": "20:00"}
        mutations = [
            ({"sessionCount": 0}, "sessionCount"),
            ({"sessionCount": True}, "sessionCount"),
            ({"announcementDaysBefore": 15}, "announcementDaysBefore"),
            ({"announcementTime": "6 PM"}, "announcementTime"),
            ({"firstSessionDate": "2026-02-30"}, "firstSessionDate"),
            ({"exceptions": {}}, "exceptions must be a list"),
            ({"exceptions": [{"week": 13, "date": "2026-08-01"}]}, "week"),
            ({"exceptions": [{"week": 1, "date": "bad"}]}, "date"),
            ({"exceptions": [{"week": 1, "date": "2026-08-01", "sessionTime": "25:00"}]}, "sessionTime"),
            ({"sessionCount": 2, "exceptions": [{"week": 3, "date": "2026-08-01"}]}, "sessionCount"),
            ({"exceptions": [{"week": 1, "date": "2026-08-01"}, {"week": 1, "date": "2026-08-08"}]}, "unique"),
        ]
        for mutation, message in mutations:
            with self.subTest(mutation=mutation):
                schedules, error = _validate_track_schedules([{**base, **mutation}])
                self.assertIsNone(schedules)
                self.assertIn(message, error)

    def test_rejects_duplicate_track_ids(self):
        base = {"trackId": "builder", "trackName": "빌더 트랙", "weekday": "MON", "sessionTime": "20:00"}
        schedules, error = _validate_track_schedules([base, {**base, "trackName": "빌더 복제"}])
        self.assertIsNone(schedules)
        self.assertIn('must be unique', error or '')

    def test_rejects_unknown_or_mismatched_track_identity(self):
        base = {"trackId": "builder", "trackName": "빌더 트랙", "weekday": "MON", "sessionTime": "20:00"}
        schedules, error = _validate_track_schedules([
            {**base, 'trackId': 'x' * 81},
        ])
        self.assertIsNone(schedules)
        self.assertIn('at most 80', error or '')

        schedules, error = _validate_track_schedules([{**base, 'trackName': '빌더'}])
        self.assertIsNone(schedules)
        self.assertIn('must be 빌더 트랙', error or '')


if __name__ == "__main__":
    unittest.main()
