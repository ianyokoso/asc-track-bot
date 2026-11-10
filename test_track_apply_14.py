"""14기 신청서 6개 트랙 — 신청 폼·노션 요일 칸·Discord 역할 접두사가 같은 트랙 목록을 쓰는지 검증.

admin_server 는 flask 를 import 하므로 서버(또는 flask 가 깔린 환경)에서 돈다:
    ASC_ENV=test python3 -m unittest test_track_apply_14
"""
import re
import unittest
from pathlib import Path

import admin_server
from cogs.admin import _TRACK_DISCORD_PREFIX, _uses_creator_subform_roles
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
            # 14기~ 크리에이터도 '크리에이터 트랙' 그대로 (숏폼/롱폼 치환 없음)
            self.assertEqual(got["weekdays"][day], label, key)
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
        self.assertEqual(prefix, "AI숏폼영상")
        self.assertEqual(admin_server._DISCORD_PREFIX_TO_TRACK[prefix], "AI 숏폼 영상 트랙")
        m = admin_server._TRACK_ROLE_RE.match(f"{prefix}-14기")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("prefix"), prefix)

    def test_every_14th_track_has_a_prefix(self):
        for _, label, _ in TRACKS_14.values():
            self.assertIn(label, _TRACK_DISCORD_PREFIX, label)

    def test_creator_is_single_role_from_14th(self):
        # 14기~: '크리에이터-14기' 역할 + '크리에이터-14기-과제-인증' 채널 (숏폼/롱폼 분리 없음)
        self.assertFalse(_uses_creator_subform_roles("크리에이터", "14"))
        self.assertFalse(_uses_creator_subform_roles("크리에이터", "15기"))
        # 13기 이하를 다시 반영하면 기존 '크리에이터-13기-숏폼' 역할 규칙 유지
        self.assertTrue(_uses_creator_subform_roles("크리에이터", "13"))
        self.assertFalse(_uses_creator_subform_roles("AI숏폼영상", "14"))

    def test_creator_leader_label(self):
        got = admin_server._extract_track_application_submission(
            {"tracks": [{"type": "weekday", "id": "creator", "leader": True}]}
        )
        self.assertEqual(got["leaderLabels"], ["크리에이터 트랙"])


class TrackSurveyTest(unittest.TestCase):
    """저장하기 뒤 트랙별 설문 — 저장 모양·병합·노션 텍스트·공개 범위."""

    USER = {"id": "111111111111111111", "username": "tester", "displayName": "테스터"}
    TRACKS = [{"type": "weekday", "id": "creator"}, {"type": "weekday", "id": "ai_shortform"}]
    CREATOR = {"level": "가끔 올리지만 꾸준하지 않아요", "link": " https://threads.net/@me ",
               "focus": ["첫 줄부터 읽히는 글쓰기", "꾸준히 발행하는 루틴 만들기", "세 번째는 버림"],
               "focusReason": "글감은 많아요", "goal": "4주 12편", "groupTime": "평일 저녁"}

    def test_sanitize_keeps_only_applied_weekday_tracks(self):
        got = admin_server._sanitize_track_surveys(
            {"creator": self.CREATOR, "builder": {"level": "처음이에요"}, "ai_shortform": "not-a-dict"},
            self.TRACKS,
        )
        self.assertEqual(list(got), ["creator"])
        self.assertEqual(got["creator"]["link"], "https://threads.net/@me")
        self.assertEqual(len(got["creator"]["focus"]), 2)  # 최대 2개

    def test_page_by_page_submit_merges_and_does_not_count_as_edit(self):
        first = admin_server._build_track_application_record(
            self.USER, {"tracks": self.TRACKS, "notes": "메모", "surveys": {"creator": self.CREATOR}})
        self.assertEqual(list(first["surveys"]), ["creator"])
        second = admin_server._build_track_application_record(
            self.USER,
            {"tracks": self.TRACKS, "notes": "메모", "surveys": {"ai_shortform": {"level": "만들어 본 적 없어요"}}},
            existing=first,
        )
        self.assertEqual(sorted(second["surveys"]), ["ai_shortform", "creator"])
        self.assertEqual(second["edits"], 0)  # 설문만 더한 저장은 수정으로 안 셈

    def test_changing_tracks_counts_as_edit_and_drops_old_survey(self):
        first = admin_server._build_track_application_record(
            self.USER, {"tracks": self.TRACKS, "notes": "메모", "surveys": {"creator": self.CREATOR}})
        changed = admin_server._build_track_application_record(
            self.USER, {"tracks": [{"type": "weekday", "id": "builder"}], "notes": "메모"}, existing=first)
        self.assertEqual(changed["edits"], 1)
        self.assertEqual(changed["surveys"], {})

    def test_admin_edit_keeps_surveys_it_did_not_send(self):
        existing = admin_server._build_track_application_record(
            self.USER, {"tracks": self.TRACKS, "notes": "메모", "surveys": {"creator": self.CREATOR}})
        raw = {k: v for k, v in existing.items() if k != "surveys"}
        got = admin_server._build_admin_track_application_record(raw, existing=existing)
        self.assertIn("creator", got["surveys"])

    def test_notion_text(self):
        record = admin_server._build_track_application_record(
            self.USER, {"tracks": self.TRACKS, "notes": "", "surveys": {"creator": self.CREATOR}})
        text = admin_server._format_track_surveys_text(record)
        self.assertTrue(text.startswith("[크리에이터 트랙]"))
        self.assertIn("· 기대하는 내용: 첫 줄부터 읽히는 글쓰기, 꾸준히 발행하는 루틴 만들기", text)
        self.assertIn("· 조별 모임 선호: 평일 저녁", text)
        chunks = admin_server._notion_rich_text_chunks("가" * 4000)
        self.assertEqual([len(c["text"]["content"]) for c in chunks], [1900, 1900, 200])
        fields = admin_server._resolve_track_application_db_fields({"properties": {"트랙 설문": {"type": "rich_text"}}})
        self.assertEqual(fields["surveys"], "트랙 설문")

    def test_list_hides_surveys_from_non_admin(self):
        from unittest import mock
        member = {"id": "1", "userId": "1", "name": "a", "tracks": self.TRACKS, "surveys": {"creator": self.CREATOR}}
        client = admin_server.app.test_client()
        with mock.patch.object(admin_server, "_verify_session_guild_membership", lambda *a, **k: None), \
             mock.patch.object(admin_server, "_build_track_application_member_list", lambda c: [dict(member)]), \
             mock.patch.object(admin_server, "_find_viewer_track_application_member", lambda c, u: None):
            with mock.patch.object(admin_server, "_is_admin_session", lambda: False):
                got = client.get("/api/track-applications").get_json()["members"][0]
                self.assertNotIn("surveys", got)
            with mock.patch.object(admin_server, "_is_admin_session", lambda: True):
                got = client.get("/api/track-applications").get_json()["members"][0]
                self.assertIn("creator", got["surveys"])


if __name__ == "__main__":
    unittest.main()
