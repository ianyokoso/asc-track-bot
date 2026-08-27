"""노션 트랙 일정 → 구글 캘린더 동기화.

서버(Oracle)에서 돌아가는 걸 전제로 한다. 사람이 브라우저에서 승인하는 절차가 없어야
Slack 명령이나 대시보드 버튼으로 한 번에 돌릴 수 있기 때문에, 사용자 OAuth 가 아니라
**서비스 계정**을 쓴다.

    python3 gcal_sync.py --dry-run      # 무엇이 바뀌는지만 출력 (자격증명 없어도 됨)
    python3 gcal_sync.py --apply        # 실제 반영
    python3 gcal_sync.py --links        # 멤버 배포용 구독 링크 출력

프로그램에서 쓸 때는 sync_all() 이 그대로 리포트 dict 를 돌려주므로 Slack 메시지로
바로 옮길 수 있다.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib

from gcal_schedule import build_track_calendars, load_token

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
TIMEZONE = "Asia/Seoul"
COHORT_LABEL = os.environ.get("GCAL_COHORT_LABEL", "ASC 11기")

# 이 스크립트가 만든 일정에만 붙는 표식. 손으로 넣은 일정은 건드리지 않는다.
SYNC_TAG = "asc-track-cal"

BASE_DIR = pathlib.Path(__file__).resolve().parent


class CredentialsMissing(RuntimeError):
    pass


# ─── 자격증명 ────────────────────────────────────────────────
def _session():
    """서비스 계정으로 인증된 세션. 없으면 CredentialsMissing."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise CredentialsMissing(
            "GOOGLE_SERVICE_ACCOUNT_JSON 미설정 — 서비스 계정 JSON 파일 경로나 "
            "JSON 문자열 자체를 넣을 것"
        )
    info = json.loads(pathlib.Path(raw).read_text()) if raw.lstrip()[:1] != "{" else json.loads(raw)

    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - 설치 안내용
        raise CredentialsMissing("google-auth 미설치 — pip install google-auth") from exc

    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds)


def _call(session, method: str, path: str, **kwargs):
    resp = session.request(method, f"{API}{path}", **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} → {resp.status_code} {resp.text[:300]}")
    return resp.json() if resp.content else {}


# ─── 캘린더 id 보관 ──────────────────────────────────────────
def _state_path() -> pathlib.Path:
    env = os.environ.get("ASC_ENV", "test")
    return BASE_DIR / f"gcal_calendars.{env}.json"


