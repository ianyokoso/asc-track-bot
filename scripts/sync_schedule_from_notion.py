#!/usr/bin/env python3
"""노션 '구글 캘린더용 DB' → 신청 페이지의 일정 세 곳을 한 번에 갱신.

일정 데이터가 페이지 안에 세 벌로 나뉘어 있어서, 하나만 고치면 나머지가 어긋난다.
실제로 그 사고가 반복돼서(앱 개발·디자인 3·4주차가 한 주 밀린 채 남아 있었다) 만든 도구다.

  ① 신청 폼의 '일정 확인하기' 표   static/track-apply.html 의 NOTION_SCHEDULE 블록
  ② '트랙 상세보기'               같은 파일의 PREOPEN_TRACKS[].weeks
  ③ 하단 '공통 일정' 목록          cohort_config_{env}.json 의 commonSchedule
  ④ 그 목록의 HTML 폴백            track-apply.html 의 ul.js-common-schedule (뷰마다 하나씩)
                                 평소엔 ③ 이 덮어쓰지만, 설정이 비거나 API 가 죽으면 이 값이 보인다

사용:
  python3 scripts/sync_schedule_from_notion.py              # 무엇이 바뀌는지만 출력
  python3 scripts/sync_schedule_from_notion.py --apply      # 실제 반영

반영 뒤에는 배포해야 라이브에 보인다:  bash scripts/push-and-deploy.sh --light
③ 은 서버의 설정 파일이 정답이라, 서버에서 한 번 더 돌리거나 파일을 옮겨야 한다
(로컬에 파일이 없으면 건너뛰고 적용할 JSON 을 출력한다).

트랙 목록·노션 DB id 는 gcal_schedule.SOURCES 를 그대로 쓴다 — 구글 캘린더 동기화와
같은 원본을 보게 해서 둘이 어긋나지 않도록.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import gcal_sync  # noqa: E402  (env 로더 재사용)
from gcal_schedule import (  # noqa: E402
    COMMON_KEY, SOURCES, _dashed, _plain, load_token, parse_date, parse_time_range,
)

TRACK_KEYS = [key for key, _, _ in SOURCES if key != COMMON_KEY]
PAGE_PATH = BASE_DIR / "static" / "track-apply.html"
KST = timezone(timedelta(hours=9))


# ─── 노션 읽기 ────────────────────────────────────────────────
def _query(db_id: str, token: str) -> list[dict]:
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{_dashed(db_id)}/query",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Notion-Version": "2022-06-28",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
        rows += payload["results"]
        if not payload.get("has_more"):
            return rows
        cursor = payload["next_cursor"]


def _normalize(page: dict, year: int) -> dict:
    props = page["properties"]
    raw_date, raw_time = _plain(props["날짜"]), _plain(props["시간"])
    option = _plain(props.get("참가옵션", {"type": "rich_text", "rich_text": []}))
    lines = [ln.strip(" -•\t") for ln in _plain(props["내용"]).split("\n") if ln.strip()]
    title = lines[0] if lines else "(제목 없음)"

    week = ""
    m = re.match(r"\[(\d)주차\]\s*", title)
    if m:
        week, title = f"{m.group(1)}주차", title[m.end():]
    elif "오리엔테이션" in title:
        week = "OT"

    day = parse_date(raw_date, year)
    start, end = parse_time_range(raw_time)
    return {
        "date": raw_date, "time": raw_time, "week": week, "title": title.strip(),
        "detail": " · ".join(lines[1:]), "option": option,
        "sort": (day, start), "span": (day, start, end),
        "day": day, "start": start, "end": end,
    }


def load_notion(year: int) -> tuple[dict[str, list[dict]], list[dict]]:
    token = load_token()
    src = {key: db for key, _, db in SOURCES}
    common = sorted((_normalize(p, year) for p in _query(src[COMMON_KEY], token)),
                    key=lambda e: e["sort"])
    tracks = {}
    for key in TRACK_KEYS:
        tracks[key] = sorted((_normalize(p, year) for p in _query(src[key], token)),
                             key=lambda e: e["sort"])
    return tracks, common


def _base_title(title: str) -> str:
    """괄호 설명을 뗀 핵심 제목 — 같은 일정이 트랙·공통에 다르게 적힌 걸 묶는다."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()


def merge_with_ot(track_events: list[dict], common: list[dict]) -> list[dict]:
    """트랙 일정 + 공통 OT. 나머지 공통 일정은 하단 목록에만 넣는다(운영 요청).

    트랙 DB 에도 OT 행이 있어서, 같은 시각·같은 제목이면 공통 쪽으로 대체한다.
    시각만 같고 제목이 다르면 다른 일정이다 — 빌더 1주차 강의가 공통 '세일즈 고민상담소'
    와 시간이 겹쳐 통째로 지워진 적이 있다.
    """
    ot = [c for c in common if c["week"] == "OT"]
    ot_keys = {(c["span"], _base_title(c["title"])) for c in ot}
    kept = [e for e in track_events if (e["span"], _base_title(e["title"])) not in ot_keys]
    return sorted(kept + ot, key=lambda e: e["sort"])


