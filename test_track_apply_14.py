"""14기 신청서 6개 트랙 — 신청 폼·노션 요일 칸·Discord 역할 접두사가 같은 트랙 목록을 쓰는지 검증.

admin_server 는 flask 를 import 하므로 서버(또는 flask 가 깔린 환경)에서 돈다:
    ASC_ENV=test python3 -m unittest test_track_apply_14
"""
import re
import unittest
from pathlib import Path

import admin_server
from cogs.admin import _TRACK_DISCORD_PREFIX
from gcal_schedule import COMMON_KEY, SOURCES

PAGE = (Path(__file__).parent / "static" / "track-apply.html").read_text(encoding="utf-8")

# 14기 정규 6개: 트랙 id → (노션 트랙신청 DB 요일 칸, 트랙명, 폼 라디오 name)
TRACKS_14 = {
    "creator": ("monday", "크리에이터 트랙", "mon"),
    "self_inquiry": ("monday", "나 탐구 트랙", "mon"),
    "ai_agent": ("tuesday", "AI 에이전트 트랙", "tue"),
    "ai_shortform": ("wednesday", "AI 숏폼 영상 트랙", "wed"),
    "app_dev": ("thursday", "앱 개발 트랙", "thu"),
    "builder": ("saturday", "빌더 트랙", "sat"),
}


def _form_tracks():
    """신청 폼의 (라디오 name, data-track) 목록."""
    return re.findall(
        r'<input type="radio" name="(\w+)" id="[\w-]+">[^<]*</label>\s*</div>\s*'
        r'<div class="track-schedule" data-track="(\w+)">',
        PAGE,
    )


class FormTracksTest(unittest.TestCase):
    def test_form_offers_exactly_the_six_tracks(self):
        self.assertEqual(
            sorted(_form_tracks()),
            sorted((day, key) for key, (_, _, day) in TRACKS_14.items()),
        )

    def test_design_is_gone_from_form(self):
        self.assertNotIn('data-track="design"', PAGE)

    def test_form_tracks_match_calendar_sources(self):
        # 일정표는 gcal_schedule.SOURCES 기준으로 채워지므로 key 가 같아야 한다.
        source_keys = {key for key, _, _ in SOURCES if key != COMMON_KEY}
        self.assertEqual({key for _, key in _form_tracks()}, source_keys)

    def test_every_schedule_marker_is_filled(self):
        for key in TRACKS_14:
            body = re.search(
                rf"<!-- NOTION_SCHEDULE:{key} -->\n([\s\S]*?)\n<!-- /NOTION_SCHEDULE:{key} -->", PAGE
            )
            self.assertIsNotNone(body, key)
            self.assertIn('class="sched-row"', body.group(1), key)

    def test_ot_baseline_matches_page_schedule(self):
        # 일정 shift 기준일이 HTML 에 박힌 OT 와 어긋나면 모든 날짜가 그만큼 밀려 보인다.
        self.assertIn("const _OT_BASELINE_ISO = '2026-11-15';", PAGE)
        self.assertIn('<span class="ot-date-text">11/15(일)</span>', PAGE)


class SubmissionMappingTest(unittest.TestCase):
    def test_each_track_lands_in_its_weekday_column(self):
        for key, (day, label, _) in TRACKS_14.items():
            got = admin_server._extract_track_application_submission(
                {"tracks": [{"type": "weekday", "id": key}]}
            )
            expected = "크리에이터 숏폼 트랙" if key == "creator" else label
            self.assertEqual(got["weekdays"][day], expected, key)
            others = {d: v for d, v in got["weekdays"].items() if d != day}
            self.assertFalse(any(others.values()), (key, others))

    def test_two_tracks_on_different_days(self):
        got = admin_server._extract_track_application_submission(
            {"tracks": [
                {"type": "weekday", "id": "ai_shortform", "leader": True},
                {"type": "weekday", "id": "builder"},
            ]}
        )
        self.assertEqual(got["weekdays"]["wednesday"], "AI 숏폼 영상 트랙")
        self.assertEqual(got["weekdays"]["saturday"], "빌더 트랙")
        self.assertEqual(got["leaderLabels"], ["AI 숏폼 영상 트랙"])

    def test_saturday_column_is_written_to_notion(self):
        fields = admin_server._resolve_track_application_db_fields(
            {"properties": {"토요일 트랙": {"type": "select"}}}
        )
        self.assertEqual(fields["saturday_track"], "토요일 트랙")

    def test_new_track_is_canonical(self):
        self.assertIn("AI 숏폼 영상 트랙", admin_server._canonical_track_names())


class DiscordPrefixTest(unittest.TestCase):
    def test_new_track_role_prefix_round_trips(self):
        prefix = _TRACK_DISCORD_PREFIX["AI 숏폼 영상 트랙"]
        self.assertEqual(prefix, "AI숏폼")
        self.assertEqual(admin_server._DISCORD_PREFIX_TO_TRACK[prefix], "AI 숏폼 영상 트랙")
        m = admin_server._TRACK_ROLE_RE.match(f"{prefix}-14기")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("prefix"), prefix)

    def test_every_14th_track_has_a_prefix(self):
        for _, label, _ in TRACKS_14.values():
            lookup = "크리에이터 숏폼 트랙" if label == "크리에이터 트랙" else label
            self.assertIn(lookup, _TRACK_DISCORD_PREFIX, label)


if __name__ == "__main__":
    unittest.main()
