"""14기 트랙 구성(6개 + 공통)과 구글 캘린더 동기화 보조 동작 검증. 네트워크 없이 돈다."""
import datetime
import os
import unittest
from unittest import mock

import gcal_schedule
import gcal_sync
from gcal_schedule import COMMON_KEY, SOURCES, ParseError, parse_time_range


class TrackListTest(unittest.TestCase):
    def test_six_tracks_and_common(self):
        self.assertEqual(
            [key for key, _, _ in SOURCES],
            ["creator", "self_inquiry", "ai_agent", "ai_shortform", "app_dev", "builder", COMMON_KEY],
        )

    def test_each_notion_db_used_once(self):
        db_ids = [db for _, _, db in SOURCES]
        self.assertEqual(len(db_ids), len(set(db_ids)))

    def test_reused_dbs_point_at_14th_tracks(self):
        by_db = {db: label for _, label, db in SOURCES}
        self.assertEqual(by_db["2f76400e92688085ae02c6b3165f6947"], "AI 숏폼 영상 트랙")  # 13기 빌더 DB
        self.assertEqual(by_db["31a6400e926880668c52c0c2a720c245"], "나 탐구 트랙")      # 13기 디자인 DB
        self.assertEqual(by_db["4146400e92688077a81fd96b83d0708a"], "빌더 트랙")


class TimeRangeTest(unittest.TestCase):
    def t(self, h, m=0):
        return datetime.time(h, m)

    def test_morning_block_written_with_trailing_am(self):
        # 14기 빌더 트랙 토요일 오전 강의 표기
        self.assertEqual(parse_time_range("10:00-12:00AM"), (self.t(10), self.t(12)))
        self.assertEqual(parse_time_range("11:30-12:30AM"), (self.t(11, 30), self.t(12, 30)))

    def test_existing_notations_unchanged(self):
        self.assertEqual(parse_time_range("8:00-10:00PM"), (self.t(20), self.t(22)))
        self.assertEqual(parse_time_range("09:30-11:00PM"), (self.t(21, 30), self.t(23)))
        self.assertEqual(parse_time_range("12:00-13:00"), (self.t(12), self.t(13)))
        self.assertEqual(parse_time_range("10:00-13:00AM"), (self.t(10), self.t(13)))

    def test_real_midnight_end_still_rejected(self):
        with self.assertRaises(ParseError):
            parse_time_range("10:00PM-12:00AM")


class FakeSession:
    def __init__(self, summaries):
        self.summaries = summaries
        self.calls = []

    def request(self, method, url, *, params=None, json_body=None):
        self.calls.append((method, url, json_body))
        if method == "GET" and "/events" not in url:
            return {"summary": self.summaries[url.rsplit("/", 1)[1]]}
        return {}


class RenameCalendarTest(unittest.TestCase):
    def test_previous_cohort_name_is_reported_and_patched_only_on_apply(self):
        session = FakeSession({"cal1": "ASC 13기 · 빌더 트랙"})
        old = gcal_sync.rename_calendar(session, "cal1", "ASC 14기", "빌더 트랙", dry_run=True)
        self.assertEqual(old, "ASC 13기 · 빌더 트랙")
        self.assertNotIn("PATCH", [c[0] for c in session.calls])

        gcal_sync.rename_calendar(session, "cal1", "ASC 14기", "빌더 트랙", dry_run=False)
        patch = [c for c in session.calls if c[0] == "PATCH"]
        self.assertEqual(len(patch), 1)
        self.assertEqual(patch[0][2]["summary"], "ASC 14기 · 빌더 트랙")

    def test_matching_name_is_left_alone(self):
        session = FakeSession({"cal1": "ASC 14기 · 빌더 트랙"})
        self.assertIsNone(gcal_sync.rename_calendar(session, "cal1", "ASC 14기", "빌더 트랙", dry_run=False))
        self.assertNotIn("PATCH", [c[0] for c in session.calls])


class SubscribeLinksTest(unittest.TestCase):
    def test_retired_design_calendar_not_distributed(self):
        ids = '{"creator": "c@g", "design": "d@g", "builder": "b@g", "common": "x@g"}'
        with mock.patch.dict(os.environ, {"GOOGLE_CALENDAR_IDS_JSON": ids}):
            links = gcal_sync.subscribe_links()
        self.assertEqual(set(links), {"creator", "builder", "common"})
        self.assertEqual(links["builder"]["label"], "빌더 트랙")


class SyncAllReportTest(unittest.TestCase):
    def test_new_tracks_get_new_calendars_in_preview(self):
        tracks = {key: {"label": label, "events": []} for key, label, _ in SOURCES}
        known = {"common": "x", "builder": "b", "creator": "c", "ai_agent": "a", "app_dev": "p", "design": "d"}
        session = FakeSession({v: f"ASC 13기 · {k}" for k, v in known.items()})
        with mock.patch.object(gcal_sync, "build_track_calendars", return_value=tracks), \
                mock.patch.object(gcal_sync, "load_token", return_value="t"), \
                mock.patch.object(gcal_sync, "_session", return_value=session), \
                mock.patch.object(gcal_sync, "load_calendar_ids", return_value=known):
            report = gcal_sync.sync_all(dry_run=True, cohort_label="ASC 14기", year=2026)
        new = {k for k, t in report["tracks"].items() if t["new_calendar"]}
        self.assertEqual(new, {"self_inquiry", "ai_shortform"})
        self.assertNotIn("design", report["tracks"])
        self.assertEqual(report["tracks"]["builder"]["renamed_from"], "ASC 13기 · builder")
        self.assertFalse([c for c in session.calls if c[0] in ("POST", "PUT", "PATCH", "DELETE")])


if __name__ == "__main__":
    unittest.main()