# ─── ① 신청 폼의 일정표 ───────────────────────────────────────
def _esc_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_TABLE_HEAD = ('<details class="schedule-details"><summary class="schedule-summary">일정 확인하기</summary>'
               '<div class="schedule-body"><table><thead><tr><th class="col-week">주차</th>'
               '<th class="col-date">날짜</th><th class="col-time">시간</th>'
               '<th class="col-session">세션</th></tr></thead><tbody>')
_TABLE_TAIL = "</tbody></table></div></details>"


def render_table(events: list[dict]) -> str:
    rows = []
    for ev in events:
        cell = f'<div class="sched-title">{_esc_html(ev["title"])}'
        if ev["option"] and ev["option"] != "필수":
            cell += f'<span class="sched-participation">{_esc_html(ev["option"])}</span>'
        cell += "</div>"
        if ev["detail"]:
            cell += f'<div class="sched-detail">{_esc_html(ev["detail"])}</div>'
        rows.append(
            f'<tr class="sched-row"><td class="col-week"><span class="sched-week">{ev["week"]}</span></td>'
            f'<td class="col-date">{_esc_html(ev["date"])}</td>'
            f'<td class="col-time">{_esc_html(ev["time"])}</td>'
            f'<td class="col-session">{cell}</td></tr>'
        )
    return "\n".join([_TABLE_HEAD] + rows + [_TABLE_TAIL])


def update_tables(page: str, merged: dict[str, list[dict]]) -> tuple[str, list[str]]:
    changed = []
    for key, events in merged.items():
        pattern = re.compile(
            rf"(<!-- NOTION_SCHEDULE:{key} -->\n)([\s\S]*?)(\n<!-- /NOTION_SCHEDULE:{key} -->)")
        m = pattern.search(page)
        if not m:
            print(f"  ⚠️  일정표 마커 없음: {key}")
            continue
        new = render_table(events)
        if m.group(2).strip() != new.strip():
            page = page[:m.start(2)] + new + page[m.end(2):]
            changed.append(key)
    return page, changed