def load_calendar_ids() -> dict[str, str]:
    override = os.environ.get("GOOGLE_CALENDAR_IDS_JSON")
    if override:
        return json.loads(override)
    path = _state_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_calendar_ids(ids: dict[str, str]) -> None:
    _state_path().write_text(
        json.dumps(ids, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ─── 캘린더 생성 ─────────────────────────────────────────────
def ensure_calendar(session, key: str, label: str, share_with: str | None) -> str:
    """트랙 캘린더를 확보한다. 이미 만들어 뒀으면 그 id 를 쓴다(중복 생성 방지)."""
    ids = load_calendar_ids()
    if key in ids:
        return ids[key]

    created = _call(
        session,
        "POST",
        "/calendars",
        json={
            "summary": f"{COHORT_LABEL} · {label}",
            "description": (
                f"{COHORT_LABEL} {label} 일정입니다. "
                "공통 일정(OT·네트워킹·특강)도 함께 들어 있습니다.\n"
                "원본: 노션 '구글 캘린더용 DB'"
            ),
            "timeZone": TIMEZONE,
        },
    )
    calendar_id = created["id"]

    # 링크를 아는 사람은 누구나 읽기 — 멤버 구독용
    _call(
        session,
        "POST",
        f"/calendars/{calendar_id}/acl",
        json={"role": "reader", "scope": {"type": "default"}},
    )
    # 서비스 계정이 소유자라 그대로 두면 운영자 화면에서 안 보인다. 사람에게도 넘겨준다.
    if share_with:
        _call(
            session,
            "POST",
            f"/calendars/{calendar_id}/acl",
            json={"role": "owner", "scope": {"type": "user", "value": share_with}},
        )

    ids[key] = calendar_id
    _save_calendar_ids(ids)
    return calendar_id


# ─── 이벤트 동기화 ───────────────────────────────────────────
def _to_google_event(event: dict) -> dict:
    description = event["description"]
    if event.get("attendance"):
        description += f"\n\n참가: {event['attendance']}"
    description += f"\n\nhttps://www.notion.so/{event['notion_page_id']}"
    return {
        "summary": event["summary"],
        "description": description,
        "start": {"dateTime": event["start"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TIMEZONE},
        "end": {"dateTime": event["end"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TIMEZONE},
        "extendedProperties": {
            "private": {"syncTag": SYNC_TAG, "notionPageId": event["notion_page_id"]},
        },
    }


def _list_synced(session, calendar_id: str) -> dict[str, dict]:
    """이 스크립트가 넣은 일정만 노션 page id → 이벤트로 모은다."""
    found, page_token = {}, None
    while True:
        params = {
            "privateExtendedProperty": f"syncTag={SYNC_TAG}",
            "maxResults": 2500,
            "showDeleted": "false",
            "singleEvents": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        data = _call(session, "GET", f"/calendars/{calendar_id}/events", params=params)
        for item in data.get("items", []):
            page_id = (
                item.get("extendedProperties", {}).get("private", {}).get("notionPageId")
            )
            if page_id:
                found[page_id] = item
        page_token = data.get("nextPageToken")
        if not page_token:
            return found


def _needs_update(prev: dict, body: dict) -> bool:
    """내용이 실제로 달라졌을 때만 update 를 날린다 (호출·수정이력 절약)."""
    return (
        prev.get("summary") != body["summary"]
        or prev.get("description", "") != body["description"]
        or prev.get("start", {}).get("dateTime", "")[:19] != body["start"]["dateTime"]
        or prev.get("end", {}).get("dateTime", "")[:19] != body["end"]["dateTime"]
    )


def sync_calendar(session, calendar_id: str, events: list[dict], dry_run: bool) -> dict:
    """캘린더 하나를 목표 상태로 맞춘다. 노션에서 사라진 일정은 캘린더에서도 지운다."""
    existing = _list_synced(session, calendar_id) if session else {}
    stats = {"created": 0, "updated": 0, "deleted": 0, "kept": 0}

    seen = set()
    for event in events:
        body = _to_google_event(event)
        page_id = event["notion_page_id"]
        seen.add(page_id)
        prev = existing.get(page_id)
        if prev is None:
            if not dry_run:
                _call(session, "POST", f"/calendars/{calendar_id}/events", json=body)
            stats["created"] += 1
        elif _needs_update(prev, body):
            if not dry_run:
                _call(
                    session,
                    "PUT",
                    f"/calendars/{calendar_id}/events/{prev['id']}",
                    json=body,
                )
            stats["updated"] += 1
        else:
            stats["kept"] += 1

    for page_id, item in existing.items():
        if page_id not in seen:
            if not dry_run:
                _call(session, "DELETE", f"/calendars/{calendar_id}/events/{item['id']}")
            stats["deleted"] += 1

    return stats


# ─── 진입점 ──────────────────────────────────────────────────
def sync_all(
    *, dry_run: bool = True, share_with: str | None = None, year: int | None = None
) -> dict:
    """전 트랙 동기화. 리포트 dict 를 돌려주므로 Slack 메시지로 그대로 옮길 수 있다."""
    year = year or int(os.environ.get("GCAL_SCHEDULE_YEAR", datetime.date.today().year))
    tracks = build_track_calendars(load_token(), year)

    try:
        session = _session()
        offline = False
    except CredentialsMissing as exc:
        if not dry_run:
            raise
        # 자격증명 없이도 무엇이 들어갈지는 보여준다.
        session, offline = None, str(exc)

    report = {"year": year, "dry_run": dry_run, "offline": offline, "tracks": {}}
    for key, track in tracks.items():
        calendar_id = None
        if session:
            calendar_id = (
                load_calendar_ids().get(key)
                if dry_run
                else ensure_calendar(session, key, track["label"], share_with)
            )
        stats = (
            sync_calendar(session, calendar_id, track["events"], dry_run)
            if calendar_id or not session
            else {"created": len(track["events"]), "updated": 0, "deleted": 0, "kept": 0}
        )
        report["tracks"][key] = {
            "label": track["label"],
            "calendar_id": calendar_id,
            "events": len(track["events"]),
            **stats,
        }
    return report


def subscribe_links() -> dict[str, dict[str, str]]:
    links = {}
    for key, calendar_id in load_calendar_ids().items():
        links[key] = {
            "calendar_id": calendar_id,
            "add": f"https://calendar.google.com/calendar/u/0/r?cid={calendar_id}",
            "ics": f"https://calendar.google.com/calendar/ical/{calendar_id}/public/basic.ics",
        }
    return links


def main() -> None:
    parser = argparse.ArgumentParser(description="노션 트랙 일정 → 구글 캘린더")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="바뀔 내용만 출력 (기본)")
    group.add_argument("--apply", action="store_true", help="실제로 반영")
    group.add_argument("--links", action="store_true", help="구독 링크 출력")
    parser.add_argument("--share-with", help="캘린더 소유권을 넘길 구글 계정")
    parser.add_argument("--year", type=int, help="일정 연도 (기본: 올해)")
    args = parser.parse_args()

    if args.links:
        for key, link in subscribe_links().items():
            print(f"\n■ {key}\n  추가: {link['add']}\n  ICS : {link['ics']}")
        return

    report = sync_all(dry_run=not args.apply, share_with=args.share_with, year=args.year)
    print(f"기준 연도 {report['year']}년 · {'미리보기' if report['dry_run'] else '반영'}")
    if report["offline"]:
        print(f"⚠️  구글 미연결 — {report['offline']}\n   아래는 캘린더가 비었다고 가정한 계획입니다.\n")
    for track in report["tracks"].values():
        print(
            f"  {track['label']:<16} 일정 {track['events']:>2}건 → "
            f"추가 {track['created']:>2} / 수정 {track['updated']:>2} / "
            f"삭제 {track['deleted']:>2} / 유지 {track['kept']:>2}"
        )


if __name__ == "__main__":
    main()
