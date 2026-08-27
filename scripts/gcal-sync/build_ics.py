"""트랙별 .ics 파일 생성 — 구글 캘린더 설정 없이 바로 가져오기용.

Apps Script(Code.gs)가 본 시스템이고, 이건 같은 데이터로 만드는 즉석 산출물이다.
캘린더에 넣기 전에 파싱 결과를 눈으로 확인하는 용도로도 쓴다.

    python3 scripts/gcal-sync/build_ics.py [출력디렉터리]
"""
from __future__ import annotations

import datetime
import os
import pathlib
import sys

from gcal_schedule import build_track_calendars, load_token

COHORT_LABEL = os.environ.get("GCAL_COHORT_LABEL", "ASC 12기")
UID_DOMAIN = "asc-track-bot"


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 는 한 줄 75옥텟 제한 — 넘으면 공백 한 칸으로 이어붙인다."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out, chunk = [], b""
    for ch in line:
        encoded = ch.encode("utf-8")
        limit = 75 if not out else 74
        if len(chunk) + len(encoded) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += encoded
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def build_ics(name: str, events: list[dict], stamp: datetime.datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{UID_DOMAIN}//ASC Track Schedule//KO",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
        "X-WR-TIMEZONE:Asia/Seoul",
        # 한국은 서머타임이 없어 KST 고정 오프셋으로 충분하다.
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Seoul",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0900",
        "TZOFFSETTO:+0900",
        "TZNAME:KST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for event in events:
        description = event["description"]
        if event.get("attendance"):
            description += f"\n\n참가: {event['attendance']}"
        description += f"\n\nhttps://www.notion.so/{event['notion_page_id']}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event['notion_page_id']}@{UID_DOMAIN}",
            f"DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}",
            f"DTSTART;TZID=Asia/Seoul:{event['start']:%Y%m%dT%H%M%S}",
            f"DTEND;TZID=Asia/Seoul:{event['end']:%Y%m%dT%H%M%S}",
            _fold(f"SUMMARY:{_escape(event['summary'])}"),
            _fold(f"DESCRIPTION:{_escape(description)}"),
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main() -> None:
    out_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "build/ics")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc)

    calendars = build_track_calendars(load_token(), year=2026)

    for key, calendar in calendars.items():
        events, label = calendar["events"], calendar["label"]
        name = f"{COHORT_LABEL} · {label}"
        path = out_dir / f"asc-{key}.ics"
        path.write_text(build_ics(name, events, stamp), encoding="utf-8")
        print(f"{path.name:24} {len(events):>2}건   {name}")


if __name__ == "__main__":
    main()