# ─── ② 트랙 상세보기 (PREOPEN_TRACKS[].weeks) ─────────────────
def _esc_js(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def render_weeks(events: list[dict]) -> str:
    out = []
    for ev in events:
        line = (f"          {{ week: '{ev['week']}', date: '{_esc_js(ev['date'])}', "
                f"time: '{_esc_js(ev['time'])}', title: '{_esc_js(ev['title'])}'")
        if ev["detail"]:
            line += f", detail: '{_esc_js(ev['detail'])}'"
        if ev["option"] and ev["option"] != "필수":
            line += ", optional: true"
        out.append(line + " },")
    return "\n".join(out)


def update_preopen(page: str, merged: dict[str, list[dict]]) -> tuple[str, list[str]]:
    start = page.index("const PREOPEN_TRACKS")
    end = page.index("\n    ];", start)
    segment = page[start:end]
    changed = []

    for key, events in merged.items():
        # 해당 트랙 블록의 weeks 배열만 교체한다 (tagline·target 은 노션에 없으니 보존).
        block = re.search(
            rf"(\n      \{{\n        id: '{key}',)([\s\S]*?)(?=\n      \{{\n        id: '|\Z)",
            segment)
        if not block:
            print(f"  ⚠️  트랙 상세보기 블록 없음: {key}")
            continue
        weeks = re.search(r"(weeks: \[\n)([\s\S]*?)(\n        \],)", block.group(2))
        if not weeks:
            print(f"  ⚠️  weeks 배열 없음: {key}")
            continue
        new = render_weeks(events)
        if weeks.group(2).strip() == new.strip():
            continue
        s = block.start(2) + weeks.start(2)
        e = block.start(2) + weeks.end(2)
        segment = segment[:s] + new + segment[e:]
        changed.append(key)

    return page[:start] + segment + page[end:], changed


# ─── ③ 하단 공통 일정 (cohort config) ─────────────────────────
def _kor_time(ev: dict) -> str:
    def hm(t):
        return f"{t.hour % 12 or 12}:{t.minute:02d}"
    return f"{'오후' if ev['start'].hour >= 12 else '오전'} {hm(ev['start'])}–{hm(ev['end'])}"


def _label(ev: dict) -> str:
    """제목의 괄호 설명과 '장소:' 를 한 괄호로 합친다."""
    paren = re.search(r"\(([^)]*)\)\s*$", ev["title"])
    label = re.sub(r"\s*\([^)]*\)\s*$", "", ev["title"]).strip()
    place = re.search(r"장소:\s*([^·]+)", ev["detail"])
    parts = [p for p in [paren.group(1).strip() if paren else "",
                         place.group(1).strip().rstrip(" ·") if place else ""] if p]
    return label + (" (" + " · ".join(parts) + ")" if parts else "")


def build_common_schedule(common: list[dict]) -> list[dict]:
    return [{"date": ev["day"].isoformat(), "label": _label(ev), "time": _kor_time(ev)}
            for ev in common]


def render_common_fallback(common: list[dict]) -> str:
    """HTML 폴백 목록(ul.js-common-schedule)의 내용.

    평소엔 JS 가 서버 설정으로 통째로 덮어쓰지만, 설정이 비었거나 API 가 실패하면
    이 값이 그대로 보인다. 낡은 날짜가 남아 있으면 그때 사용자에게 틀린 일정이 나간다.
    """
    out = []
    for ev in common:
        out.append(
            '          <li class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">\n'
            f'            <span class="text-ink font-medium tabular-nums shrink-0">{_esc_html(ev["date"])}</span>\n'
            f'            <span class="text-ink-soft shrink-0">{_esc_html(_kor_time(ev))}</span>\n'
            '            <span class="text-ink-soft">·</span>\n'
            f'            <span class="text-ink">{_esc_html(_label(ev))}</span>\n'
            '          </li>'
        )
    return "\n".join(out)


def update_common_fallback(page: str, common: list[dict]) -> tuple[str, int]:
    """페이지 안의 모든 ul.js-common-schedule 을 갱신한다 (뷰마다 하나씩 있다)."""
    body = render_common_fallback(common)
    pattern = re.compile(r'(<ul class="space-y-1\.5 js-common-schedule">\n)([\s\S]*?)(\n        </ul>)')
    changed = 0
    out, pos = [], 0
    for m in pattern.finditer(page):
        out.append(page[pos:m.start(2)])
        if m.group(2).strip() != body.strip():
            changed += 1
        out.append(body)
        pos = m.end(2)
    out.append(page[pos:])
    return "".join(out), changed


def cohort_config_path() -> pathlib.Path:
    env = os.environ.get("ASC_ENV", "test")
    return BASE_DIR / f"cohort_config_{env}.json"


def update_cohort_config(path: pathlib.Path, schedule: list[dict], apply: bool) -> str:
    """반환: 'missing' | 'same' | 'changed' — 호출부가 상태를 그대로 출력한다."""
    if not path.exists():
        return "missing"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if cfg.get("commonSchedule") == schedule:
        return "same"
    if apply:
        cfg["commonSchedule"] = schedule
        cfg["updatedAt"] = datetime.now(KST).isoformat()
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "changed"


# ─── main ────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="노션 일정 → 신청 페이지 3곳 동기화")
    ap.add_argument("--apply", action="store_true", help="실제 파일 수정 (기본은 미리보기)")
    ap.add_argument("--year", type=int, default=2026, help="노션 날짜에 붙일 연도")
    args = ap.parse_args()

    gcal_sync._load_env()
    tracks, common = load_notion(args.year)
    merged = {key: merge_with_ot(evs, common) for key, evs in tracks.items()}

    print(f"노션: 트랙 {len(tracks)}개 / 공통 {len(common)}건\n")
    for key, evs in merged.items():
        print(f"  {key:<10} {len(evs)}건  ({evs[0]['date']} ~ {evs[-1]['date']})")

    page = PAGE_PATH.read_text(encoding="utf-8")
    page, t_changed = update_tables(page, merged)
    page, p_changed = update_preopen(page, merged)
    page, f_changed = update_common_fallback(page, common)
    schedule = build_common_schedule(common)
    cfg_path = cohort_config_path()
    cfg_state = update_cohort_config(cfg_path, schedule, args.apply)

    print("\n바뀌는 곳")
    print(f"  ① 신청 폼 일정표      : {', '.join(t_changed) if t_changed else '변경 없음'}")
    print(f"  ② 트랙 상세보기       : {', '.join(p_changed) if p_changed else '변경 없음'}")
    cfg_msg = {"missing": f"⚠️ {cfg_path.name} 이 없음 — 이 파일은 서버에 있다. 서버에서 실행할 것",
               "same": "변경 없음", "changed": "갱신"}[cfg_state]
    print(f"  ③ 공통 일정           : {cfg_msg}")
    print(f"  ④ 공통 일정 HTML 폴백 : {f_changed}곳 갱신" if f_changed else "  ④ 공통 일정 HTML 폴백 : 변경 없음")
    if cfg_state == "missing":
        print("     (수동 반영용 JSON)")
        print(json.dumps(schedule, ensure_ascii=False, indent=2))

    if not args.apply:
        print("\n[미리보기] 파일은 건드리지 않았습니다. 반영: --apply")
        return 0

    if t_changed or p_changed or f_changed:
        PAGE_PATH.write_text(page, encoding="utf-8")
    print("\n✅ 반영 완료 — 배포: bash scripts/push-and-deploy.sh --light")
    return 0


if __name__ == "__main__":
    sys.exit(main())
