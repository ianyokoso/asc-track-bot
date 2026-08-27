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
import urllib.error
import urllib.parse
import urllib.request

from gcal_schedule import build_track_calendars, load_token

API = "https://www.googleapis.com/calendar/v3"
TIMEZONE = "Asia/Seoul"
# 노션 '구글 캘린더용 DB' 는 9~10월 = 12기 일정이다.
# .env 의 CURRENT_COHORT 는 진행 중인 기수라 여기와 어긋날 수 있어 따로 둔다.
DEFAULT_COHORT_LABEL = os.environ.get("GCAL_COHORT_LABEL", "ASC 12기")

# 이 스크립트가 만든 일정에만 붙는 표식. 손으로 넣은 일정은 건드리지 않는다.
SYNC_TAG = "asc-track-cal"

BASE_DIR = pathlib.Path(__file__).resolve().parent


class CredentialsMissing(RuntimeError):
    pass


# ─── 자격증명 ────────────────────────────────────────────────
TOKEN_URL = "https://oauth2.googleapis.com/token"


class _OAuthSession:
    """refresh token 으로 access token 을 받아 쓰는 세션.

    브라우저 승인은 scripts/gcal-sync/authorize.py 로 1회만 하면 되고,
    그 뒤로는 사람 개입 없이 돈다 — Slack 에서 부르려면 이게 필요하다.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._auth = (client_id, client_secret, refresh_token)
        self._token: str | None = None

    def _access_token(self) -> str:
        if self._token:
            return self._token
        client_id, client_secret, refresh_token = self._auth
        body = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(TOKEN_URL, data=body), timeout=30
            ) as resp:
                self._token = json.load(resp)["access_token"]
        except urllib.error.HTTPError as exc:
            raise CredentialsMissing(
                f"access token 갱신 실패 {exc.code}: {exc.read().decode()[:200]}"
            ) from exc
        return self._token

    def request(self, method: str, url: str, *, params=None, json_body=None):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._access_token()}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"{method} {url} → {exc.code} {exc.read().decode()[:300]}"
            ) from exc


def _session():
    """refresh token 으로 인증된 세션. 없으면 CredentialsMissing."""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    if client_id and client_secret and refresh_token:
        return _OAuthSession(client_id, client_secret, refresh_token)

    raise CredentialsMissing(
        "구글 자격증명 없음 — python3 scripts/gcal-sync/authorize.py <client_secret.json> 로 "
        "1회 승인할 것"
    )


def _call(session, method: str, path: str, **kwargs):
    return session.request(method, f"{API}{path}", **kwargs)


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
def _ensure_acl(session, calendar_id: str, share_with: str | None) -> None:
    """접근 권한을 맞춘다. 같은 규칙을 다시 넣어도 덮어쓰기라 여러 번 돌려도 안전하다."""
    # 링크를 아는 사람은 누구나 읽기 — 멤버 구독용
    _call(
        session,
        "POST",
        f"/calendars/{calendar_id}/acl",
        json_body={"role": "reader", "scope": {"type": "default"}},
    )
    if share_with:
        _call(
            session,
            "POST",
            f"/calendars/{calendar_id}/acl",
            json_body={"role": "owner", "scope": {"type": "user", "value": share_with}},
        )


def ensure_calendar(
    session, key: str, label: str, share_with: str | None, cohort_label: str
) -> str:
    """트랙 캘린더를 확보한다. 이미 만들어 뒀으면 그 id 를 쓴다(중복 생성 방지)."""
    ids = load_calendar_ids()
    calendar_id = ids.get(key)

    if calendar_id is None:
        created = _call(
            session,
            "POST",
            "/calendars",
            json_body={
                "summary": f"{cohort_label} · {label}",
                "description": (
                    f"{cohort_label} {label} 일정입니다. "
                    "공통 일정(OT·네트워킹·특강)도 함께 들어 있습니다.\n"
                    "원본: 노션 '구글 캘린더용 DB'"
                ),
                "timeZone": TIMEZONE,
            },
        )
        calendar_id = created["id"]
        ids[key] = calendar_id
        _save_calendar_ids(ids)

    # 이미 있던 캘린더에도 매번 걸어준다 — 나중에 공유 대상이 바뀌어도 따라잡을 수 있게.
    _ensure_acl(session, calendar_id, share_with)
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
                _call(session, "POST", f"/calendars/{calendar_id}/events", json_body=body)
            stats["created"] += 1
        elif _needs_update(prev, body):
            if not dry_run:
                _call(
                    session,
                    "PUT",
                    f"/calendars/{calendar_id}/events/{prev['id']}",
                    json_body=body,
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
    *,
    dry_run: bool = True,
    share_with: str | None = None,
    year: int | None = None,
    cohort_label: str | None = None,
) -> dict:
    """전 트랙 동기화. 리포트 dict 를 돌려주므로 Slack 메시지로 그대로 옮길 수 있다."""
    year = year or int(os.environ.get("GCAL_SCHEDULE_YEAR", datetime.date.today().year))
    cohort_label = cohort_label or DEFAULT_COHORT_LABEL
    tracks = build_track_calendars(load_token(), year)

    try:
        session = _session()
        offline = False
    except CredentialsMissing as exc:
        if not dry_run:
            raise
        # 자격증명 없이도 무엇이 들어갈지는 보여준다.
        session, offline = None, str(exc)

    report = {
        "year": year,
        "cohort": cohort_label,
        "dry_run": dry_run,
        "offline": offline,
        "tracks": {},
    }
    for key, track in tracks.items():
        calendar_id = None
        if session:
            calendar_id = (
                load_calendar_ids().get(key)
                if dry_run
                else ensure_calendar(
                    session, key, track["label"], share_with, cohort_label
                )
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


def _load_env() -> None:
    """서버·로컬 모두에서 .env 계열을 읽는다.

    서버에는 python-dotenv 가 깔려 있으니 repo 표준 로더(env_utils)를 그대로 쓴다.
    로컬엔 없을 수 있어서, 그때만 같은 우선순위(.env → .env.prod → .env.test)로
    직접 읽는다. 이미 셸에 있는 값은 건드리지 않는다.
    """
    try:
        from env_utils import load_backend_env

        load_backend_env(str(BASE_DIR))
        return
    except ImportError:
        pass

    for name in (".env", ".env.prod", ".env.test"):
        path = BASE_DIR / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip("\"'")


def main() -> None:
    _load_env()

    parser = argparse.ArgumentParser(description="노션 트랙 일정 → 구글 캘린더")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="바뀔 내용만 출력 (기본)")
    group.add_argument("--apply", action="store_true", help="실제로 반영")
    group.add_argument("--links", action="store_true", help="구독 링크 출력")
    parser.add_argument("--share-with", help="캘린더 소유권을 넘길 구글 계정")
    parser.add_argument("--year", type=int, help="일정 연도 (기본: 올해)")
    parser.add_argument("--cohort", type=int, help="기수 숫자 (예: 12)")
    args = parser.parse_args()

    if args.links:
        for key, link in subscribe_links().items():
            print(f"\n■ {key}\n  추가: {link['add']}\n  ICS : {link['ics']}")
        return

    report = sync_all(
        dry_run=not args.apply,
        share_with=args.share_with,
        year=args.year,
        cohort_label=f"ASC {args.cohort}기" if args.cohort else None,
    )
    print(
        f"{report['cohort']} · 기준 연도 {report['year']}년 · "
        f"{'미리보기' if report['dry_run'] else '반영'}"
    )
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
