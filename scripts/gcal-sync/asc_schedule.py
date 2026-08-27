"""ASC 트랙 일정(노션 '구글 캘린더용 DB') → 정규화된 이벤트 목록.

노션 원본은 사람이 읽는 표기라 그대로 캘린더에 못 넣는다:
  날짜  "9/9 (수)"        — 연도 없음, 요일이 괄호로 붙음
  시간  "8:00-10:00PM"    — 종료에만 오전/오후 표기, "10:00-13:00AM" 같은 24시간 혼용도 있음
여기서 그 표기를 KST datetime 으로 푼다.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import urllib.request

NOTION_VERSION = "2022-06-28"
TIMEZONE = "Asia/Seoul"

# 노션 '구글 캘린더용 DB' 페이지 안의 인라인 DB들
PAGE_ID = "2f76400e926880428159d8a651a703d3"
COMMON_KEY = "common"
SOURCES: list[tuple[str, str, str]] = [
    # (key, 표시 이름, notion db id)
    ("builder",     "빌더 트랙",        "2f76400e92688085ae02c6b3165f6947"),
    ("sales",       "세일즈 실전 트랙",  "2f76400e9268805c881ee9d772eef5ea"),
    ("ai_agent",    "AI 에이전트 트랙",  "2f76400e926880868000c05be3250a45"),
    ("design",      "디자인 트랙",       "31a6400e926880668c52c0c2a720c245"),
    ("self_inquiry", "나 탐구 트랙",      "3376400e926881e9a6b6f47ebe0b6c73"),
    (COMMON_KEY,    "공통 일정",         "3ba6400e926880349370fd63ef70e35d"),
]

WEEKDAYS = "월화수목금토일"


class ParseError(ValueError):
    """날짜/시간 표기를 해석하지 못했을 때. 조용히 건너뛰지 않고 드러낸다."""


def _dashed(notion_id: str) -> str:
    i = notion_id.replace("-", "")
    return f"{i[:8]}-{i[8:12]}-{i[12:16]}-{i[16:20]}-{i[20:]}"


def _plain(prop: dict) -> str:
    kind = prop["type"]
    if kind in ("title", "rich_text"):
        return "".join(chunk.get("plain_text", "") for chunk in prop[kind])
    return ""


def parse_date(raw: str, year: int) -> datetime.date:
    """'9/9 (수)' → date(2026, 9, 9). 괄호 요일은 검산에 쓴다."""
    m = re.match(r"\s*(\d{1,2})\s*/\s*(\d{1,2})\s*(?:\(\s*([월화수목금토일])\s*\))?", raw)
    if not m:
        raise ParseError(f"날짜 표기를 해석할 수 없음: {raw!r}")
    month, day, weekday = int(m.group(1)), int(m.group(2)), m.group(3)
    date = datetime.date(year, month, day)
    if weekday and WEEKDAYS[date.weekday()] != weekday:
        raise ParseError(
            f"요일 불일치: {raw!r} 는 {year}년 기준 {WEEKDAYS[date.weekday()]}요일"
        )
    return date


def _to_24h(hour: int, minute: int, meridiem: str | None) -> datetime.time:
    # 13 이상이면 이미 24시간 표기 — "10:00-13:00AM" 의 13:00 이 여기 해당한다.
    if hour >= 13:
        return datetime.time(hour, minute)
    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    return datetime.time(hour, minute)


def parse_time_range(raw: str) -> tuple[datetime.time, datetime.time]:
    """'8:00-10:00PM' → (20:00, 22:00).

    오전/오후 표기가 뒤쪽에만 붙는 게 이 DB 의 관행이라 앞쪽이 뒤쪽 표기를 물려받는다.
    """
    text = raw.replace("~", "-").replace("–", "-").replace("—", "-").strip()
    m = re.match(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)?\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*$",
        text,
        re.IGNORECASE,
    )
    if not m:
        raise ParseError(f"시간 표기를 해석할 수 없음: {raw!r}")
    sh, sm, s_mer, eh, em, e_mer = m.groups()
    s_mer = s_mer.upper() if s_mer else None
    e_mer = e_mer.upper() if e_mer else None
    start = _to_24h(int(sh), int(sm), s_mer or e_mer)
    end = _to_24h(int(eh), int(em), e_mer or s_mer)
    if end <= start:
        raise ParseError(f"종료가 시작보다 빠름: {raw!r} → {start}-{end}")
    return start, end


def _title_of(content: str) -> str:
    for line in content.splitlines():
        line = line.strip().lstrip("•-").strip()
        if line:
            return line
    return "(제목 없음)"


def fetch_rows(db_id: str, token: str) -> list[dict]:
    rows, cursor = [], None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{_dashed(db_id)}/query",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req) as resp:
            payload = json.load(resp)
        rows.extend(payload["results"])
        if not payload.get("has_more"):
            return rows
        cursor = payload["next_cursor"]


def build_events(token: str, year: int) -> dict[str, list[dict]]:
    """key → 이벤트 목록. 이벤트는 시작 시각 오름차순."""
    result: dict[str, list[dict]] = {}
    for key, label, db_id in SOURCES:
        events = []
        for page in fetch_rows(db_id, token):
            props = page["properties"]
            raw_date = _plain(props["날짜"])
            raw_time = _plain(props["시간"])
            content = _plain(props["내용"])
            date = parse_date(raw_date, year)
            start_t, end_t = parse_time_range(raw_time)
            events.append(
                {
                    "notion_page_id": page["id"].replace("-", ""),
                    "source": label,
                    "summary": _title_of(content),
                    "description": content,
                    "attendance": _plain(props["참가옵션"]),
                    "start": datetime.datetime.combine(date, start_t),
                    "end": datetime.datetime.combine(date, end_t),
                    "raw_date": raw_date,
                    "raw_time": raw_time,
                }
            )
        events.sort(key=lambda e: e["start"])
        result[key] = events
    return result


def load_token(env_path: str = ".env.test") -> str:
    token = os.environ.get("NOTION_TOKEN")
    if token:
        return token
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("NOTION_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NOTION_TOKEN 을 찾을 수 없음 (환경변수 또는 .env.test)")
