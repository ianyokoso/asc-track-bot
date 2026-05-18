from flask import Flask, request, jsonify, redirect, session, send_from_directory
from flask_cors import CORS
from datetime import timedelta, datetime, timezone
import json
import os
import re
import requests
import secrets
import subprocess
import threading
import sys
import time
import uuid
from urllib.parse import urlencode

from env_utils import (
    get_bot_command_queue_file,
    get_bot_config_file,
    get_bot_heartbeat_file,
    get_writable_env_file,
    load_backend_env,
)

app = Flask(__name__)

# 🛡 CORS — wildcard + supports_credentials 는 CSRF 위험.
#    명시적 화이트리스트만 허용. 추가 도메인은 ALLOWED_ORIGINS env 로 콤마 구분.
_DEFAULT_ALLOWED_ORIGINS = [
    'https://asc-track-bot.vercel.app',         # 트랙 신청 폼
    'https://asc-bot-dashboard.vercel.app',     # 운영 대시보드
    'http://localhost:3000',                    # 로컬 dev
    'http://localhost:5173',                    # vite dev
    'http://127.0.0.1:3000',
    'http://127.0.0.1:5173',
]
_extra_origins = [o.strip() for o in (os.getenv('ALLOWED_ORIGINS', '') or '').split(',') if o.strip()]
ALLOWED_ORIGINS = _DEFAULT_ALLOWED_ORIGINS + _extra_origins
CORS(
    app,
    origins=ALLOWED_ORIGINS,
    supports_credentials=True,
    allow_headers=['Content-Type', 'Authorization'],
    methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')


@app.route('/')
def serve_root():
    return redirect('/apply', code=302)


def _no_cache_headers(response):
    """
    🛑 정적 HTML/JS/CSS 에 강제 캐시 무효화 헤더 부착.

    이전 footgun:
      Flask 의 send_from_directory 기본 SEND_FILE_MAX_AGE_DEFAULT = 12 hours.
      학생 모바일 브라우저가 옛 track-apply.html 을 12 시간 캐싱.
      운영진이 '전체 초기화' 한 후 신규 fix 가 배포돼도 학생 디바이스는
      옛 JS 그대로 → stale localStorage / 미적용 fix 로 UI 불일치 지속.
      "캐시 비워주세요" 라고 학생들에게 안내하는 건 운영상 비현실적.

    수정:
      - no-cache, no-store, must-revalidate 로 매 요청마다 서버에서 새로 받게.
      - Pragma/Expires 도 동시 명시 (legacy proxy 호환).
    """
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/apply')
def serve_apply_page():
    """track-bot Flask 가 직접 apply UI 정적 HTML 을 서빙."""
    return _no_cache_headers(send_from_directory(STATIC_DIR, 'track-apply.html'))


@app.route('/static/<path:filename>')
def serve_static_assets(filename):
    return _no_cache_headers(send_from_directory(STATIC_DIR, filename))


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

env_info = load_backend_env(BASE_DIR)
CONFIG_FILE = get_bot_config_file(BASE_DIR, explicit=env_info["env_name"])

ENV_FILE = get_writable_env_file(BASE_DIR)

print(f"[INFO] [Admin Server] Using config: {CONFIG_FILE}")
print(f"[INFO] [Admin Server] Settings will be loaded/saved to: {ENV_FILE}")
print(f"[INFO] [Admin Server] Env mode: {env_info['env_name']}")

app.secret_key = (
    os.getenv('DASHBOARD_SESSION_SECRET')
    or os.getenv('FLASK_SECRET_KEY')
    or 'asc-dashboard-dev-session-secret'
)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_COOKIE_NAME'] = 'asc_dashboard_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', '').lower() in {'1', 'true', 'yes'}


def _safe_error_message(exc, default='Internal server error.'):
    """
    Prod 환경에서는 내부 예외 메시지를 클라이언트로 노출하지 않는다.
    test/dev 에서는 디버깅을 위해 그대로 노출.

    공격자가 stack/lib 정보로 핑거프린팅 + 디스커버리 하는 걸 막기 위함.
    """
    env_name = (os.getenv('ASC_ENV') or os.getenv('RUN_MODE') or '').strip().lower()
    if env_name in ('test', 'dev', 'development', 'sandbox', 'staging', 'mock', 'local'):
        return str(exc) if exc else default
    return default


@app.after_request
def _restore_notion_token_after_request(response):
    """
    🛡 매 요청 종료 시 notion_api 모듈의 NOTION_TOKEN 을 환경변수 prod 값으로 강제 복원.

    track-application 등 일부 라우트가 _load_notion_api(test_token) 으로
    모듈 attr 를 test 토큰으로 바꾸는 경우 누수가 발생할 수 있는데,
    이 훅이 매 요청 끝에 prod 값으로 되돌려서 다음 요청/백그라운드 sync 가
    잘못된 워크스페이스를 조회하는 사고를 방지한다.
    """
    try:
        import notion_api as _notion_api
        prod_token = os.environ.get('NOTION_TOKEN')
        if prod_token and getattr(_notion_api, 'NOTION_TOKEN', None) != prod_token:
            _notion_api.NOTION_TOKEN = prod_token
    except Exception:
        # 어떤 이유로든 실패해도 응답은 그대로 반환 (요청 흐름을 깨지 않음)
        pass
    return response
TRACK_APPLICATION_DEFAULT_PATH = '/apply'
TEST_PERSONAL_DASHBOARD_PATH = '/__preview/personal-dashboard'
TRACK_APPLICATION_PATHS = {TRACK_APPLICATION_DEFAULT_PATH, '/track-apply'}
TRACK_APPLICATION_CACHE_ENV = (
    (env_info.get('env_name') or '').lower()
    if str(env_info.get('env_name') or '').lower() not in {'', 'legacy'}
    else ('prod' if os.path.exists(os.path.join(BASE_DIR, '.env.prod')) else 'default')
)
TRACK_APPLICATION_CACHE_FILE = os.path.join(
    BASE_DIR,
    f"track_applications_cache_{TRACK_APPLICATION_CACHE_ENV}.json"
)
TRACK_APPLICATION_ADMIN_MOCK_CACHE_FILE = os.path.join(
    BASE_DIR,
    f"track_applications_admin_mock_cache_{TRACK_APPLICATION_CACHE_ENV}.json"
)
COHORT_CONFIG_FILE = os.path.join(
    BASE_DIR,
    f"cohort_config_{TRACK_APPLICATION_CACHE_ENV}.json"
)

def load_env_file():
    env_vars = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    key, val = line.strip().split('=', 1)
                    env_vars[key.strip()] = val.strip()
    return env_vars

def save_env_file(env_vars):
    if not os.path.exists(ENV_FILE):
        print(f"[WARN] {ENV_FILE} not found. Creating new.")
        lines = []
    else:
        with open(ENV_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
    new_lines = []
    # Identify keys to update
    keys_to_update = list(env_vars.keys())
    
    for line in lines:
        updated = False
        for key in keys_to_update:
            if line.startswith(key + '='):
                new_lines.append(f"{key}={env_vars[key]}\n")
                keys_to_update.remove(key)
                updated = True
                break
        if not updated:
            new_lines.append(line)
            
    # Append new keys
    for key in keys_to_update:
        new_lines.append(f"{key}={env_vars[key]}\n")
        
    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)


GROUP_PREVIEW_DEFAULT_MEMBER_TEST_DB_ID = os.getenv(
    'GROUP_PREVIEW_TEST_MEMBER_DB_ID',
    '3566400e9268808e9b0ae23a1231e809',
)
GROUP_PREVIEW_DEFAULT_GROUP_TEST_DB_ID = os.getenv(
    'GROUP_PREVIEW_TEST_GROUP_DB_ID',
    '3566400e9268803b9c9bc5ade3cf3a21',
)
GROUP_PREVIEW_DEFAULT_TRACK_APPLICATION_TEST_DB_ID = os.getenv(
    'GROUP_PREVIEW_TEST_TRACK_APPLICATION_DB_ID',
    '3566400e926880e78335fcaba4914196',
)
GROUP_PREVIEW_TEST_NOTION_TOKEN = os.getenv('GROUP_PREVIEW_TEST_NOTION_TOKEN', '').strip()
DISCORD_API_BASE = 'https://discord.com/api/v10'
_DISCORD_CLIENT_ID_CACHE = {
    "resolved": False,
    "value": None,
}
DISCORD_CREATOR_TRACKS = {
    'Shortform': ['크리에이터 숏폼 트랙', '크리에이터 라이트 트랙 (숏폼)'],
    'Longform': ['크리에이터 롱폼 트랙', '크리에이터 라이트 트랙 (롱폼)'],
}


def _normalize_notion_id(value):
    if not value:
        return None
    cleaned = ''.join(ch for ch in str(value).strip() if ch.isalnum()).lower()
    return cleaned if len(cleaned) == 32 else None


def _get_track_application_notion_target():
    notion_client = _load_notion_api(notion_token_override=GROUP_PREVIEW_TEST_NOTION_TOKEN or None)
    track_application_db_id = _normalize_notion_id(
        os.getenv('GROUP_PREVIEW_TEST_TRACK_APPLICATION_DB_ID')
        or GROUP_PREVIEW_DEFAULT_TRACK_APPLICATION_TEST_DB_ID
        or os.getenv('TRACK_APPLICATION_DB_ID')
    )
    return notion_client, track_application_db_id


def _load_notion_api(notion_token_override=None):
    """
    notion_api 모듈을 안전하게 로드/재설정.

    🚨 이전 버그: override 토큰을 setattr 한 뒤 finally 에서 환경변수만 복원했음.
       그 결과 모듈 attr 가 test 토큰으로 고정 → 후속 prod sync 가 test 워크스페이스
       를 조회해 8기 멤버 0 명 같은 데이터 오염 발생.

    수정:
    - override 가 있는 호출: 환경변수를 임시로 바꿔 reload → 호출자에게 모듈 반환
      직후 환경변수와 모듈 attr 모두 prod 값으로 즉시 복원.
    - override 없는 호출: 환경변수의 prod NOTION_TOKEN 으로 강제 reload (이전
      누수가 있더라도 깨끗한 상태로 복구).
    """
    import importlib
    import notion_api as _notion_api

    prod_token = os.environ.get('NOTION_TOKEN')

    if notion_token_override:
        # 일시 override 후 reload
        os.environ['NOTION_TOKEN'] = notion_token_override
        try:
            _notion_api = importlib.reload(_notion_api)
            _notion_api.NOTION_TOKEN = notion_token_override
        finally:
            # 환경변수를 즉시 prod 로 복원 → 다른 스레드/요청이 이 값을 봐도 안전
            if prod_token is None:
                os.environ.pop('NOTION_TOKEN', None)
            else:
                os.environ['NOTION_TOKEN'] = prod_token
        return _notion_api

    # Override 없는 호출: 매번 prod 토큰으로 강제 reload (leak 방어)
    _notion_api = importlib.reload(_notion_api)
    if prod_token:
        _notion_api.NOTION_TOKEN = prod_token
    return _notion_api


def _read_bot_command_queue(queue_file=None):
    queue_path = queue_file or COMMAND_QUEUE_FILE
    if not os.path.exists(queue_path):
        return None

    try:
        with open(queue_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _assert_bot_command_queue_idle(queue_file=None):
    queued = _read_bot_command_queue(queue_file)
    if queued and queued.get('status') in {'pending', 'processing'}:
        raise RuntimeError('Another bot command is already running. Please wait a moment and try again.')


def _run_bot_command_and_wait(command_type, payload, *, timeout=120.0, poll_interval=0.5, queue_file=None):
    queue_path = queue_file or COMMAND_QUEUE_FILE
    _assert_bot_command_queue_idle(queue_path)

    command_id = f"{command_type}-{time.time()}"
    command = {
        "id": command_id,
        "type": command_type,
        "payload": payload,
        "status": "pending",
        "created_at": time.time(),
    }

    with open(queue_path, 'w', encoding='utf-8') as f:
        json.dump(command, f, ensure_ascii=False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        queued = _read_bot_command_queue(queue_path)
        if queued and queued.get('id') == command_id:
            status = queued.get('status')
            if status == 'completed':
                return queued.get('result') or {}
            if status == 'failed':
                raise RuntimeError(queued.get('error') or 'Bot command failed.')
        time.sleep(poll_interval)

    raise TimeoutError(f'Bot command timed out after {timeout:.0f}s: {command_type}')


def _load_dashboard_cache_data():
    try:
        import supabase_client

        sb_data = supabase_client.get_dashboard()
        if sb_data and (sb_data.get('members') or sb_data.get('submissions')):
            return sb_data
    except Exception as e:
        print(f"[WARN] Supabase read failed, falling back to file: {e}")

    data_file = os.path.join(BASE_DIR, 'dashboard_data.json')
    if os.path.exists(data_file):
        with open(data_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def _get_public_app_base_url():
    explicit = str(os.getenv('DASHBOARD_APP_BASE_URL', '')).strip()
    if explicit:
        return explicit.rstrip('/')
    proto = request.headers.get('X-Forwarded-Proto', request.scheme)
    host = request.headers.get('X-Forwarded-Host', request.host)
    return f'{proto}://{host}'


def _get_public_api_base_url():
    explicit = str(os.getenv('DASHBOARD_API_BASE_URL', '')).strip()
    if explicit:
        return explicit.rstrip('/')
    base = _get_public_app_base_url()
    host = request.headers.get('X-Forwarded-Host', request.host)
    if 'vercel.app' in host or host.startswith('localhost:3000'):
        return f'{base}/api-proxy'
    return base


def _build_app_redirect_url(path='/', **query_params):
    target = f"{_get_public_app_base_url()}{_sanitize_relative_path(path)}"
    for key, value in query_params.items():
        if value is None or value == '':
            continue
        target = _append_query_value(target, key, value)
    return target


def _append_query_value(path, key, value):
    sep = '&' if '?' in path else '?'
    return f'{path}{sep}{urlencode({key: value})}'


def _sanitize_relative_path(path):
    if not path or not str(path).startswith('/') or str(path).startswith('//'):
        return '/'
    return str(path)


def _discord_oauth_is_configured():
    return bool(_get_discord_client_id() and os.getenv('DISCORD_CLIENT_SECRET'))


def _is_truthy_env(value):
    if value is None:
        return False
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _is_test_personal_dashboard_enabled():
    if env_info.get('env_name') != 'test':
        return False
    raw_flag = os.getenv('ENABLE_TEST_PERSONAL_DASHBOARD')
    if raw_flag is None or not raw_flag.strip():
        return True
    return _is_truthy_env(raw_flag)


def _is_track_application_oauth_enabled():
    if env_info.get('env_name') == 'test':
        return True
    return _is_truthy_env(os.getenv('ENABLE_TRACK_APPLICATION_OAUTH'))


def _get_admin_discord_user_ids():
    """
    fallback 화이트리스트: ADMIN_DISCORD_USER_IDS (comma-separated 디스코드 user id) → set.
    역할 기반 체크가 실패해도 여기에 박혀있으면 admin 으로 인정.
    """
    raw = str(os.getenv('ADMIN_DISCORD_USER_IDS', '')).strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(',') if part.strip()}


def _get_admin_role_name():
    """운영진으로 인정할 디스코드 역할 이름. 기본 '운영자'."""
    return str(os.getenv('ADMIN_DISCORD_ROLE_NAME', '운영자')).strip() or '운영자'


def _get_admin_guild_id():
    """운영진 권한 체크 대상 길드 ID. test 모드면 TEST_DISCORD_GUILD_ID, 아니면 PROD."""
    if env_info.get('env_name') == 'test':
        return str(os.getenv('TEST_DISCORD_GUILD_ID', '')).strip() or None
    return str(os.getenv('PROD_DISCORD_GUILD_ID', '')).strip() or None


# ── 운영진 역할 ID 캐시 ─────────────────────────────────────
# Discord 길드의 역할 ID 는 거의 안 바뀌지만 운영진 역할 추가/이름 변경 시
# 빠르게 반영되어야 하므로 짧은 TTL.
_ADMIN_ROLE_CACHE_TTL = 30  # seconds — 5분 → 30초로 단축 (운영진 즉시 반영)
_admin_role_id_cache = {'role_id': None, 'fetched_at': 0.0, 'role_name': None}


def _resolve_admin_role_id():
    """길드의 '운영자' 역할 ID 조회 (bot token 사용, 캐시 TTL 30초)."""
    import time
    now = time.time()
    target_name = _get_admin_role_name()
    if (
        _admin_role_id_cache['role_id']
        and _admin_role_id_cache.get('role_name') == target_name
        and (now - _admin_role_id_cache['fetched_at']) < _ADMIN_ROLE_CACHE_TTL
    ):
        return _admin_role_id_cache['role_id']

    bot_token = str(os.getenv('DISCORD_BOT_TOKEN', '')).strip()
    guild_id = _get_admin_guild_id()
    if not bot_token or not guild_id:
        return None

    try:
        r = requests.get(
            f'https://discord.com/api/v10/guilds/{guild_id}/roles',
            headers={'Authorization': f'Bot {bot_token}'},
            timeout=10,
        )
        r.raise_for_status()
        roles = r.json() or []
        for role in roles:
            if role.get('name') == target_name:
                role_id = str(role.get('id') or '').strip()
                if role_id:
                    _admin_role_id_cache.update({
                        'role_id': role_id,
                        'fetched_at': now,
                        'role_name': target_name,
                    })
                    return role_id
        print(f"[WARN] Admin role '{target_name}' not found in guild {guild_id}")
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch guild roles: {e}")
    except Exception as e:
        print(f"[ERROR] Unexpected error resolving admin role: {e}")
    return None


def _fetch_user_role_ids(user_id):
    """user_id 가 길드에서 갖고 있는 역할 ID 목록 조회 (bot token 사용)."""
    bot_token = str(os.getenv('DISCORD_BOT_TOKEN', '')).strip()
    guild_id = _get_admin_guild_id()
    if not bot_token or not guild_id or not user_id:
        return []
    try:
        r = requests.get(
            f'https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}',
            headers={'Authorization': f'Bot {bot_token}'},
            timeout=10,
        )
        if r.status_code == 404:
            return []  # 길드에 멤버 없음
        r.raise_for_status()
        member = r.json() or {}
        return [str(rid) for rid in (member.get('roles') or [])]
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch member {user_id} roles: {e}")
        return []
    except Exception as e:
        print(f"[ERROR] Unexpected error fetching member roles: {e}")
        return []


def _is_admin_user(user_id):
    """
    운영진 판정 — 우선순위:
      1) ADMIN_DISCORD_USER_IDS env 화이트리스트 (역할 시스템 장애 시 비상 우회)
      2) 길드의 '운영자'(or ADMIN_DISCORD_ROLE_NAME) 역할 보유 여부
    """
    if not user_id:
        return False
    user_id_str = str(user_id).strip()
    if user_id_str in _get_admin_discord_user_ids():
        return True
    admin_role_id = _resolve_admin_role_id()
    if not admin_role_id:
        return False
    return admin_role_id in _fetch_user_role_ids(user_id_str)


def _is_admin_session():
    """현재 Flask 세션의 discord_user 가 admin 인지."""
    user = session.get('discord_user') or {}
    return _is_admin_user(user.get('id'))


def _is_oauth_enabled_for_path(path):
    safe_path = _sanitize_relative_path(path)
    if safe_path == TEST_PERSONAL_DASHBOARD_PATH:
        return _is_test_personal_dashboard_enabled()
    if safe_path in TRACK_APPLICATION_PATHS:
        return _is_track_application_oauth_enabled()
    return False


def _is_test_only_auth_path(path):
    return _sanitize_relative_path(path) == TEST_PERSONAL_DASHBOARD_PATH


def _build_auth_disabled_payload(path):
    safe_path = _sanitize_relative_path(path)
    if safe_path in TRACK_APPLICATION_PATHS:
        return {
            "authenticated": False,
            "oauthConfigured": _discord_oauth_is_configured(),
            "featureEnabled": False,
            "testOnly": False,
            "loginUrl": None,
            "message": "Discord OAuth for track applications is currently disabled.",
        }

    return {
        "authenticated": False,
        "oauthConfigured": _discord_oauth_is_configured(),
        "featureEnabled": False,
        "testOnly": True,
        "loginUrl": None,
        "message": "This personalized dashboard is available only in test mode or when ENABLE_TEST_PERSONAL_DASHBOARD is enabled.",
    }


def _fetch_discord_client_id_from_bot_token():
    if _DISCORD_CLIENT_ID_CACHE["resolved"]:
        return _DISCORD_CLIENT_ID_CACHE["value"]

    bot_token = str(os.getenv('DISCORD_BOT_TOKEN', '')).strip()
    if not bot_token:
        _DISCORD_CLIENT_ID_CACHE["resolved"] = True
        _DISCORD_CLIENT_ID_CACHE["value"] = None
        return None

    try:
        response = requests.get(
            f'{DISCORD_API_BASE}/oauth2/applications/@me',
            headers={'Authorization': f'Bot {bot_token}'},
            timeout=10,
        )
        response.raise_for_status()
        app_id = str((response.json() or {}).get('id', '')).strip() or None
        _DISCORD_CLIENT_ID_CACHE["resolved"] = True
        _DISCORD_CLIENT_ID_CACHE["value"] = app_id
        return app_id
    except requests.RequestException as e:
        print(f"[WARN] Failed to resolve Discord client ID from bot token: {e}")
        _DISCORD_CLIENT_ID_CACHE["resolved"] = True
        _DISCORD_CLIENT_ID_CACHE["value"] = None
        return None


def _get_discord_client_id():
    explicit = str(os.getenv('DISCORD_CLIENT_ID', '')).strip()
    if explicit:
        return explicit
    return _fetch_discord_client_id_from_bot_token()


def _get_discord_redirect_uri():
    explicit = os.getenv('DISCORD_REDIRECT_URI')
    if explicit:
        return explicit
    return f'{_get_public_api_base_url()}/api/auth/discord/callback'


def _build_discord_avatar_url(user_data):
    avatar = user_data.get('avatar')
    user_id = user_data.get('id')
    if avatar and user_id:
        return f'https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=128'
    return None


def _get_authenticated_discord_user():
    user = session.get('discord_user')
    if not user or not user.get('id'):
        return None
    return user


def _fetch_guild_nickname(user_id):
    """
    Discord REST API 로 길드 멤버의 서버별 nickname 조회.

    GET /guilds/{guild_id}/members/{user_id} → response.get('nick') 반환.
    Bot token 인증 (admin_server 가 이미 다른 API 호출에 사용 중).

    반환:
      - 사용자가 길드 멤버이고 서버 닉네임을 설정했으면 str (예: "오케이/ASC 커뮤니티 매니저")
      - 닉네임 없음 (글로벌 그대로 사용 케이스) → None
      - 길드 비멤버 (404) / 토큰 누락 / 네트워크 에러 → None (best-effort)

    실패 시 silent: 호출자가 None 폴백으로 글로벌 displayName 그대로 쓰면 됨.
    """
    user_id = str(user_id or '').strip()
    if not user_id:
        return None
    bot_token = str(os.getenv('DISCORD_BOT_TOKEN', '')).strip()
    guild_id = _get_admin_guild_id()
    if not bot_token or not guild_id:
        return None
    try:
        r = requests.get(
            f'https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}',
            headers={'Authorization': f'Bot {bot_token}'},
            timeout=10,
        )
        if r.status_code == 404:
            # 사용자가 해당 길드 멤버 아님 — 글로벌로 폴백.
            return None
        r.raise_for_status()
        data = r.json() or {}
        nick = data.get('nick')
        if isinstance(nick, str):
            nick = nick.strip()
            return nick or None
        return None
    except requests.RequestException as e:
        print(f"[WARN] _fetch_guild_nickname({user_id}) network error: {e}")
        return None
    except Exception as e:
        print(f"[WARN] _fetch_guild_nickname({user_id}) unexpected error: {e}")
        return None


def _refresh_session_discord_user(force=False, ttl_seconds=60):
    """
    Discord API 를 호출해서 session['discord_user'] 의 username / displayName /
    globalName / avatarUrl 을 최신값으로 갱신.

    - OAuth callback 에서 함께 저장한 session['discord_access_token'] 이 있어야 작동.
    - 옛 세션 (access_token 미저장) 은 그대로 두고 silent skip.
    - 동일 세션 내에서 ttl_seconds (기본 60s) 이내 호출은 캐시처럼 skip.
      신청서 제출(POST) 시점에는 force=True 로 무조건 갱신.
    - Discord 401 (token 만료/취소) → 갱신 포기. 다음 OAuth 까지 stale 유지.

    실패해도 예외 던지지 않음 — 갱신은 best-effort.
    """
    import time

    if not session.get('discord_user'):
        return
    access_token = session.get('discord_access_token')
    if not access_token:
        return

    now = time.time()
    if not force:
        last = session.get('discord_user_refreshed_at')
        if last and (now - float(last)) < ttl_seconds:
            return

    try:
        response = requests.get(
            f'{DISCORD_API_BASE}/users/@me',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        if response.status_code == 401:
            print('[INFO] Discord access token expired — skip refresh, wait for next OAuth')
            return
        response.raise_for_status()
        user_data = response.json() or {}
        existing = dict(session.get('discord_user') or {})
        # 안전장치: refresh 결과 user.id 가 다르면 세션 덮어쓰기 거부 (예상 못 한 케이스).
        new_id = str(user_data.get('id') or '').strip()
        if new_id and new_id != str(existing.get('id') or '').strip():
            print(f"[WARN] Discord refresh ID mismatch (session={existing.get('id')} vs api={new_id}) — keep session")
            return

        username = user_data.get('username') or existing.get('username') or ''
        global_name = user_data.get('global_name')

        # 서버별 닉네임 (guild nickname) 우선 사용 — 운영자가 길드에서 설정한 표시명.
        # 예: "오케이/ASC 커뮤니티 매니저" (global "케이" 보다 운영 컨텍스트 풍부).
        # 길드 비멤버거나 닉네임 미설정 시 None → 글로벌 폴백.
        guild_nick = _fetch_guild_nickname(new_id or existing.get('id'))

        existing.update({
            'username': username,
            # displayName 우선순위: guildNickname > globalName > username > 기존 값.
            'displayName': guild_nick or global_name or username or existing.get('displayName') or '',
            'globalName': global_name if global_name is not None else existing.get('globalName'),
            'guildNickname': guild_nick if guild_nick is not None else existing.get('guildNickname'),
            'avatarUrl': _build_discord_avatar_url(user_data) or existing.get('avatarUrl'),
        })
        session['discord_user'] = existing
        session['discord_user_refreshed_at'] = now
    except requests.RequestException as e:
        print(f'[WARN] Discord user refresh failed: {e}')
    except Exception as e:
        print(f'[WARN] Discord user refresh unexpected error: {e}')


def _get_admin_discord_ids():
    """
    운영진 Discord ID 집합. ADMIN_DISCORD_IDS (복수, 콤마구분) 우선,
    없으면 ADMIN_DISCORD_ID (단수) fallback. 매 호출마다 env 재조회 (운영진 추가 즉시 반영).
    """
    raw_multi = (os.getenv('ADMIN_DISCORD_IDS', '') or '').strip()
    raw_single = (os.getenv('ADMIN_DISCORD_ID', '') or '').strip()
    ids = set()
    for raw in (raw_multi, raw_single):
        for part in raw.split(','):
            part = part.strip()
            if part:
                ids.add(part)
    return ids


def _is_admin(discord_user):
    """주어진 인증된 user 가 운영진인지 확인."""
    if not discord_user:
        return False
    user_id = str(discord_user.get('id', '')).strip()
    return bool(user_id) and user_id in _get_admin_discord_ids()


def _get_current_cohort_label(raw_value=None):
    raw = str(raw_value or os.getenv('CURRENT_COHORT', '')).strip()
    if not raw:
        return '기수미정'
    return raw if raw.endswith('기') else f'{raw}기'


def _get_kst_now():
    return datetime.now(timezone(timedelta(hours=9)))


def _format_track_application_timestamp(dt=None):
    return (dt or _get_kst_now()).strftime('%m-%d %H:%M')


# ========== Cohort Config (기수·신청 기간·Today override) ==========
# /apply 페이지가 표시하는 기수 라벨, 신청 윈도우 시작/종료 날짜, Today 오버라이드 (데모용).
# 변경 시 즉시 반영, 클라이언트가 페이지 로드 시 GET 으로 가져옴.

_COHORT_CONFIG_LOCK = threading.Lock()
_COHORT_CONFIG_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _default_cohort_config():
    return {
        'cohortLabel': _get_current_cohort_label(),
        'applicationStartDate': '2026-05-20',
        'applicationEndDate': '2026-05-24',
        'todayOverride': None,  # null = 실제 KST today 사용
        'updatedAt': None,
    }


def _normalize_cohort_label(value):
    raw = str(value or '').strip()
    if not raw:
        return _get_current_cohort_label()
    return raw if raw.endswith('기') else f'{raw}기'


def _validate_iso_date(value):
    raw = str(value or '').strip()
    if not raw or not _COHORT_CONFIG_DATE_RE.match(raw):
        return None
    try:
        datetime.strptime(raw, '%Y-%m-%d')
        return raw
    except ValueError:
        return None


def _read_cohort_config():
    with _COHORT_CONFIG_LOCK:
        if not os.path.exists(COHORT_CONFIG_FILE):
            return _default_cohort_config()
        try:
            with open(COHORT_CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return _default_cohort_config()
            base = _default_cohort_config()
            base.update({k: v for k, v in data.items() if k in base})
            base['cohortLabel'] = _normalize_cohort_label(base.get('cohortLabel'))
            return base
        except Exception as e:
            print(f"[WARN] Failed to read cohort config: {e}")
            return _default_cohort_config()


def _write_cohort_config(data):
    with _COHORT_CONFIG_LOCK:
        payload = _default_cohort_config()
        payload.update(data or {})
        payload['cohortLabel'] = _normalize_cohort_label(payload.get('cohortLabel'))
        payload['updatedAt'] = _get_kst_now().isoformat()
        try:
            with open(COHORT_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return payload
        except Exception as e:
            print(f"[ERROR] Failed to write cohort config: {e}")
            return None


@app.route('/api/cohort-config', methods=['GET'])
def get_cohort_config_route():
    # 🔧 todayOverride 는 모든 사용자에게 노출 (2026-05-08 revert).
    #   직전 (commit fb5f657) 에서 admin 전용으로 마스킹했는데, 이는 admin 의 demo 의도
    #   (예: TODAY=2026-05-23 으로 closed 미리보기) 를 모바일 사용자에게는 보여주지
    #   못하게 함. 사용자 피드백: '모바일 시크릿 페이지로 다시 로그인하니 아직 TODAY가
    #   5월 8일로 되어있음'.
    #   직전 footgun (CTA mode 본 후 로그인 → 다시 upcoming) 은 클라이언트 realPastOpen
    #   가드 (track-apply.html _evaluateViewerStateFromConfig) 가 이미 처리함.
    return jsonify(_read_cohort_config()), 200


def _prune_multi_select_options(notion_api, db_id, prop_name, canonical_names):
    """
    Notion DB 의 multi_select 속성 옵션을 canonical_names 만 남기고 정리.
    반환: { 'kept': [...], 'removed': [...], 'missing': True/False, 'error': str|None }
    """
    if not db_id or not prop_name:
        return {'kept': [], 'removed': [], 'missing': True, 'error': 'db_id or prop_name missing'}
    try:
        db = notion_api.get_database(db_id)
    except Exception as e:
        return {'kept': [], 'removed': [], 'missing': False, 'error': f'get_database failed: {e}'}
    if not db:
        return {'kept': [], 'removed': [], 'missing': True, 'error': 'database not found'}

    prop = (db.get('properties') or {}).get(prop_name)
    if not prop or prop.get('type') != 'multi_select':
        return {'kept': [], 'removed': [], 'missing': True, 'error': f"property '{prop_name}' not multi_select"}

    options = prop.get('multi_select', {}).get('options', []) or []
    kept = [opt for opt in options if opt.get('name') in canonical_names]
    removed = [opt for opt in options if opt.get('name') not in canonical_names]

    if not removed:
        return {
            'kept': [opt.get('name') for opt in kept],
            'removed': [],
            'missing': False,
            'error': None,
        }

    # PATCH database with reduced options list. Notion 은 제거된 옵션을 schema 에서 삭제하지만
    # 이미 그 옵션을 갖고 있던 page 의 값은 그대로 유지된다.
    import requests as _requests
    url = f"https://api.notion.com/v1/databases/{db_id}"
    headers = notion_api.get_headers() if hasattr(notion_api, 'get_headers') else None
    if not headers:
        try:
            from notion_api import get_headers as _gh
            headers = _gh()
        except Exception as e:
            return {'kept': [], 'removed': [], 'missing': False, 'error': f'get_headers unavailable: {e}'}
    payload = {
        "properties": {
            prop_name: {
                "multi_select": {"options": [{"name": opt['name']} for opt in kept]}
            }
        }
    }
    try:
        resp = _requests.patch(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200:
            return {
                'kept': [opt.get('name') for opt in kept],
                'removed': [],
                'missing': False,
                'error': f'PATCH failed: {resp.status_code} {resp.text[:200]}',
            }
    except Exception as e:
        return {'kept': [opt.get('name') for opt in kept], 'removed': [], 'missing': False, 'error': f'PATCH exception: {e}'}

    return {
        'kept': [opt.get('name') for opt in kept],
        'removed': [opt.get('name') for opt in removed],
        'missing': False,
        'error': None,
    }


@app.route('/api/admin/notion/track-options', methods=['GET'])
def get_notion_track_options_route():
    """현재 Notion DB 들의 트랙 옵션 + canonical 비교 리포트 (preview 용)."""
    user = _get_authenticated_discord_user()
    if not user or not _is_admin_user(user.get('id')):
        return jsonify({'status': 'error', 'message': 'admin only'}), 403

    canonical = sorted(_canonical_track_names())

    targets = [
        {
            'label': '멤버 마스터 DB · 트랙',
            'db_id': os.getenv('GROUP_PREVIEW_TEST_MEMBER_DB_ID') or os.getenv('TRACK_JO_DB_ID'),
            'prop_name': '트랙',
        },
        {
            'label': '트랙 마스터 DB · 트랙명',
            'db_id': os.getenv('GROUP_PREVIEW_TEST_GROUP_DB_ID') or os.getenv('GROUP_DB_ID'),
            'prop_name': '트랙명',
        },
    ]

    canonical_set = set(canonical)
    try:
        from notion_api import get_database as _get_db
    except Exception:
        return jsonify({'status': 'error', 'message': 'notion_api unavailable'}), 500

    report = []
    for tgt in targets:
        db_id = tgt['db_id']
        if not db_id:
            report.append({'label': tgt['label'], 'dbId': None, 'kept': [], 'extras': [], 'error': 'db env missing'})
            continue
        try:
            db = _get_db(db_id)
        except Exception as e:
            report.append({'label': tgt['label'], 'dbId': db_id, 'kept': [], 'extras': [], 'error': str(e)})
            continue
        if not db:
            report.append({'label': tgt['label'], 'dbId': db_id, 'kept': [], 'extras': [], 'error': 'db not found'})
            continue
        prop = (db.get('properties') or {}).get(tgt['prop_name'])
        if not prop or prop.get('type') != 'multi_select':
            report.append({'label': tgt['label'], 'dbId': db_id, 'kept': [], 'extras': [], 'error': f"prop '{tgt['prop_name']}' missing or not multi_select"})
            continue
        options = [opt.get('name') for opt in (prop.get('multi_select', {}).get('options') or [])]
        kept = [n for n in options if n in canonical_set]
        extras = [n for n in options if n not in canonical_set]
        report.append({'label': tgt['label'], 'dbId': db_id, 'propName': tgt['prop_name'], 'kept': kept, 'extras': extras, 'error': None})

    return jsonify({'canonical': canonical, 'targets': report}), 200


@app.route('/api/admin/notion/prune-track-options', methods=['POST'])
def prune_notion_track_options_route():
    """Canonical 외의 트랙 옵션을 Notion 에서 제거. 옵션이 페이지에 이미 있으면 페이지 값은 유지됨."""
    user = _get_authenticated_discord_user()
    if not user or not _is_admin_user(user.get('id')):
        return jsonify({'status': 'error', 'message': 'admin only'}), 403

    canonical = _canonical_track_names()
    try:
        import notion_api as _napi
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'notion_api import failed: {e}'}), 500

    targets = [
        {'label': '멤버 마스터 DB · 트랙', 'db_id': os.getenv('GROUP_PREVIEW_TEST_MEMBER_DB_ID') or os.getenv('TRACK_JO_DB_ID'), 'prop_name': '트랙'},
        {'label': '트랙 마스터 DB · 트랙명', 'db_id': os.getenv('GROUP_PREVIEW_TEST_GROUP_DB_ID') or os.getenv('GROUP_DB_ID'), 'prop_name': '트랙명'},
    ]

    results = []
    for tgt in targets:
        result = _prune_multi_select_options(_napi, tgt['db_id'], tgt['prop_name'], canonical)
        results.append({
            'label': tgt['label'],
            'dbId': tgt['db_id'],
            'propName': tgt['prop_name'],
            **result,
        })
    return jsonify({'canonical': sorted(canonical), 'results': results}), 200


@app.route('/api/cohort-config', methods=['PUT'])
def put_cohort_config_route():
    user = _get_authenticated_discord_user()
    if not user or not _is_admin_user(user.get('id')):
        return jsonify({'status': 'error', 'message': 'admin only'}), 403

    body = request.get_json(silent=True) or {}
    current = _read_cohort_config()

    new_label = _normalize_cohort_label(body.get('cohortLabel') or current['cohortLabel'])

    new_start = _validate_iso_date(body.get('applicationStartDate'))
    new_end = _validate_iso_date(body.get('applicationEndDate'))
    if body.get('applicationStartDate') is not None and not new_start:
        return jsonify({'status': 'error', 'message': 'invalid applicationStartDate (YYYY-MM-DD)'}), 400
    if body.get('applicationEndDate') is not None and not new_end:
        return jsonify({'status': 'error', 'message': 'invalid applicationEndDate (YYYY-MM-DD)'}), 400
    new_start = new_start or current['applicationStartDate']
    new_end = new_end or current['applicationEndDate']
    if new_start > new_end:
        return jsonify({'status': 'error', 'message': 'applicationStartDate must be <= applicationEndDate'}), 400

    today_raw = body.get('todayOverride', current.get('todayOverride'))
    if today_raw in (None, '', 'null'):
        today_override = None
    else:
        today_override = _validate_iso_date(today_raw)
        if not today_override:
            return jsonify({'status': 'error', 'message': 'invalid todayOverride (YYYY-MM-DD or null)'}), 400

    saved = _write_cohort_config({
        'cohortLabel': new_label,
        'applicationStartDate': new_start,
        'applicationEndDate': new_end,
        'todayOverride': today_override,
    })
    if not saved:
        return jsonify({'status': 'error', 'message': 'persist failed'}), 500

    # 🆕 cohort 라벨이 바뀌면 트랙 신청 캐시(실+mock) 자동 초기화.
    # 의도: 9기 → 10기 전환 시 운영진이 "왜 새 기수에 데이터가 그대로 있지?" 라고
    # 묻는 footgun 방지. 데이터는 cohort_label 별 bucket 으로 분리되어 있긴 하지만
    # 같은 라벨로 mock fill 했던 이력이 잔존하는 등 표시상 혼란이 생김.
    # 정책: 라벨 변경 = '새 기수 시작' 으로 간주, 모든 cohorts bucket 비움.
    # (운영자가 별도 export 후 라벨 바꾸는 워크플로 가정 — 9기 archive 는 Notion 永속.)
    cohort_reset_summary = None
    old_label = (current.get('cohortLabel') or '').strip()
    if old_label and old_label != new_label:
        try:
            cleared_real = 0
            existing_real = _read_track_application_cache() or {}
            for _, bucket in (existing_real.get('cohorts') or {}).items():
                apps = (bucket or {}).get('applications') or {}
                cleared_real += len(apps) if isinstance(apps, dict) else 0
            _write_track_application_cache_file(TRACK_APPLICATION_CACHE_FILE, {'cohorts': {}})

            cleared_mock = 0
            existing_mock = _read_track_application_admin_mock_cache() or {}
            for _, bucket in (existing_mock.get('cohorts') or {}).items():
                members = (bucket or {}).get('members') or []
                cleared_mock += len(members) if isinstance(members, list) else 0
            _write_track_application_admin_mock_cache({'cohorts': {}})

            cohort_reset_summary = {
                'previousCohort': old_label,
                'newCohort': new_label,
                'clearedRealApplications': cleared_real,
                'clearedMockMembers': cleared_mock,
            }
            print(f"[INFO] cohort label changed {old_label} -> {new_label}, "
                  f"cleared {cleared_real} real apps + {cleared_mock} mock members")
        except Exception as e:
            print(f"[ERROR] failed to clear caches on cohort change: {e}")

    response = dict(saved)
    if cohort_reset_summary:
        response['cohortReset'] = cohort_reset_summary
    return jsonify(response), 200


def _default_track_application_cache():
    return {
        "version": 1,
        "cohorts": {},
        "updatedAt": None,
    }


def _read_track_application_cache_file(cache_file, warning_label):
    if not os.path.exists(cache_file):
        return _default_track_application_cache()

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_track_application_cache()
        data.setdefault('version', 1)
        data.setdefault('cohorts', {})
        data.setdefault('updatedAt', None)
        if not isinstance(data.get('cohorts'), dict):
            data['cohorts'] = {}
        return data
    except Exception as e:
        print(f"[WARN] Failed to read {warning_label}: {e}")
        return _default_track_application_cache()


def _write_track_application_cache_file(cache_file, data):
    payload = dict(data or {})
    payload['version'] = 1
    payload['updatedAt'] = _get_kst_now().isoformat()
    payload.setdefault('cohorts', {})
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read_track_application_cache():
    return _read_track_application_cache_file(
        TRACK_APPLICATION_CACHE_FILE,
        'track application cache',
    )


def _write_track_application_cache(data):
    _write_track_application_cache_file(TRACK_APPLICATION_CACHE_FILE, data)


def _read_track_application_admin_mock_cache():
    return _read_track_application_cache_file(
        TRACK_APPLICATION_ADMIN_MOCK_CACHE_FILE,
        'track application admin mock cache',
    )


def _write_track_application_admin_mock_cache(data):
    _write_track_application_cache_file(TRACK_APPLICATION_ADMIN_MOCK_CACHE_FILE, data)


def _infer_member_initials(name='', handle='', fallback=''):
    source = str(name or '').strip() or str(handle or '').strip().lstrip('@') or str(fallback or '').strip()
    if not source:
        return '??'
    compact = ''.join(ch for ch in source if not ch.isspace())
    if not compact:
        return '??'
    return compact[:2].upper()


def _sanitize_track_application_track(track):
    if not isinstance(track, dict):
        return None

    track_type = str(track.get('type') or '').strip().lower()
    track_id = str(track.get('id') or '').strip()
    if track_type not in {'weekday', 'light'} or not track_id:
        return None

    sanitized = {
        "type": track_type,
        "id": track_id,
    }
    if track_type == 'weekday':
        sanitized['leader'] = bool(track.get('leader'))
        creator_sub = str(track.get('creatorSub') or '').strip()
        if creator_sub:
            sanitized['creatorSub'] = creator_sub
    return sanitized


def _sanitize_track_application_member(raw_member):
    if not isinstance(raw_member, dict):
        return None

    user_id = str(raw_member.get('userId') or raw_member.get('id') or '').strip()
    if not user_id:
        return None

    name = (
        str(raw_member.get('name') or '').strip()
        or str(raw_member.get('displayName') or '').strip()
        or str(raw_member.get('username') or '').strip()
        or user_id
    )
    handle = str(raw_member.get('handle') or '').strip()
    if handle and not handle.startswith('@'):
        handle = f'@{handle}'

    sanitized_tracks = []
    for track in raw_member.get('tracks') or []:
        normalized = _sanitize_track_application_track(track)
        if normalized:
            sanitized_tracks.append(normalized)

    return {
        "id": user_id,
        "userId": user_id,
        "name": name,
        "handle": handle,
        "initials": str(raw_member.get('initials') or '').strip() or _infer_member_initials(name, handle, user_id),
        "avatarUrl": raw_member.get('avatarUrl'),
        "tracks": sanitized_tracks,
        "submitted": raw_member.get('submitted'),
        "submittedAt": raw_member.get('submittedAt'),
        "edits": int(raw_member.get('edits') or 0),
        "notes": str(raw_member.get('notes') or '').strip(),
    }


def _build_track_application_record(discord_user, payload, existing=None):
    existing = existing or {}
    submitted_at = _get_kst_now()
    user_id = str(discord_user.get('id') or '').strip()
    username = str(discord_user.get('username') or '').strip()
    # Priority: session(Discord API) > payload(client JS cache) > existing > fallback.
    # 이전 동작: payload 가 우선이라 클라가 옛 닉네임을 보내면 그대로 stale 저장됨.
    # session 은 OAuth callback 또는 _refresh_session_discord_user 로 최신화돼 있어 신뢰 가능.
    display_name = (
        str(discord_user.get('displayName') or '').strip()
        or str(discord_user.get('globalName') or '').strip()
        or username
        or str(payload.get('displayName') or '').strip()
        or str(existing.get('name') or '').strip()
        or user_id
    )
    handle = (f'@{username}' if username else '') or str(payload.get('handle') or '').strip()
    if handle and not handle.startswith('@'):
        handle = f'@{handle}'

    tracks = []
    seen = set()
    for track in payload.get('tracks') or []:
        normalized = _sanitize_track_application_track(track)
        if not normalized:
            continue
        track_key = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        if track_key in seen:
            continue
        seen.add(track_key)
        tracks.append(normalized)

    previous_submitted = bool(existing.get('submitted'))
    edits = int(existing.get('edits') or 0) + (1 if previous_submitted else 0)

    return {
        "id": user_id,
        "userId": user_id,
        "name": display_name,
        "handle": handle,
        "initials": _infer_member_initials(display_name, handle, user_id),
        "avatarUrl": payload.get('avatarUrl') or discord_user.get('avatarUrl'),
        "tracks": tracks,
        "submitted": _format_track_application_timestamp(submitted_at),
        "submittedAt": submitted_at.isoformat(),
        "edits": edits,
        "notes": str(payload.get('notes') or '').strip(),
    }


def _build_admin_track_application_record(raw_member, existing=None):
    existing = _sanitize_track_application_member(existing or {}) or {}
    normalized = _sanitize_track_application_member(raw_member)
    if not normalized:
        return None

    submitted = normalized.get('submitted') or existing.get('submitted')
    submitted_at = normalized.get('submittedAt') or existing.get('submittedAt')
    tracks = list(normalized.get('tracks') or [])

    if tracks and not submitted:
        now = _get_kst_now()
        submitted = _format_track_application_timestamp(now)
        submitted_at = submitted_at or now.isoformat()
    elif tracks and not submitted_at:
        submitted_at = _get_kst_now().isoformat()
    elif not tracks:
        submitted = None
        submitted_at = None

    name = normalized.get('name') or existing.get('name') or normalized['id']
    handle = normalized.get('handle') or existing.get('handle') or ''
    avatar_url = normalized.get('avatarUrl') or existing.get('avatarUrl')

    return {
        "id": normalized['id'],
        "userId": normalized['id'],
        "name": name,
        "handle": handle,
        "initials": normalized.get('initials') or existing.get('initials') or _infer_member_initials(name, handle, normalized['id']),
        "avatarUrl": avatar_url,
        "tracks": tracks,
        "submitted": submitted,
        "submittedAt": submitted_at,
        "edits": int(normalized.get('edits') or existing.get('edits') or 0),
        "notes": normalized.get('notes') or existing.get('notes') or '',
    }


def _get_track_application_records_for_cohort(cohort_label):
    cache = _read_track_application_cache()
    cohort_records = (
        cache.get('cohorts', {})
        .get(cohort_label, {})
        .get('applications', {})
    )
    return cohort_records if isinstance(cohort_records, dict) else {}


def _get_track_application_admin_mock_members_for_cohort(cohort_label):
    cache = _read_track_application_admin_mock_cache()
    raw_members = (
        cache.get('cohorts', {})
        .get(cohort_label, {})
        .get('members', [])
    )
    if not isinstance(raw_members, list):
        return []

    members = []
    for raw_member in raw_members:
        normalized = _sanitize_track_application_member(raw_member)
        if normalized:
            members.append(normalized)
    return members


def _find_viewer_track_application_member(cohort_label, discord_user):
    if not isinstance(discord_user, dict):
        return None

    records = _get_track_application_records_for_cohort(cohort_label)
    if not records:
        return None

    viewer_id = str(discord_user.get('id') or '').strip()
    if viewer_id:
        direct = _sanitize_track_application_member(records.get(viewer_id))
        if direct:
            return direct

    viewer_handle = str(discord_user.get('username') or '').strip().lower()
    if viewer_handle and not viewer_handle.startswith('@'):
        viewer_handle = f'@{viewer_handle}'

    for raw_member in records.values():
        normalized = _sanitize_track_application_member(raw_member)
        if not normalized:
            continue
        member_id = str(normalized.get('userId') or normalized.get('id') or '').strip()
        if viewer_id and member_id == viewer_id:
            return normalized
        member_handle = str(normalized.get('handle') or '').strip().lower()
        if viewer_handle and member_handle == viewer_handle:
            return normalized

    return None


def _build_track_application_member_list(cohort_label):
    members = []
    for _, raw_member in _get_track_application_records_for_cohort(cohort_label).items():
        normalized = _sanitize_track_application_member(raw_member)
        if not normalized:
            continue
        members.append(normalized)

    members.sort(
        key=lambda item: (
            0 if item.get('tracks') else 1,
            item.get('submittedAt') or '',
            item.get('name') or item.get('handle') or item.get('id') or '',
        ),
        reverse=False,
    )
    return members


TRACK_APPLICATION_WEEKDAY_TRACK_MAP = {
    # 9기 개편 (2026-05-18): self_inquiry 월→수 이동, ai_agent 수→화 이동.
    # 노션 트랙신청 DB 의 요일별 select property 에 어떤 요일 슬롯으로 값 넣을지 결정.
    'sales_real': ('monday', '세일즈 실전 트랙'),
    'ai_agent': ('tuesday', 'AI 에이전트 트랙'),
    'self_inquiry': ('wednesday', '나 탐구 트랙'),
    'creator': ('wednesday', '크리에이터 트랙'),
    'app_dev': ('thursday', '앱 개발 트랙'),
    # 빌더 정규 (advanced/basic) — 9기 폼에선 선택 불가. legacy 데이터 (이전 기수 신청자) 호환을
    # 위해 매핑은 유지 (화요일 슬롯). 7월 재오픈 시 그대로 재활용 가능.
    'builder_advanced': ('tuesday', '빌더 심화 트랙'),
    'builder_basic': ('tuesday', '빌더 기초 트랙'),
}

TRACK_APPLICATION_LIGHT_TRACK_MAP = {
    'creator_light_short': '크리에이터 라이트 트랙 (숏폼)',
    'creator_light_long': '크리에이터 라이트 트랙 (롱폼)',
    'builder_light_basic': '빌더 라이트 트랙 (기초)',
    'builder_light_adv': '빌더 라이트 트랙 (심화)',
}

TRACK_APPLICATION_LEADER_LABELS = {
    '세일즈 실전 트랙',
    '나 탐구 트랙',
    '빌더 심화 트랙',
    '빌더 기초 트랙',
    # 🔧 '크리에이터 트랙' 은 Notion 트랙 신청서 DB 옵션에서 제거됨 (2026-05-08).
    #   숏폼/롱폼 sub-form 라벨만 leader 자격 인정.
    '크리에이터 숏폼 트랙',
    '크리에이터 롱폼 트랙',
    'AI 에이전트 트랙',
    '앱 개발 트랙',
}

TRACK_APPLICATION_CREATOR_SUB_MAP = {
    'short_only': '숏폼만',
    'short_long': '숏폼 + 롱폼',
}


# Notion '트랙' / '트랙명' multi_select 에 들어가야 할 정식 트랙명 화이트리스트.
# 그 외는 legacy/orphan 으로 간주하고 prune 대상.
#
# '크리에이터 트랙' 은 parent (조 배정 / Discord 역할 단위), 그 안에 숏폼·롱폼 sub-track 이 존재.
# 멤버 마스터 DB '트랙' 옵션에는 sub-track 도 별개 옵션으로 남아있어야 한다 (멤버별 어느 폼인지 표기).
TRACK_APPLICATION_CREATOR_SUB_TRACK_LABELS = {
    '크리에이터 숏폼 트랙',
    '크리에이터 롱폼 트랙',
}


def _canonical_track_names():
    names = set()
    for _, label in TRACK_APPLICATION_WEEKDAY_TRACK_MAP.values():
        names.add(label)
    for label in TRACK_APPLICATION_LIGHT_TRACK_MAP.values():
        names.add(label)
    names |= TRACK_APPLICATION_CREATOR_SUB_TRACK_LABELS
    return names


def _normalize_track_application_cohort_value(cohort_label):
    raw = str(cohort_label or '').strip()
    if not raw:
        return ''
    return raw[:-1] if raw.endswith('기') else raw


def _extract_track_application_submission(record):
    weekdays = {
        'monday': None,
        'tuesday': None,
        'wednesday': None,
        'thursday': None,
    }
    light_tracks = []
    leader_labels = []
    creator_sub = None

    for raw_track in record.get('tracks') or []:
        track = _sanitize_track_application_track(raw_track)
        if not track:
            continue

        if track.get('type') == 'weekday':
            weekday_info = TRACK_APPLICATION_WEEKDAY_TRACK_MAP.get(track.get('id'))
            if not weekday_info:
                continue
            day_key, label = weekday_info
            # 🔧 크리에이터 트랙 — Notion 수요일 트랙 select 옵션을 '크리에이터 숏폼 트랙' /
            #   '크리에이터 롱폼 트랙' 둘만 남기는 정책으로 변경 (2026-05-08).
            #   parent '크리에이터 트랙' 옵션 제거됨 → 어떤 경우에도 둘 중 하나로만 매핑.
            #   - short_long → '크리에이터 롱폼 트랙' (롱폼까지 하는 케이스를 더 명시적으로 표시)
            #   - short_only / 미설정 / 기타 → '크리에이터 숏폼 트랙' (shorts 가 baseline)
            #   creatorSub 자체는 notes 에 계속 백업 ('숏폼만' / '숏폼 + 롱폼').
            if track.get('id') == 'creator':
                creator_sub_id = str(track.get('creatorSub') or '').strip()
                if creator_sub_id == 'short_long':
                    label = '크리에이터 롱폼 트랙'
                else:
                    label = '크리에이터 숏폼 트랙'
                creator_sub = TRACK_APPLICATION_CREATOR_SUB_MAP.get(creator_sub_id) or creator_sub
            weekdays[day_key] = label
            if track.get('leader') and label in TRACK_APPLICATION_LEADER_LABELS:
                leader_labels.append(label)
        elif track.get('type') == 'light':
            label = TRACK_APPLICATION_LIGHT_TRACK_MAP.get(track.get('id'))
            if label and label not in light_tracks:
                light_tracks.append(label)

    return {
        'weekdays': weekdays,
        'lightTracks': light_tracks,
        'leaderLabels': leader_labels,
        'creatorSub': creator_sub,
    }


def _resolve_track_application_db_fields(db_obj):
    properties = (db_obj or {}).get('properties', {})
    return {
        'title': _pick_db_property(properties, 'title', ['이름', 'Name']),
        # `사용자 ID` — Discord snowflake (numeric). 디스코드 ID(핸들) 와 별개의 unique
        # identifier. 핸들은 사용자가 바꿀 수 있지만 snowflake 는 영구. 매칭/조회 시
        # 더 신뢰할 수 있는 키 (특히 master DB 미등록 신청자도 보존됨).
        'user_id': _pick_db_property(properties, 'rich_text', ['사용자 ID', 'User ID']),
        'discord_id': _pick_db_property(properties, 'rich_text', ['디스코드 ID', 'Discord ID', 'Handle']),
        'discord_nickname': _pick_db_property(properties, 'rich_text', ['디스코드 닉네임', 'Discord Nickname', 'Display Name']),
        'cohort': _pick_db_property(properties, 'rich_text', ['기수', 'Cohort']),
        'submitted_at': _pick_db_property(properties, 'date', ['신청 날짜', 'Submitted At']),
        'monday_track': _pick_db_property(properties, 'select', ['월요일 트랙', 'Monday Track']),
        'tuesday_track': _pick_db_property(properties, 'select', ['화요일 트랙', 'Tuesday Track']),
        'wednesday_track': _pick_db_property(properties, 'select', ['수요일 트랙', 'Wednesday Track']),
        'thursday_track': _pick_db_property(properties, 'select', ['목요일 트랙', 'Thursday Track']),
        'light_tracks': _pick_db_property(properties, 'multi_select', ['라이트 트랙', 'Light Track']),
        'creator_light_checkbox': _pick_db_property(
            properties,
            'checkbox',
            ['[선택] 라이트 트랙 (* 재참여자 Only) (크리에이터 라이트 트랙)'],
        ),
        'builder_light_checkbox': _pick_db_property(
            properties,
            'checkbox',
            ['[선택] 라이트 트랙 (* 재참여자 Only) (빌더 트랙)'],
        ),
        # leader_apply 는 select 또는 multi_select 둘 다 지원.
        # 운영 정책상 한 멤버가 여러 트랙 조장을 동시 지원할 수 있으면 multi_select 권장.
        # _build_track_application_notion_properties 에서 leader_apply_type 으로 분기.
        'leader_apply': (
            _pick_db_property(properties, 'multi_select', ['조장 지원 여부', 'Leader Apply'])
            or _pick_db_property(properties, 'select', ['조장 지원 여부', 'Leader Apply'])
        ),
        'leader_apply_type': (
            'multi_select' if _pick_db_property(properties, 'multi_select', ['조장 지원 여부', 'Leader Apply'])
            else ('select' if _pick_db_property(properties, 'select', ['조장 지원 여부', 'Leader Apply']) else None)
        ),
        'notes': _pick_db_property(
            properties,
            'rich_text',
            # 운영자가 노션 DB 컬럼명을 어떻게 정해도 매핑되도록 후보 확장.
            # '기타' 가 정식 컬럼명. 나머지는 legacy / 별칭.
            ['기타', '기타사항', '메모', 'Notes', 'ASC 9기 활동에서 기대하는 점', '기대하는 점'],
        ),
        'processed': _pick_db_property(properties, 'checkbox', ['봇 처리 완료', 'Processed']),
        'handled': _pick_db_property(properties, 'checkbox', ['조치 여부', 'Handled']),
        'group': _pick_db_property(properties, 'select', ['조', 'Group']),
        'member_relation': _pick_db_property(properties, 'relation', ['멤버 마스터 DB', 'Member Master DB']),
        'cohort_relation': _pick_db_property(properties, 'relation', ['기수 마스터 DB', 'Cohort Master DB']),
    }


def _find_page_in_relation_db_by_cohort(notion_client, db_id, cohort_label):
    normalized_targets = []
    for candidate in [str(cohort_label or '').strip(), _normalize_track_application_cohort_value(cohort_label)]:
        if candidate and candidate not in normalized_targets:
            normalized_targets.append(candidate)

    if not db_id or not normalized_targets:
        return None

    db_obj = notion_client.get_database(db_id)
    if not db_obj:
        return None

    properties = db_obj.get('properties', {})
    title_prop = _pick_db_property(properties, 'title', ['이름', '기수', 'Name', 'Title'])
    rich_text_prop = _pick_db_property(properties, 'rich_text', ['기수', 'Cohort', '이름'])

    filters = []
    if title_prop:
        for target in normalized_targets:
            filters.append({"property": title_prop, "title": {"equals": target}})
            filters.append({"property": title_prop, "title": {"contains": target}})
    if rich_text_prop:
        for target in normalized_targets:
            filters.append({"property": rich_text_prop, "rich_text": {"equals": target}})
            filters.append({"property": rich_text_prop, "rich_text": {"contains": target}})

    if not filters:
        return None

    payload = {
        "page_size": 1,
        "filter": filters[0] if len(filters) == 1 else {"or": filters},
    }
    pages = notion_client.fetch_all_pages(
        f'https://api.notion.com/v1/databases/{db_id}/query',
        payload,
    )
    return pages[0] if pages else None


def _find_existing_track_application_page(notion_client, db_id, fields, member, cohort_label, member_page_id=None):
    filters = []
    identifier_filters = []

    handle = str(member.get('handle') or '').strip()
    name = str(member.get('name') or '').strip()

    if member_page_id and fields.get('member_relation'):
        identifier_filters.append({
            "property": fields['member_relation'],
            "relation": {"contains": member_page_id},
        })
    if handle and fields.get('discord_id'):
        identifier_filters.append({
            "property": fields['discord_id'],
            "rich_text": {"equals": handle},
        })
    if name and fields.get('title'):
        identifier_filters.append({
            "property": fields['title'],
            "title": {"equals": name},
        })

    if not identifier_filters:
        return None

    filters.append(identifier_filters[0] if len(identifier_filters) == 1 else {"or": identifier_filters})

    cohort_targets = []
    for candidate in [str(cohort_label or '').strip(), _normalize_track_application_cohort_value(cohort_label)]:
        if candidate and candidate not in cohort_targets:
            cohort_targets.append(candidate)
    if cohort_targets and fields.get('cohort'):
        cohort_filters = [
            {"property": fields['cohort'], "rich_text": {"equals": target}}
            for target in cohort_targets
        ]
        filters.append(cohort_filters[0] if len(cohort_filters) == 1 else {"or": cohort_filters})

    payload = {
        "page_size": 1,
        "filter": filters[0] if len(filters) == 1 else {"and": filters},
    }
    pages = notion_client.fetch_all_pages(
        f'https://api.notion.com/v1/databases/{db_id}/query',
        payload,
    )
    return pages[0] if pages else None


def _build_track_application_notion_properties(
    notion_client,
    db_id,
    fields,
    record,
    cohort_label,
    existing_page=None,
):
    submission = _extract_track_application_submission(record)
    properties = {}

    member_page_id = None
    cohort_page_id = None

    try:
        if db_id:
            db_obj = notion_client.get_database(db_id)
            relation_fields = (db_obj or {}).get('properties', {})
            member_relation_name = fields.get('member_relation')
            cohort_relation_name = fields.get('cohort_relation')
            member_relation_db_id = (
                relation_fields.get(member_relation_name, {}).get('relation', {}).get('database_id')
                if member_relation_name else None
            )
            cohort_relation_db_id = (
                relation_fields.get(cohort_relation_name, {}).get('relation', {}).get('database_id')
                if cohort_relation_name else None
            )

            if member_relation_db_id:
                member_db = notion_client.get_database(member_relation_db_id)
                member_fields = _resolve_member_db_fields(member_db or {})
                member_page = _find_existing_member_page(
                    notion_client,
                    member_relation_db_id,
                    member_fields,
                    record,
                )
                if member_page:
                    member_page_id = member_page.get('id')

            if cohort_relation_db_id:
                cohort_page = _find_page_in_relation_db_by_cohort(
                    notion_client,
                    cohort_relation_db_id,
                    cohort_label,
                )
                if cohort_page:
                    cohort_page_id = cohort_page.get('id')
    except Exception as e:
        print(f"[WARN] Failed to resolve track application relations: {e}")

    if fields.get('title'):
        properties[fields['title']] = {
            "title": [{"text": {"content": str(record.get('name') or record.get('userId') or 'unknown')[:2000]}}]
        }
    if fields.get('user_id'):
        # Discord snowflake (numeric) — 신청자 본인의 영구 unique identifier.
        # 코드는 record['userId'] 에 OAuth 세션의 discord_user.get('id') 를 그대로 담는다
        # (_build_track_application_record / _build_admin_track_application_record).
        user_id_text = str(record.get('userId') or record.get('id') or '').strip()
        properties[fields['user_id']] = (
            {"rich_text": [{"text": {"content": user_id_text[:2000]}}]}
            if user_id_text
            else {"rich_text": []}
        )
    if fields.get('discord_id'):
        discord_handle = str(record.get('handle') or '').strip()
        properties[fields['discord_id']] = (
            {"rich_text": [{"text": {"content": discord_handle[:2000]}}]}
            if discord_handle
            else {"rich_text": []}
        )
    if fields.get('discord_nickname'):
        discord_nickname = str(record.get('name') or '').strip()
        properties[fields['discord_nickname']] = (
            {"rich_text": [{"text": {"content": discord_nickname[:2000]}}]}
            if discord_nickname
            else {"rich_text": []}
        )
    if fields.get('cohort'):
        cohort_text = str(cohort_label or '').strip() or _normalize_track_application_cohort_value(cohort_label)
        properties[fields['cohort']] = {
            "rich_text": [{"text": {"content": cohort_text[:2000]}}]
        }
    if fields.get('submitted_at'):
        submitted_at = str(record.get('submittedAt') or '').strip()
        properties[fields['submitted_at']] = {
            "date": {"start": submitted_at or _get_kst_now().isoformat()}
        }
    if fields.get('monday_track'):
        properties[fields['monday_track']] = {
            "select": {"name": submission['weekdays']['monday']} if submission['weekdays']['monday'] else None
        }
    if fields.get('tuesday_track'):
        properties[fields['tuesday_track']] = {
            "select": {"name": submission['weekdays']['tuesday']} if submission['weekdays']['tuesday'] else None
        }
    if fields.get('wednesday_track'):
        properties[fields['wednesday_track']] = {
            "select": {"name": submission['weekdays']['wednesday']} if submission['weekdays']['wednesday'] else None
        }
    if fields.get('thursday_track'):
        properties[fields['thursday_track']] = {
            "select": {"name": submission['weekdays']['thursday']} if submission['weekdays']['thursday'] else None
        }
    if fields.get('light_tracks'):
        properties[fields['light_tracks']] = {
            "multi_select": [{"name": label} for label in submission['lightTracks']]
        }
    if fields.get('creator_light_checkbox'):
        properties[fields['creator_light_checkbox']] = {
            "checkbox": any(label.startswith('크리에이터 라이트 트랙') for label in submission['lightTracks'])
        }
    if fields.get('builder_light_checkbox'):
        properties[fields['builder_light_checkbox']] = {
            "checkbox": any(label.startswith('빌더 라이트 트랙') for label in submission['lightTracks'])
        }
    if fields.get('leader_apply'):
        leader_labels = list(submission['leaderLabels'])
        leader_apply_type = fields.get('leader_apply_type') or 'select'
        if leader_apply_type == 'multi_select':
            # 다중 조장 지원: 모든 라벨을 multi_select 옵션으로 한 번에 set
            if leader_labels:
                options = [
                    {"name": f'[{label}] 조장에 지원하겠습니다.'}
                    for label in leader_labels
                ]
            else:
                options = [{"name": '지원에 희망하지 않습니다.'}]
            properties[fields['leader_apply']] = {"multi_select": options}
        else:
            # 단일 select: 기존 동작 유지 (첫 번째 라벨만, 나머지는 notes 백업)
            if leader_labels:
                leader_value = f'[{leader_labels[0]}] 조장에 지원하겠습니다.'
            else:
                leader_value = '지원에 희망하지 않습니다.'
            properties[fields['leader_apply']] = {"select": {"name": leader_value}}
    if fields.get('notes'):
        note_parts = []
        raw_notes = str(record.get('notes') or '').strip()
        if raw_notes:
            note_parts.append(raw_notes)
        if submission.get('creatorSub'):
            note_parts.append(f"크리에이터 세부 선택: {submission['creatorSub']}")
        # multi_select 일 땐 모든 라벨이 select 자체에 들어가니 notes 백업 불필요.
        # select 단일일 때만 다중 조장을 notes 에 백업 (기존 동작).
        if (
            len(submission['leaderLabels']) > 1
            and (fields.get('leader_apply_type') or 'select') == 'select'
        ):
            note_parts.append(f"조장 지원 트랙: {', '.join(submission['leaderLabels'])}")
        if note_parts:
            properties[fields['notes']] = {
                "rich_text": [{"text": {"content": '\n'.join(note_parts)[:2000]}}]
            }
        else:
            properties[fields['notes']] = {"rich_text": []}
    if fields.get('member_relation'):
        properties[fields['member_relation']] = {
            "relation": [{"id": member_page_id}] if member_page_id else []
        }
    if fields.get('cohort_relation'):
        properties[fields['cohort_relation']] = {
            "relation": [{"id": cohort_page_id}] if cohort_page_id else []
        }

    if not existing_page:
        if fields.get('processed'):
            properties[fields['processed']] = {"checkbox": False}
        if fields.get('handled'):
            properties[fields['handled']] = {"checkbox": False}
        if fields.get('group'):
            properties[fields['group']] = {"select": None}

    return properties


def _upsert_track_application_record_to_notion(record, cohort_label):
    notion_client, db_id = _get_track_application_notion_target()
    db_id = str(db_id or '').strip()
    if not db_id:
        raise RuntimeError('TRACK_APPLICATION_DB_ID is not configured.')

    db_obj = notion_client.get_database(db_id)
    if not db_obj:
        raise RuntimeError('Failed to load track application database schema.')

    fields = _resolve_track_application_db_fields(db_obj)
    member_page_id = None
    member_relation_name = fields.get('member_relation')
    if member_relation_name:
        member_relation_db_id = (
            db_obj.get('properties', {})
            .get(member_relation_name, {})
            .get('relation', {})
            .get('database_id')
        )
        if member_relation_db_id:
            member_db = notion_client.get_database(member_relation_db_id)
            member_fields = _resolve_member_db_fields(member_db or {})
            member_page = _find_existing_member_page(
                notion_client,
                member_relation_db_id,
                member_fields,
                record,
            )
            if member_page:
                member_page_id = member_page.get('id')

    existing_page = _find_existing_track_application_page(
        notion_client,
        db_id,
        fields,
        record,
        cohort_label,
        member_page_id=member_page_id,
    )
    properties = _build_track_application_notion_properties(
        notion_client,
        db_id,
        fields,
        record,
        cohort_label,
        existing_page=existing_page,
    )

    if existing_page:
        success = notion_client.update_page_properties(existing_page['id'], properties)
        if not success:
            raise RuntimeError(f"Failed to update existing track application page {existing_page['id']}.")
        return existing_page['id']

    page_id = notion_client.add_row_to_database(db_id, properties)
    if not page_id:
        raise RuntimeError('Failed to create track application page in Notion.')
    return page_id


def _sync_track_application_records_to_notion(records, cohort_label):
    synced = []
    for record in records:
        page_id = _upsert_track_application_record_to_notion(record, cohort_label)
        synced.append({
            "userId": str(record.get('userId') or record.get('id') or '').strip(),
            "pageId": page_id,
        })
    return synced


def _get_member_tracks(member):
    tracks = list(member.get('tracks') or [])
    if not tracks and member.get('track'):
        tracks = [member['track']]
    ordered = []
    for track in tracks:
        if track and track not in ordered:
            ordered.append(track)
    return ordered


def _has_creator_track(member_tracks):
    return any(track in DISCORD_CREATOR_TRACKS for track in member_tracks)


def _find_dashboard_member_by_discord_id(dashboard_data, discord_user_id):
    for member in dashboard_data.get('members', []):
        if str(member.get('discordId', '')).strip() == str(discord_user_id).strip():
            return member
    return None


def _build_authenticated_dashboard_payload(discord_user):
    dashboard_data = _load_dashboard_cache_data() or {"members": [], "submissions": []}
    member = _find_dashboard_member_by_discord_id(dashboard_data, discord_user['id'])

    payload = {
        "authenticated": True,
        "oauthConfigured": _discord_oauth_is_configured(),
        "user": discord_user,
        "member": member,
        "submissions": [],
        "summary": {
            "tracks": [],
            "totalSubmitted": 0,
            "lastSubmittedAt": None,
            "creatorTrackEligible": False,
        }
    }

    if not member:
        return payload

    member_tracks = _get_member_tracks(member)
    member_submissions = [
        submission for submission in dashboard_data.get('submissions', [])
        if submission.get('memberId') == member.get('id')
    ]
    member_submissions.sort(key=lambda item: item.get('date', ''), reverse=True)

    payload["submissions"] = member_submissions
    payload["summary"] = {
        "tracks": member_tracks,
        "totalSubmitted": sum(1 for submission in member_submissions if submission.get('status') == 'submitted'),
        "lastSubmittedAt": member_submissions[0]['date'] if member_submissions else None,
        "creatorTrackEligible": _has_creator_track(member_tracks),
    }
    return payload


def _extract_assignment_title(assignment):
    title_items = assignment.get('properties', {}).get('과제명', {}).get('title', [])
    if not title_items:
        return ''
    return ''.join(item.get('plain_text') or item.get('text', {}).get('content', '') for item in title_items)


def _serialize_assignment(assignment):
    props = assignment.get('properties', {})
    track_names = [item.get('name') for item in props.get('트랙', {}).get('multi_select', []) if item.get('name')]
    due_date = props.get('마감일', {}).get('date', {}) or {}
    cohort_name = (props.get('기수', {}).get('select') or {}).get('name', '')
    return {
        "id": assignment.get('id'),
        "title": _extract_assignment_title(assignment),
        "trackNames": track_names,
        "dueDate": due_date.get('start'),
        "cohort": cohort_name,
        "url": assignment.get('url'),
    }


def _select_relevant_assignment(assignments, today_str):
    if not assignments:
        return None
    upcoming = [assignment for assignment in assignments if assignment.get('dueDate') and assignment['dueDate'] >= today_str]
    if upcoming:
        upcoming.sort(key=lambda item: item['dueDate'])
        return upcoming[0]
    assignments.sort(key=lambda item: item.get('dueDate') or '', reverse=True)
    return assignments[0]


def _build_creator_assignment_payload(discord_user):
    dashboard_payload = _build_authenticated_dashboard_payload(discord_user)
    member = dashboard_payload.get('member')
    if not member:
        return []

    member_tracks = _get_member_tracks(member)
    desired_assignment_tracks = []
    for track in member_tracks:
        desired_assignment_tracks.extend(DISCORD_CREATOR_TRACKS.get(track, []))
    desired_assignment_tracks = sorted(set(desired_assignment_tracks))
    if not desired_assignment_tracks:
        return []

    notion_api = _load_notion_api()
    all_assignments = [_serialize_assignment(assignment) for assignment in notion_api.get_all_assignments()]
    current_cohort = str(os.getenv('CURRENT_COHORT', '')).strip()
    today_str = datetime.now().strftime('%Y-%m-%d')

    results = []
    for dashboard_track, notion_tracks in DISCORD_CREATOR_TRACKS.items():
        if dashboard_track not in member_tracks:
            continue

        candidates = []
        for assignment in all_assignments:
            if current_cohort and assignment.get('cohort') and current_cohort not in str(assignment['cohort']):
                continue
            if any(track_name in notion_tracks for track_name in assignment.get('trackNames', [])):
                candidates.append(dict(assignment))

        chosen = _select_relevant_assignment(candidates, today_str)
        if not chosen:
            continue
        chosen['dashboardTrack'] = dashboard_track
        results.append(chosen)

    return results


@app.route('/api/auth/discord', methods=['GET'])
def start_discord_oauth():
    next_path = _sanitize_relative_path(request.args.get('next') or TRACK_APPLICATION_DEFAULT_PATH)
    if not _is_oauth_enabled_for_path(next_path):
        return redirect(_build_app_redirect_url(next_path, discord_auth='disabled'))
    if not _discord_oauth_is_configured():
        return redirect(_build_app_redirect_url(next_path, discord_auth='not_configured'))
    client_id = _get_discord_client_id()
    if not client_id:
        return redirect(_build_app_redirect_url(next_path, discord_auth='not_configured'))

    state = secrets.token_urlsafe(24)
    session.permanent = True
    session['discord_oauth_state'] = state
    session['discord_oauth_next'] = next_path

    params = {
        'client_id': client_id,
        'redirect_uri': _get_discord_redirect_uri(),
        'response_type': 'code',
        'scope': 'identify',
        'state': state,
    }
    return redirect(f"https://discord.com/oauth2/authorize?{urlencode(params)}")


@app.route('/api/auth/discord/callback', methods=['GET'])
def discord_oauth_callback():
    next_path = _sanitize_relative_path(session.pop('discord_oauth_next', TRACK_APPLICATION_DEFAULT_PATH))
    if not _is_oauth_enabled_for_path(next_path):
        return redirect(_build_app_redirect_url(next_path, discord_auth='disabled'))
    expected_state = session.pop('discord_oauth_state', None)
    client_id = _get_discord_client_id()
    if not client_id or not os.getenv('DISCORD_CLIENT_SECRET'):
        return redirect(_build_app_redirect_url(next_path, discord_auth='not_configured'))

    oauth_error = request.args.get('error')
    if oauth_error:
        return redirect(_build_app_redirect_url(next_path, discord_auth='error'))

    state = request.args.get('state')
    code = request.args.get('code')
    if not expected_state or state != expected_state or not code:
        return redirect(_build_app_redirect_url(next_path, discord_auth='invalid_state'))

    try:
        token_response = requests.post(
            'https://discord.com/api/oauth2/token',
            data={
                'client_id': client_id,
                'client_secret': os.getenv('DISCORD_CLIENT_SECRET'),
                'grant_type': 'authorization_code',
                'code': code,
                'redirect_uri': _get_discord_redirect_uri(),
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=15,
        )
        token_response.raise_for_status()
        access_token = token_response.json().get('access_token')
        if not access_token:
            raise RuntimeError('Missing Discord access token.')

        user_response = requests.get(
            f'{DISCORD_API_BASE}/users/@me',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=15,
        )
        user_response.raise_for_status()
        user_data = user_response.json()

        session.permanent = True
        user_id = str(user_data.get('id', '')).strip()
        # 서버별 닉네임 (guild nickname) 우선 — global "케이" 보다 운영 컨텍스트 풍부.
        # 길드 비멤버거나 닉네임 미설정 시 None → globalName/username 폴백.
        guild_nick = _fetch_guild_nickname(user_id)
        discord_user_info = {
            'id': user_id,
            'username': user_data.get('username', ''),
            'displayName': guild_nick or user_data.get('global_name') or user_data.get('username', ''),
            'globalName': user_data.get('global_name'),
            'guildNickname': guild_nick,
            'avatarUrl': _build_discord_avatar_url(user_data),
        }
        session['discord_user'] = discord_user_info
        # access_token 을 세션에 저장해 _refresh_session_discord_user 가 추후
        # Discord API 로 닉네임 변경을 감지할 수 있게 함 (만료 시 401 → silent skip).
        session['discord_access_token'] = access_token
        import time as _time
        session['discord_user_refreshed_at'] = _time.time()
        # Standalone /apply (Flask 가 직접 서빙) 에서 viewer 정보를 query param 으로 전달.
        # Wrapper(React) 가 사라져 /api/auth/me 호출 단계가 없으므로, callback 시 URL 에 박아 보낸다.
        is_admin = _is_admin_user(discord_user_info['id'])
        return redirect(_build_app_redirect_url(
            next_path,
            discord_auth='success',
            discordUserId=discord_user_info['id'],
            discordDisplayName=discord_user_info['displayName'] or discord_user_info['username'],
            discordHandle=(f"@{discord_user_info['username']}" if discord_user_info['username'] else ''),
            discordAvatarUrl=discord_user_info['avatarUrl'] or '',
            isAdmin='1' if is_admin else '0',
        ))
    except requests.RequestException as e:
        print(f"[ERROR] Discord OAuth exchange failed: {e}")
        return redirect(_build_app_redirect_url(next_path, discord_auth='token_error'))
    except Exception as e:
        print(f"[ERROR] Discord OAuth callback failed: {e}")
        return redirect(_build_app_redirect_url(next_path, discord_auth='callback_error'))


@app.route('/api/auth/me', methods=['GET'])
def get_authenticated_dashboard_data():
    next_path = _sanitize_relative_path(request.args.get('next') or TRACK_APPLICATION_DEFAULT_PATH)
    if not _is_oauth_enabled_for_path(next_path):
        return jsonify(_build_auth_disabled_payload(next_path)), 403
    login_url = _append_query_value(f'{_get_public_api_base_url()}/api/auth/discord', 'next', next_path)
    # 페이지 로드마다 Discord 닉네임/아바타 최신화 (TTL 60s).
    _refresh_session_discord_user()
    discord_user = _get_authenticated_discord_user()

    if not discord_user:
        return jsonify({
            "authenticated": False,
            "oauthConfigured": _discord_oauth_is_configured(),
            "featureEnabled": True,
            "testOnly": _is_test_only_auth_path(next_path),
            "loginUrl": login_url,
        })

    payload = _build_authenticated_dashboard_payload(discord_user)
    payload['loginUrl'] = login_url
    payload['featureEnabled'] = True
    payload['testOnly'] = _is_test_only_auth_path(next_path)
    payload['isAdmin'] = _is_admin_user(discord_user.get('id'))
    return jsonify(payload)


@app.route('/api/track-applications', methods=['GET'])
def get_track_applications():
    cohort_label = _get_current_cohort_label(
        request.args.get('cohortLabel') or request.args.get('cohort')
    )
    members = _build_track_application_member_list(cohort_label)
    discord_user = _get_authenticated_discord_user()
    viewer_member = _find_viewer_track_application_member(cohort_label, discord_user)
    return jsonify({
        "status": "success",
        "cohortLabel": cohort_label,
        "members": members,
        "viewerUserId": str(discord_user.get('id', '')).strip() if discord_user else None,
        "viewerMember": viewer_member,
        "generatedAt": _get_kst_now().isoformat(),
    })


@app.route('/api/mockups/track-applications', methods=['GET'])
def get_track_application_admin_mock_members():
    cohort_label = _get_current_cohort_label(
        request.args.get('cohortLabel') or request.args.get('cohort')
    )
    members = _get_track_application_admin_mock_members_for_cohort(cohort_label)
    return jsonify({
        "status": "success",
        "cohortLabel": cohort_label,
        "members": members,
        "generatedAt": _get_kst_now().isoformat(),
    })


def _check_track_application_window():
    """
    학생 신청 (POST /api/track-applications) 가 신청 윈도우 안에서만 받게 강제.

    이전 footgun:
      클라이언트 view 가드만 있고 서버는 시간 검증 없음 → 학생이 devtools/curl
      등으로 직접 API 호출하면 마감 후/시작 전에도 노션·캐시에 row 들어감.
      운영 정책 위배 + 운영자가 closed/upcoming 화면을 띄워둬도 우회 가능.

    수정:
      - cohort_config 의 applicationStartDate / applicationEndDate / todayOverride
        기준으로 현재 시각(혹은 override)이 윈도우 안인지 검사.
      - 윈도우 밖이면 (None, status, message) 반환해 호출부가 즉시 거부 응답.
      - 운영자(_is_admin_session) 는 우회 — 학생 대신 입력 케이스 보존.
        운영자 편집은 별도 정식 endpoint(/api/track-applications/admin) 사용 권장.
    """
    if _is_admin_session():
        return None, None, None  # 운영자 우회

    cfg = _read_cohort_config() or _default_cohort_config()
    start_iso = str(cfg.get('applicationStartDate') or '').strip()
    end_iso = str(cfg.get('applicationEndDate') or '').strip()
    today_override = str(cfg.get('todayOverride') or '').strip() or None

    # 🔧 today 결정 (2026-05-08 수정):
    #   override 가 있어도 'real time 이 startDate 통과한 경우' 에는 무시 — 즉
    #   admin 의 demo override 가 일반 사용자의 실제 신청을 막지 않게.
    #   (예: override='2026-05-15' / startDate='2026-05-08' / 실제 오늘=2026-05-08
    #   → 종전 override 만 봄 → '시작 전' 으로 차단 → 사용자 신청 못함.)
    real_today_iso = _get_kst_now().strftime('%Y-%m-%d')
    if today_override and _COHORT_CONFIG_DATE_RE.match(today_override):
        # override 가 real today 보다 미래면 그대로 사용 (admin 이 closed 미리보기 등 의도).
        # override 가 real today 보다 과거면 real 로 fallback (real 이 진실).
        today_iso = today_override if today_override >= real_today_iso else real_today_iso
    else:
        today_iso = real_today_iso

    if start_iso and today_iso < start_iso:
        return False, 403, f'아직 신청 시작 전입니다 (오픈: {start_iso}).'
    if end_iso and today_iso > end_iso:
        return False, 403, f'신청이 마감되었습니다 (마감일: {end_iso}).'
    return True, None, None


@app.route('/api/track-applications', methods=['POST'])
def save_track_application():
    # 신청서 저장 직전 무조건 Discord API 재조회 → 닉네임 변경이 즉시 노션에 반영되도록.
    # force=True 라 TTL 무시.
    _refresh_session_discord_user(force=True)
    discord_user = _get_authenticated_discord_user()
    if not discord_user:
        return jsonify({"status": "error", "message": "Discord authentication required."}), 401

    # 🛑 신청 윈도우 가드 — 마감 후/시작 전 직접 호출 차단 (운영자는 우회).
    window_ok, window_status, window_msg = _check_track_application_window()
    if window_ok is False:
        return jsonify({"status": "error", "message": window_msg}), window_status

    payload = request.get_json(silent=True) or {}
    cohort_label = _get_current_cohort_label(payload.get('cohortLabel') or payload.get('cohort'))
    tracks = payload.get('tracks') or []
    if not isinstance(tracks, list) or not tracks:
        return jsonify({"status": "error", "message": "At least one track is required."}), 400

    cache = _read_track_application_cache()
    cohorts = cache.setdefault('cohorts', {})
    cohort_bucket = cohorts.setdefault(cohort_label, {"applications": {}})
    applications = cohort_bucket.setdefault('applications', {})

    user_id = str(discord_user.get('id') or '').strip()
    existing = applications.get(user_id) if isinstance(applications, dict) else None
    record = _build_track_application_record(discord_user, payload, existing=existing)
    try:
        notion_page_id = _upsert_track_application_record_to_notion(record, cohort_label)
    except Exception as e:
        print(f"[ERROR] Failed to sync track application to Notion: {e}")
        return jsonify({
            "status": "error",
            "message": "트랙 신청을 Notion DB에 저장하지 못했습니다.",
        }), 500
    applications[user_id] = record
    _write_track_application_cache(cache)

    return jsonify({
        "status": "success",
        "message": "Track application saved.",
        "member": record,
        "notionPageId": notion_page_id,
    })


@app.route('/api/mockups/track-applications/admin', methods=['PUT'])
def save_admin_track_application_mock_members():
    if not _is_admin_session():
        return jsonify({
            "status": "error",
            "message": "운영진 권한이 필요합니다."
        }), 403

    payload = request.get_json(silent=True) or {}
    cohort_label = _get_current_cohort_label(payload.get('cohortLabel') or payload.get('cohort'))

    raw_members = []
    if isinstance(payload.get('member'), dict):
        raw_members = [payload['member']]
    elif isinstance(payload.get('members'), list):
        raw_members = payload.get('members') or []

    if not raw_members:
        return jsonify({"status": "error", "message": "At least one member payload is required."}), 400

    cache = _read_track_application_admin_mock_cache()
    cohorts = cache.setdefault('cohorts', {})
    cohort_bucket = cohorts.setdefault(cohort_label, {"members": []})
    existing_members = cohort_bucket.get('members', [])
    existing_by_id = {}
    if isinstance(existing_members, list):
        for existing_member in existing_members:
            normalized_existing = _sanitize_track_application_member(existing_member)
            if normalized_existing:
                existing_by_id[normalized_existing['id']] = normalized_existing

    saved_members = []
    for raw_member in raw_members:
        member_id = str(raw_member.get('userId') or raw_member.get('id') or '').strip()
        if not member_id:
            continue
        record = _build_admin_track_application_record(raw_member, existing=existing_by_id.get(member_id))
        if not record:
            continue
        existing_by_id[member_id] = record

    if not existing_by_id:
        return jsonify({"status": "error", "message": "No valid member records were provided."}), 400

    saved_members = list(existing_by_id.values())
    cohort_bucket['members'] = saved_members
    _write_track_application_admin_mock_cache(cache)

    return jsonify({
        "status": "success",
        "message": "Admin mock track applications saved.",
        "count": len(saved_members),
        "members": saved_members,
    })


@app.route('/api/track-applications/admin', methods=['PUT'])
def save_admin_track_applications():
    if not _is_admin_session():
        return jsonify({
            "status": "error",
            "message": "운영진 권한이 필요합니다."
        }), 403

    payload = request.get_json(silent=True) or {}
    cohort_label = _get_current_cohort_label(payload.get('cohortLabel') or payload.get('cohort'))

    raw_members = []
    if isinstance(payload.get('member'), dict):
        raw_members = [payload['member']]
    elif isinstance(payload.get('members'), list):
        raw_members = payload.get('members') or []

    if not raw_members:
        return jsonify({"status": "error", "message": "At least one member payload is required."}), 400

    cache = _read_track_application_cache()
    cohorts = cache.setdefault('cohorts', {})
    cohort_bucket = cohorts.setdefault(cohort_label, {"applications": {}})
    applications = cohort_bucket.setdefault('applications', {})
    if not isinstance(applications, dict):
        applications = {}
        cohort_bucket['applications'] = applications

    saved_members = []
    for raw_member in raw_members:
        member_id = str(raw_member.get('userId') or raw_member.get('id') or '').strip()
        if not member_id:
            continue
        record = _build_admin_track_application_record(raw_member, existing=applications.get(member_id))
        if not record:
            continue
        applications[member_id] = record
        saved_members.append(record)

    if not saved_members:
        return jsonify({"status": "error", "message": "No valid member records were provided."}), 400

    try:
        notion_sync = _sync_track_application_records_to_notion(saved_members, cohort_label)
    except Exception as e:
        print(f"[ERROR] Failed to sync admin track applications to Notion: {e}")
        return jsonify({
            "status": "error",
            "message": "관리자 신청 목록을 Notion DB에 저장하지 못했습니다.",
        }), 500

    _write_track_application_cache(cache)
    return jsonify({
        "status": "success",
        "message": "Admin track applications synced.",
        "count": len(saved_members),
        "members": saved_members,
        "notionSync": notion_sync,
    })


@app.route('/api/auth/logout', methods=['POST'])
def logout_authenticated_dashboard():
    session.pop('discord_user', None)
    session.pop('discord_oauth_state', None)
    session.pop('discord_oauth_next', None)
    return jsonify({"status": "success"})


@app.route('/api/auth/creator-assignments', methods=['GET'])
def get_creator_assignments_for_user():
    if not _is_test_personal_dashboard_enabled():
        return jsonify({"status": "error", "message": "Disabled outside test environment."}), 403
    discord_user = _get_authenticated_discord_user()
    if not discord_user:
        return jsonify({"status": "error", "message": "Authentication required."}), 401

    assignments = _build_creator_assignment_payload(discord_user)
    return jsonify({
        "status": "success",
        "assignments": assignments,
        "generatedAt": datetime.now().isoformat(),
    })


def _pick_db_property(properties, expected_type, preferred_names):
    for name in preferred_names:
        if name in properties and properties[name].get('type') == expected_type:
            return name
    for name, meta in properties.items():
        if meta.get('type') == expected_type:
            return name
    return None


def _resolve_member_db_fields(db_obj):
    properties = db_obj.get('properties', {})
    return {
        'title': _pick_db_property(properties, 'title', ['디스코드 닉네임', '이름', 'Name', 'ID']),
        'user_id': _pick_db_property(properties, 'rich_text', ['사용자 ID', 'User ID']),
        'handle': _pick_db_property(properties, 'rich_text', ['디스코드 ID', 'Discord ID', 'Handle']),
        'track': _pick_db_property(properties, 'multi_select', ['트랙', 'Tracks']),
        'group': _pick_db_property(properties, 'rich_text', ['소속 조', '그룹', 'Group']),
        'notes': _pick_db_property(properties, 'rich_text', ['기타사항', '메모', 'Notes']),
        'avatar': _pick_db_property(properties, 'url', ['프로필 이미지', 'Avatar']),
    }


def _collect_member_group_labels(tracks):
    labels = {}
    for track in tracks:
        track_name = track.get('groupDbName') or track.get('tabLabel') or track.get('tabId') or 'Unknown'
        for group in track.get('groups', []):
            group_name = group.get('name') or '미정'
            label = f'{track_name} / {group_name}'
            for member in group.get('members', []):
                member_id = str(member.get('userId') or member.get('id') or '').strip()
                if not member_id:
                    continue
                labels.setdefault(member_id, [])
                if label not in labels[member_id]:
                    labels[member_id].append(label)
    return labels


def _find_existing_member_page(notion_api, member_db_id, fields, member):
    """
    멤버 마스터 DB 매칭 정책 (2026-05-08 변경):
      - Discord 사용자 ID (snowflake) — 1차 unique 식별자.
      - Discord handle (@닉네임) — 2차 unique 식별자.
      - title (이름) 매칭은 제거 — 동명이인 위험 (사용자 정책).
      - title === member_id (legacy fallback): 마스터 DB title 컬럼에 Discord ID 가
        저장돼있는 옛 데이터 호환용. ID 매칭이라 동명이인 위험 없음 → 유지.
    """
    filters = []
    member_id = str(member.get('userId') or member.get('id') or '').strip()
    handle = str(member.get('handle', '')).strip()

    if member_id and fields.get('user_id'):
        filters.append({"property": fields['user_id'], "rich_text": {"equals": member_id}})
    if handle and fields.get('handle'):
        filters.append({"property": fields['handle'], "rich_text": {"equals": handle}})
    if member_id and fields.get('title'):
        filters.append({"property": fields['title'], "title": {"equals": member_id}})

    if not filters:
        return None

    payload = {
        "page_size": 1,
        "filter": filters[0] if len(filters) == 1 else {"or": filters}
    }
    pages = notion_api.fetch_all_pages(
        f'https://api.notion.com/v1/databases/{member_db_id}/query',
        payload,
    )
    return pages[0] if pages else None


def _build_member_properties(fields, member, group_labels, update_only_tracks_and_group=False):
    """
    멤버 마스터 DB 의 row 에 set 할 properties.

    update_only_tracks_and_group=True (기존 row 매칭 케이스):
      - 사용자 정책 (2026-05-08): '멤버 마스터 DB 는 이미 채워져 있으니 트랙·조만
        교체. 기존 페이지의 정보 (title, user_id, handle, notes 등)는 건드리지 마라.'
      - 신청서의 디스코드 닉네임/사용자 ID 로 멤버 매칭 후, 트랙 multi_select 와 조
        텍스트만 새 기수 데이터로 교체.

    update_only_tracks_and_group=False (신규 row 생성 케이스 — fallback):
      - 매칭 실패해도 어쩔 수 없이 row 만들어야 할 때 (예: legacy 호환). title 등 전체 set.
    """
    properties = {}
    member_id = str(member.get('userId') or member.get('id') or '').strip()
    member_name = str(member.get('name') or member.get('id') or '').strip() or member_id
    handle = str(member.get('handle', '')).strip()
    track_names = [name for name in member.get('trackNames', []) if name]
    group_text = ', '.join(group_labels)

    # 트랙 + 조는 두 케이스 모두 set (새 기수 정보로 교체).
    if fields.get('track'):
        properties[fields['track']] = {
            "multi_select": [{"name": name[:100]} for name in track_names[:100]]
        }
    if fields.get('group'):
        properties[fields['group']] = (
            {"rich_text": [{"text": {"content": group_text[:2000]}}]}
            if group_text
            else {"rich_text": []}
        )

    if update_only_tracks_and_group:
        # 기존 row 보존 모드 — title / user_id / handle / notes 건드리지 않음.
        return properties

    if fields.get('title'):
        properties[fields['title']] = {"title": [{"text": {"content": member_name[:2000] or 'unknown'}}]}
    if fields.get('user_id'):
        properties[fields['user_id']] = {"rich_text": [{"text": {"content": member_id[:2000]}}]}
    if fields.get('handle'):
        properties[fields['handle']] = {
            "rich_text": [{"text": {"content": (handle or member_id)[:2000]}}]
        }
    if fields.get('notes'):
        note_parts = ['source=dashboard-group-preview']
        if handle:
            note_parts.append(f'handle={handle}')
        if member.get('submitted'):
            note_parts.append(f"submitted={member['submitted']}")
        if member.get('edits') is not None:
            note_parts.append(f"edits={member['edits']}")
        properties[fields['notes']] = {
            "rich_text": [{"text": {"content": ' | '.join(note_parts)[:2000]}}]
        }
    if fields.get('avatar') and member.get('avatarUrl'):
        properties[fields['avatar']] = {"url": member['avatarUrl']}

    return properties


def _ensure_member_track_options(notion_api, member_db_id, track_property_name, members):
    if not track_property_name:
        return
    track_names = sorted({
        name
        for member in members
        for name in member.get('trackNames', [])
        if name
    })
    for track_name in track_names:
        notion_api.add_multi_select_option(member_db_id, track_property_name, track_name)


def _clear_non_participant_tracks_and_groups(notion_api, member_db_id, fields, touched_page_ids):
    """
    Option C 구현 (2026-05-08):
      현재 기수 sync 대상 (touched_page_ids) 에 포함 안 된 master DB 멤버의 트랙·조
      컬럼을 비움. 결과: master DB 트랙·조 = '현재 기수 참여자만' 반영.

      이전 기수 history 는 트랙 신청서 DB + (archive 안 된) 옛 inline DB 에 보존됨.

    반환: 비워진 멤버 수 (cleared count).
    """
    track_field = fields.get('track')
    group_field = fields.get('group')
    if not track_field and not group_field:
        return 0

    try:
        all_pages = notion_api.fetch_all_pages(
            f'https://api.notion.com/v1/databases/{member_db_id}/query',
            {"page_size": 100},
        )
    except Exception as e:
        print(f"[clear-non-participants] fetch all pages failed: {e}")
        return 0

    cleared = 0
    for page in all_pages or []:
        if page.get('id') in touched_page_ids:
            continue
        props = page.get('properties', {}) or {}

        clear_props = {}
        if track_field:
            current_tracks = (props.get(track_field, {}) or {}).get('multi_select') or []
            if current_tracks:
                clear_props[track_field] = {"multi_select": []}
        if group_field:
            current_group = (props.get(group_field, {}) or {}).get('rich_text') or []
            # rich_text 가 비어있으면 skip. 'plain_text' 중 하나라도 비어있지 않으면 clear.
            has_text = any(
                (item.get('plain_text') or '').strip() or (item.get('text', {}).get('content') or '').strip()
                for item in current_group
            )
            if has_text:
                clear_props[group_field] = {"rich_text": []}

        if not clear_props:
            continue
        try:
            if notion_api.update_page_properties(page['id'], clear_props):
                cleared += 1
        except Exception as e:
            print(f"[clear-non-participants] update failed for {page.get('id')}: {e}")

    return cleared


def _upsert_group_preview_members(notion_api, member_db_id, members, member_group_labels, auto_create_missing=False):
    """
    멤버 마스터 DB 갱신.

    auto_create_missing=False (기본):
      - 매칭 실패 → SKIP + missing 리스트 누적. (운영자 데이터 보호)
    auto_create_missing=True:
      - 매칭 실패 → 신규 row 생성 (title=name, user_id, handle, track, group 모두 set).
      - 운영자가 success modal 의 '🔧 마스터 DB 자동 추가' 버튼 클릭 시 활성화.

    매칭 성공 케이스는 항상 트랙·조만 patch (다른 필드 보존).

    반환: (member_page_ids dict, summary dict)
      summary['updated']: 매칭 성공 → 트랙·조 patch.
      summary['created']: auto_create_missing=True 일 때 신규 생성된 수.
      summary['missing']: 결국 처리 못 한 케이스 (auto_create 도 실패한 경우 / userId 없는 경우).
    """
    db_obj = notion_api.get_database(member_db_id)
    if not db_obj:
        raise RuntimeError(f'Failed to load member test DB: {member_db_id}')

    fields = _resolve_member_db_fields(db_obj)
    if not fields.get('title'):
        raise RuntimeError('Member test DB is missing a title property.')

    _ensure_member_track_options(notion_api, member_db_id, fields.get('track'), members)

    member_page_ids = {}
    summary = {'created': 0, 'updated': 0, 'missing': [], 'cleared': 0}

    for member in members:
        member_id = str(member.get('userId') or member.get('id') or '').strip()
        if not member_id:
            continue

        existing = _find_existing_member_page(notion_api, member_db_id, fields, member)

        if existing:
            # 트랙·조만 패치 (기존 페이지 다른 정보 보존).
            properties = _build_member_properties(
                fields,
                member,
                member_group_labels.get(member_id, []),
                update_only_tracks_and_group=True,
            )
            if not notion_api.update_page_properties(existing['id'], properties):
                raise RuntimeError(f'Failed to update member row: {member_id}')
            member_page_ids[member_id] = existing['id']
            summary['updated'] += 1
        elif auto_create_missing:
            # 운영자가 명시적으로 자동 생성 요청 → 신규 row 만듦.
            properties = _build_member_properties(
                fields,
                member,
                member_group_labels.get(member_id, []),
                update_only_tracks_and_group=False,
            )
            new_page_id = notion_api.add_row_to_database(member_db_id, properties)
            if not new_page_id:
                # 생성 실패 — missing 으로 fallback.
                summary['missing'].append({
                    'userId': member_id,
                    'name': str(member.get('name') or '').strip(),
                    'handle': str(member.get('handle') or '').strip(),
                    'reason': 'create_failed',
                })
                continue
            member_page_ids[member_id] = new_page_id
            summary['created'] += 1
        else:
            # 매칭 실패 — SKIP, missing 리스트에 누적해서 운영자에게 보고.
            summary['missing'].append({
                'userId': member_id,
                'name': str(member.get('name') or '').strip(),
                'handle': str(member.get('handle') or '').strip(),
            })

    # 🔧 Option C — 미참여자 트랙·조 컬럼 비우기 (2026-05-08).
    #   master DB 의 모든 row 중 이번 sync 에 포함 안 된 멤버 (touched_page_ids 에 없음)
    #   의 트랙 multi_select + 조 rich_text 를 비움. 결과: master DB 가 '현재 기수
    #   참여자만' 반영. 이전 기수 history 는 트랙 신청서 DB 에 보존.
    touched_page_ids = set(member_page_ids.values())
    summary['cleared'] = _clear_non_participant_tracks_and_groups(
        notion_api, member_db_id, fields, touched_page_ids
    )

    return member_page_ids, summary


def _archive_group_preview_inline_dbs(notion_api, track_page_id, cohort_label):
    archived = 0
    cohort_prefixes = {cohort_label.strip()}
    cohort_digits = ''.join(ch for ch in cohort_label if ch.isdigit())
    if cohort_digits:
        cohort_prefixes.add(f'{cohort_digits}기')

    for db in notion_api.get_inline_databases(track_page_id):
        title = str(db.get('title', '')).strip()
        if not title:
            continue
        if any(title.startswith(prefix) for prefix in cohort_prefixes if prefix):
            if notion_api.delete_database(db['id']):
                archived += 1
    return archived


def _archive_notion_block(notion_api, block_id):
    response = notion_api.SESSION.patch(
        f'https://api.notion.com/v1/blocks/{block_id}',
        headers=notion_api.get_headers(),
        json={"archived": True},
        timeout=notion_api.TIMEOUT,
    )
    return response.status_code == 200


def _get_group_preview_root_page(notion_api, notion_id):
    response = notion_api.SESSION.get(
        f'https://api.notion.com/v1/pages/{notion_id}',
        headers=notion_api.get_headers(),
        timeout=notion_api.TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f'Failed to load test group root page: {notion_id}')
    return notion_id


def _archive_group_preview_runtime_children(notion_api, root_page_id):
    archived = 0
    for block in notion_api.fetch_block_children(root_page_id):
        if block.get('type') != 'child_database':
            continue
        title = str(block.get('child_database', {}).get('title') or '').strip()
        if title.endswith('Runtime') and notion_api.delete_database(block['id']):
            archived += 1
    return archived


def _archive_group_preview_page_inline_dbs(notion_api, root_page_id, cohort_label):
    archived = 0
    cohort_prefixes = {cohort_label.strip()}
    cohort_digits = ''.join(ch for ch in cohort_label if ch.isdigit())
    if cohort_digits:
        cohort_prefixes.add(f'{cohort_digits}기')

    for block in notion_api.fetch_block_children(root_page_id):
        if block.get('type') != 'child_database':
            continue
        title = str(block.get('child_database', {}).get('title') or '').strip()
        if title and any(title.startswith(prefix) for prefix in cohort_prefixes if prefix):
            if notion_api.delete_database(block['id']):
                archived += 1
    return archived


def _archive_group_preview_track_headings(notion_api, root_page_id, track_names):
    archived = 0
    normalized_titles = {str(name or '').strip() for name in track_names if str(name or '').strip()}
    if not normalized_titles:
        return archived

    for block in notion_api.fetch_block_children(root_page_id):
        block_type = block.get('type')
        if block_type not in {'heading_1', 'heading_2', 'heading_3'}:
            continue
        text_items = block.get(block_type, {}).get('rich_text', [])
        title = ''.join(item.get('plain_text', '') for item in text_items).strip()
        if title in normalized_titles and _archive_notion_block(notion_api, block['id']):
            archived += 1
    return archived


def _create_group_preview_track_heading(notion_api, root_page_id, track_name):
    payload = {
        "children": [
            {
                "object": "block",
                "type": "heading_1",
                "heading_1": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": track_name[:2000]},
                        }
                    ]
                },
            }
        ]
    }
    response = notion_api.SESSION.patch(
        f'https://api.notion.com/v1/blocks/{root_page_id}/children',
        headers=notion_api.get_headers(),
        json=payload,
        timeout=notion_api.TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f'Failed to create track heading block for {track_name}: {response.text}')
    results = response.json().get('results', [])
    return results[0]['id'] if results else None


def _resolve_database_target_id(notion_api, notion_id):
    if notion_api.get_database(notion_id):
        return notion_id

    response = notion_api.SESSION.get(
        f'https://api.notion.com/v1/pages/{notion_id}',
        headers=notion_api.get_headers(),
        timeout=notion_api.TIMEOUT,
    )
    if response.status_code != 200:
        return notion_id

    child_databases = [
        block
        for block in notion_api.fetch_block_children(notion_id)
        if block.get('type') == 'child_database'
    ]
    if not child_databases:
        return notion_id

    def _created_at(block):
        raw = str(block.get('created_time') or '').strip()
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    def _score(block):
        title = str(block.get('child_database', {}).get('title', '')).strip()
        looks_runtime = title.endswith('Runtime')
        looks_clean = title and '?' not in title and '�' not in title
        return (
            1 if looks_runtime else 0,
            1 if looks_clean else 0,
            _created_at(block),
        )

    best = max(child_databases, key=_score)
    return best['id']


def _ensure_group_preview_group_database(notion_api, notion_id):
    resolved = _resolve_database_target_id(notion_api, notion_id)
    if notion_api.get_database(resolved):
        return resolved

    response = notion_api.SESSION.get(
        f'https://api.notion.com/v1/pages/{notion_id}',
        headers=notion_api.get_headers(),
        timeout=notion_api.TIMEOUT,
    )
    if response.status_code != 200:
        return resolved

    page_title = '트랙/조 DB(TEST) Runtime'
    title_prop = response.json().get('properties', {}).get('title', {}).get('title', [])
    if title_prop:
        raw_page_title = ''.join(chunk.get('plain_text', '') for chunk in title_prop).strip()
        if raw_page_title:
            page_title = f'{raw_page_title} Runtime'

    payload = {
        "parent": {"type": "page_id", "page_id": notion_id},
        "title": [{"type": "text", "text": {"content": page_title}}],
        "properties": {
            "트랙명": {"title": {}}
        }
    }
    create_resp = notion_api.SESSION.post(
        'https://api.notion.com/v1/databases',
        headers=notion_api.get_headers(),
        json=payload,
        timeout=notion_api.TIMEOUT,
    )
    if create_resp.status_code != 200:
        raise RuntimeError(
            f'Failed to create a child database under the test group page: {create_resp.text}'
        )
    return create_resp.json()['id']


def _create_group_preview_inline_db(notion_api, track_page_id, title, member_db_id):
    payload = {
        "parent": {"type": "page_id", "page_id": track_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "is_inline": True,
        "properties": {
            "ID": {"title": {}},
            "디스코드 ID": {"rich_text": {}},
            "트랙": {"select": {}},
            "기수": {"select": {}},
            "직책": {"select": {}},
            "이름": {
                "relation": {
                    "database_id": member_db_id,
                    "single_property": {}
                }
            }
        }
    }
    response = notion_api.SESSION.post(
        'https://api.notion.com/v1/databases',
        headers=notion_api.get_headers(),
        json=payload,
        timeout=notion_api.TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f'Failed to create inline group DB {title}: {response.text}')
    return response.json()['id']


def _resolve_master_db_schema(notion_api, master_db_id):
    """
    마스터 DB 의 properties 스키마를 읽어 lookup 에 필요한 키 추출.
    한 번 읽으면 commit 사이클 동안 reuse — 트랙별로 매번 호출하지 않음.

    Returns:
      {
        'title_prop_name': str | None,         # 트랙 이름이 들어있는 title column
        'has_track_multi_select': bool,        # '트랙명' multi_select 존재 여부
        'properties': dict (raw schema dict),  # 디버깅용
      }
    """
    db_obj = notion_api.get_database(master_db_id)
    if not db_obj:
        raise RuntimeError(f"Cannot read master DB schema: {master_db_id}")

    properties = db_obj.get('properties') or {}
    title_prop_name = None
    has_track_multi_select = False
    for name, info in properties.items():
        if info.get('type') == 'title' and not title_prop_name:
            title_prop_name = name
        if name == '트랙명' and info.get('type') == 'multi_select':
            has_track_multi_select = True

    return {
        'title_prop_name': title_prop_name,
        'has_track_multi_select': has_track_multi_select,
        'properties': properties,
    }


def _find_track_page_in_master_db(notion_api, master_db_id, track_name, schema=None):
    """
    master DB 안에서 트랙 페이지를 찾는다. schema-aware:
      - schema 의 '트랙명' multi_select 가 있을 때만 1차 쿼리 (없으면 skip — 400 noise 방지)
      - 항상 title column contains 로 폴백

    schema 가 None 이면 즉석 조회 (legacy 호환).
    """
    if schema is None:
        try:
            schema = _resolve_master_db_schema(notion_api, master_db_id)
        except Exception:
            schema = {'title_prop_name': '이름', 'has_track_multi_select': False}

    if schema.get('has_track_multi_select'):
        payload = {"filter": {"property": "트랙명", "multi_select": {"contains": track_name}}}
        pages = notion_api.fetch_all_pages(
            f'https://api.notion.com/v1/databases/{master_db_id}/query', payload
        )
        if pages:
            return pages[0]['id']

    title_prop = schema.get('title_prop_name') or '이름'
    payload = {"filter": {"property": title_prop, "title": {"contains": track_name}}}
    pages = notion_api.fetch_all_pages(
        f'https://api.notion.com/v1/databases/{master_db_id}/query', payload
    )
    if pages:
        return pages[0]['id']
    return None


def _get_or_create_track_page_in_master_db(notion_api, master_db_id, track_name, schema=None):
    """
    test 워크스페이스 전용 라우트에서, master DB 안에 트랙 페이지가 없으면 자동 생성.
    schema 가 주어지면 재조회 안 함 (트랙 N개 처리 시 N번 fetch 방지).
    prod 영향 없음 — 이 함수는 _commit_group_preview_to_notion (test-only) 에서만 호출됨.
    """
    if schema is None:
        schema = _resolve_master_db_schema(notion_api, master_db_id)

    page_id = _find_track_page_in_master_db(notion_api, master_db_id, track_name, schema=schema)
    if page_id:
        return page_id, False

    title_prop_name = schema.get('title_prop_name')
    if not title_prop_name:
        raise RuntimeError(f"Master DB({master_db_id}) 에 title 속성이 없습니다.")

    properties = {
        title_prop_name: {"title": [{"text": {"content": track_name[:2000]}}]}
    }
    if schema.get('has_track_multi_select'):
        properties['트랙명'] = {"multi_select": [{"name": track_name[:100]}]}

    new_page_id = notion_api.add_row_to_database(master_db_id, properties)
    if not new_page_id:
        raise RuntimeError(f"트랙 페이지 자동 생성 실패: {track_name}")
    return new_page_id, True


def _archive_track_page_inline_dbs(notion_api, track_page_id):
    """
    Legacy `_reset_cohort_groups` 와 동일: 트랙 페이지 안의 모든 inline DB 를 archive.
    재배정 시마다 깨끗한 상태에서 새 그룹 DB 를 만들기 위함.
    DEPRECATED (2026-05-08): incremental sync 로 전환되면서 호출 안 함.
    """
    existing_dbs = notion_api.get_inline_databases(track_page_id)
    deleted_count = 0
    for db in existing_dbs:
        if notion_api.delete_database(db['id']):
            deleted_count += 1
    return deleted_count


def _find_inline_db_by_title(notion_api, parent_page_id, title):
    """트랙 페이지 안에서 정확히 같은 title 의 inline DB 찾기. 없으면 None."""
    if not parent_page_id or not title:
        return None
    target = title.strip()
    for db in notion_api.get_inline_databases(parent_page_id):
        existing_title = str(db.get('title', '')).strip()
        if existing_title == target:
            return db
    return None


def _get_existing_member_relations_in_inline_db(notion_api, inline_db_id, relation_prop_name='이름'):
    """
    inline DB 의 모든 row 를 조회 → '이름' relation 의 member_page_id → row info dict.
    반환: { member_page_id: { 'row_id': ..., 'leader': bool } }.
    """
    result = {}
    if not inline_db_id:
        return result
    try:
        pages = notion_api.fetch_all_pages(
            f'https://api.notion.com/v1/databases/{inline_db_id}/query',
            {"page_size": 100},
        )
    except Exception as e:
        print(f"[group-assign] fetch inline DB rows failed: {e}")
        return result
    for page in pages or []:
        props = page.get('properties', {}) or {}
        relation = props.get(relation_prop_name, {}).get('relation', []) or []
        if not relation:
            continue
        for ref in relation:
            mid = ref.get('id')
            if not mid:
                continue
            role_select = props.get('직책', {}).get('select') or {}
            result[mid] = {
                'row_id': page.get('id'),
                'leader': str(role_select.get('name') or '').strip() == '조장',
            }
            break  # 첫 relation 만 (멤버 1명 = row 1개 정책)
    return result


def _ensure_group_inline_db_schema(notion_api, target_db_id, member_db_id):
    """
    Legacy `_execute_group_assignment` 의 schema migration 블록과 동일.
    1) '이름' 이 title 이면 'ID' 로 rename (이름 컬럼은 relation 으로 쓰기 위해)
    2) '이름' relation, '트랙'/'기수'/'직책' select, '디스코드 ID' rich_text 보장
    """
    db_obj = notion_api.get_database(target_db_id)
    if not db_obj:
        return

    current_props = db_obj.get('properties', {})

    # 1. '이름' title → 'ID' rename
    migration_payload = {}
    if '이름' in current_props and current_props['이름'].get('type') == 'title':
        migration_payload['이름'] = {'name': 'ID'}
    if migration_payload:
        notion_api.update_database_schema(target_db_id, migration_payload)
        db_obj = notion_api.get_database(target_db_id) or {}
        current_props = db_obj.get('properties', {})

    # 2. 필수 속성 보장
    update_payload = {}
    if '이름' not in current_props or current_props['이름'].get('type') != 'relation':
        update_payload['이름'] = {
            'relation': {
                'database_id': member_db_id,
                'single_property': {},
            }
        }
    for p_name in ('트랙', '기수', '직책'):
        if p_name not in current_props:
            update_payload[p_name] = {'select': {}}
    if '디스코드 ID' not in current_props:
        update_payload['디스코드 ID'] = {'rich_text': {}}

    if update_payload:
        notion_api.update_database_schema(target_db_id, update_payload)


def _seed_group_preview_inline_db_options(notion_api, inline_db_id, cohort_label, row_track_names):
    properties = {
        "직책": {"select": {"options": [{"name": "조장"}, {"name": "조원"}]}},
        "기수": {"select": {"options": [{"name": cohort_label}]}}
    }
    if row_track_names:
        properties["트랙"] = {
            "select": {
                "options": [{"name": track_name} for track_name in sorted(set(row_track_names))]
            }
        }
    notion_api.update_database_schema(inline_db_id, properties)


def _commit_group_preview_to_notion(payload, progress_callback=None):
    """
    Notion + Discord 동기화. 비동기 job 으로 실행 시 progress_callback 으로
    각 단계 진행상황을 전달.

    progress_callback(phase: str, detail: str = '', extra: dict = None)
      phase: 'notion_members' | 'notion_groups' | 'discord_sync' | 'done'
    """
    def _progress(phase, detail='', extra=None):
        if not progress_callback:
            return
        try:
            progress_callback(phase, detail, extra or {})
        except Exception as e:
            print(f"[WARN] progress_callback raised: {e}")

    if payload.get('target') != 'notion-test':
        raise ValueError('Only the test Notion target is allowed for this route.')

    member_db_id = _normalize_notion_id(payload.get('memberDbId')) or _normalize_notion_id(
        GROUP_PREVIEW_DEFAULT_MEMBER_TEST_DB_ID
    )
    group_db_id = _normalize_notion_id(payload.get('groupDbId')) or _normalize_notion_id(
        GROUP_PREVIEW_DEFAULT_GROUP_TEST_DB_ID
    )

    allowed_member_db_id = _normalize_notion_id(GROUP_PREVIEW_DEFAULT_MEMBER_TEST_DB_ID)
    allowed_group_db_id = _normalize_notion_id(GROUP_PREVIEW_DEFAULT_GROUP_TEST_DB_ID)
    if member_db_id != allowed_member_db_id or group_db_id != allowed_group_db_id:
        raise ValueError('This route is locked to the configured test Notion DB IDs only.')

    cohort_label = str(payload.get('cohortLabel') or '9기').strip()
    members = payload.get('members') or []
    tracks = payload.get('tracks') or []
    # 🔧 운영자가 success modal 의 '🔧 마스터 DB 자동 추가' 버튼 클릭 시 활성화.
    #   매칭 실패 (master DB 에 row 없음) 멤버를 자동으로 신규 생성. 기본은 false (skip).
    auto_create_missing = bool(payload.get('autoCreateMissing'))

    if not members:
        raise ValueError('No mock members were provided.')
    if not tracks:
        raise ValueError('No group assignment payload was provided.')

    test_queue_file = get_bot_command_queue_file(BASE_DIR, explicit='test')
    _assert_bot_command_queue_idle(test_queue_file)

    # 안전장치: test 전용 Notion 토큰 결정.
    # 우선순위:
    #   1) GROUP_PREVIEW_TEST_NOTION_TOKEN (명시 override)
    #   2) ASC_ENV=test 일 때 NOTION_TOKEN (.env.test 가 이미 test 워크스페이스 토큰을 로드)
    # 둘 다 없으면 prod 토큰으로 폴백되지 않도록 명시적으로 거부.
    _env_name = (os.getenv('ASC_ENV') or os.getenv('RUN_MODE') or '').strip().lower()
    notion_token_to_use = GROUP_PREVIEW_TEST_NOTION_TOKEN
    if not notion_token_to_use and _env_name == 'test':
        notion_token_to_use = (os.getenv('NOTION_TOKEN') or '').strip() or None

    if not notion_token_to_use:
        raise ValueError(
            'test Notion 토큰을 결정할 수 없습니다. '
            'GROUP_PREVIEW_TEST_NOTION_TOKEN 을 설정하거나 ASC_ENV=test 모드로 실행해 .env.test 의 NOTION_TOKEN 을 사용하세요. '
            '이 라우트는 테스트 Notion 워크스페이스 전용이며 prod 토큰으로 폴백할 수 없습니다.'
        )
    notion_api = _load_notion_api(notion_token_override=notion_token_to_use)
    member_db_id = _resolve_database_target_id(notion_api, member_db_id)
    member_group_labels = _collect_member_group_labels(tracks)

    _progress('notion_members', f'멤버 마스터 DB 갱신 중 ({len(members)}명)')
    member_page_ids, member_summary = _upsert_group_preview_members(
        notion_api,
        member_db_id,
        members,
        member_group_labels,
        auto_create_missing=auto_create_missing,
    )

    # ─────────────────────────────────────────────────────────────────────
    # Legacy 호환 트리 구조: master DB → 트랙 페이지 → inline group DB
    # group_db_id 는 master DB ID. legacy `_execute_group_assignment` 와 동일하게
    # 트랙명 multi_select / 이름 title 로 트랙 페이지를 찾고, 그 페이지 안에
    # inline DB 를 만든다. 이렇게 해야 운영 워크스페이스와 머지 가능.
    # ─────────────────────────────────────────────────────────────────────
    archived_inline_dbs = 0
    created_group_dbs = 0
    created_group_rows = 0
    touched_tracks = []
    track_name_to_page_id = {}

    # 마스터 DB 스키마 한 번만 조회 — 모든 트랙 lookup 에서 재사용.
    # (이전엔 트랙별로 '트랙명' 쿼리 → 400 노이즈 + 트랙당 1회 fetch 낭비)
    try:
        master_db_schema = _resolve_master_db_schema(notion_api, group_db_id)
    except Exception as e:
        raise RuntimeError(f"마스터 DB 스키마 조회 실패: {group_db_id}: {e}")

    # 라이트 트랙은 조 배정 없이 부모 트랙의 공지 / 과제-인증 채널만 접근 → Notion 조 inline DB 생성 X.
    LIGHT_TRACK_NAME_KEYWORDS = ('라이트 트랙', '라이트트랙')

    eligible_tracks = []
    for track in tracks:
        groups = [group for group in track.get('groups', []) if group.get('members')]
        if not groups:
            continue
        track_name = (track.get('groupDbName') or track.get('tabLabel') or track.get('tabId') or '').strip()
        if not track_name:
            continue
        if any(kw in track_name for kw in LIGHT_TRACK_NAME_KEYWORDS):
            continue
        eligible_tracks.append((track, groups, track_name))

    total_eligible = len(eligible_tracks)
    for idx, (track, groups, track_name) in enumerate(eligible_tracks, start=1):
        _progress(
            'notion_groups',
            f'{track_name} ({idx}/{total_eligible})',
            {'tracksProcessed': idx - 1, 'tracksTotal': total_eligible},
        )

        touched_tracks.append(track_name)

        # 1. master DB 에서 트랙 페이지 찾기 — test 워크스페이스에서는 없으면 자동 생성
        track_page_id, _created = _get_or_create_track_page_in_master_db(
            notion_api, group_db_id, track_name, schema=master_db_schema
        )
        track_name_to_page_id[track_name] = track_page_id

        # 🔧 incremental sync (2026-05-08):
        #   직전: 트랙 페이지의 기존 inline DB 전부 archive 후 재생성 → URL 바뀌고
        #         이미 등록된 멤버가 매번 건드려짐.
        #   변경: archive 안 함. 그룹별로 같은 title 의 inline DB 찾아서 reuse.
        #         row 도 기존 멤버 page_id 매칭으로 중복 안 만들고, 조장/조원 변동만 patch,
        #         그룹에서 빠진 멤버는 row archive.
        #   사용자 의도: '매칭이 안된 사람이 새로 들어오면 그 사람만 추가, 나머지는 건드리지 마'.

        # 3. 그룹별 inline DB find-or-create + 스키마 보장 + row incremental sync
        for group in groups:
            group_name = str(group.get('name') or '').strip()
            if not group_name:
                continue

            existing_db = _find_inline_db_by_title(notion_api, track_page_id, group_name)
            if existing_db:
                inline_db_id = existing_db['id']
            else:
                inline_db_id = _create_group_preview_inline_db(
                    notion_api,
                    track_page_id,
                    group_name,
                    member_db_id,
                )
                created_group_dbs += 1
            _ensure_group_inline_db_schema(notion_api, inline_db_id, member_db_id)
            group_url = f"https://www.notion.so/{inline_db_id.replace('-', '')}"

            existing_relations = _get_existing_member_relations_in_inline_db(
                notion_api, inline_db_id
            )

            desired_member_page_ids = set()
            for member in group.get('members', []):
                member_id = str(member.get('userId') or member.get('id') or '').strip()
                member_page_id = member_page_ids.get(member_id)
                if not member_page_id:
                    # 멤버 마스터 DB 에 매칭 안 된 멤버 — _upsert_group_preview_members 에서
                    # 이미 missing 리스트에 누적됨. 조 inline DB 에도 row 안 만들고 skip
                    # (조 페이지 자체는 생성됨, 매칭된 멤버들만 들어감).
                    print(f"[group-assign] skip group row — member not in master DB: {member_id}")
                    continue
                desired_member_page_ids.add(member_page_id)

                row_track_name = (member.get('rowTrackName') or track_name).strip()
                row_title = str(member.get('name') or member.get('id') or member_id).strip() or member_id
                row_handle = str(member.get('handle') or member.get('userId') or member_id).strip()
                desired_leader = bool(member.get('leader'))
                row_props = {
                    "ID": {"title": [{"text": {"content": row_title[:2000] or 'unknown'}}]},
                    "디스코드 ID": {
                        "rich_text": [{"text": {"content": row_handle[:2000]}}]
                    },
                    "트랙": {"select": {"name": row_track_name[:100]}},
                    "기수": {"select": {"name": cohort_label[:100]}},
                    "직책": {"select": {"name": "조장" if desired_leader else "조원"}},
                    "이름": {"relation": [{"id": member_page_id}]}
                }

                existing_row = existing_relations.get(member_page_id)
                if existing_row:
                    # 이미 있는 멤버 — 조장 토글이 바뀐 경우만 patch.
                    if existing_row.get('leader') != desired_leader:
                        notion_api.update_page_properties(
                            existing_row['row_id'],
                            {"직책": {"select": {"name": "조장" if desired_leader else "조원"}}},
                        )
                    # 그 외는 변경 없음 — 기존 row 그대로 유지 (URL/relation 보존).
                    continue

                row_id = notion_api.add_row_to_database(inline_db_id, row_props)
                if not row_id:
                    raise RuntimeError(f'Failed to create group row for {member_id} in {group_name}')
                created_group_rows += 1

            # 그룹에서 빠진 멤버 (existing 에 있지만 desired 에 없음) → row archive.
            #   admin 이 이 그룹에서 다른 그룹으로 옮겼거나 제거한 경우. stale row 정리.
            for stale_member_page_id, info in existing_relations.items():
                if stale_member_page_id in desired_member_page_ids:
                    continue
                # notion_api 모듈의 archive_page (PATCH /v1/pages/{id} archived=true)
                try:
                    from notion_api import archive_page as _archive_page
                    _archive_page(info['row_id'])
                except Exception as _e:
                    print(f"[group-assign] archive stale row failed: {_e}")

                # legacy `assign_member_to_group` — 멤버 페이지의 '소속 조' 에 링크 포함 텍스트 저장
                try:
                    notion_api.assign_member_to_group(member_page_id, group_name, group_url)
                except Exception as e:
                    # 멤버 페이지에 '소속 조' 속성이 없을 수도 있어 실패는 경고 처리
                    print(f"[WARN] assign_member_to_group failed for {member_id}: {e}")

    _progress('discord_sync', '봇이 채널·역할을 만드는 중')
    try:
        discord_summary = _run_bot_command_and_wait(
            'group_preview_sync_discord',
            {
                "cohortLabel": cohort_label,
                "tracks": tracks,
            },
            queue_file=test_queue_file,
        )
    except Exception as e:
        raise RuntimeError(f'Notion commit completed, but Discord sync failed: {e}') from e

    _progress('done', '완료')
    return {
        "status": "success",
        "message": "Mock group preview was committed to the test Notion databases and synced to Discord.",
        "summary": {
            "member_db_id": member_db_id,
            "group_db_id": group_db_id,
            "track_pages": track_name_to_page_id,
            "members_created": member_summary['created'],
            "members_updated": member_summary['updated'],
            # 미참여자 (master DB 에 있지만 이번 기수 sync 대상 아님) 트랙·조 컬럼이
            # 비워진 수 (Option C).
            "members_cleared": member_summary.get('cleared', 0),
            # 멤버 마스터 DB 에 매칭 안 돼서 skip 된 멤버 (Discord ID/handle 둘 다 안 맞음).
            # 운영자 UI 가 alert 으로 표시.
            "members_missing": member_summary.get('missing', []),
            "tracks_touched": touched_tracks,
            "archived_inline_dbs": archived_inline_dbs,
            "group_databases_created": created_group_dbs,
            "group_rows_created": created_group_rows,
            "discord": discord_summary,
        }
    }

@app.route('/api/settings', methods=['GET'])
def get_settings():
    # 🛡 F-1: admin 가드 — env/config 가 노출되지 않도록 운영진만 조회 허용.
    if not _is_admin_session():
        return jsonify({"status": "error", "message": "운영진 권한이 필요합니다."}), 403

    # 1. Load from .env
    env = load_env_file()
    
    # 2. Load from bot_config.json
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
        
    track_settings = config.get("track_settings", {})
    holiday_settings = config.get("holiday_settings", {})
    
    # Safe loading of UI reminders
    ui_reminders = config.get("ui_reminders", {})
    
    # Fallback to track_settings if UI reminders not set yet
    def_sf1 = ui_reminders.get("sfTime1", "12:00")
    def_sf2 = ui_reminders.get("sfTime2", track_settings.get("creator_short", {}).get("time", "18:00"))
    def_wk1 = ui_reminders.get("weeklyTime1", "10:00")
    def_wk2 = ui_reminders.get("weeklyTime2", track_settings.get("creator_long", {}).get("time", "18:00"))

    data = {
        "cohortName": env.get("CURRENT_COHORT", "6"),
        "startDate": env.get("COHORT_START_DATE", "2026-02-09"),
        "endDate": env.get("COHORT_END_DATE", "2026-03-09"),
        "holidayStart": holiday_settings.get("start", ""),
        "holidayEnd": holiday_settings.get("end", ""),
        "notificationsEnabled": config.get("notifications_enabled", True),
        "testMode": config.get("test_mode", False),
        "sfTime1": def_sf1,
        "sfTime2": def_sf2,
        "weeklyTime1": def_wk1,
        "weeklyTime2": def_wk2,
        "trackConfig": config.get("trackConfig", []),
        "discordChannels": config.get("discord_channels", {}),
        "discordRuntimeResources": config.get("discord_runtime_resources", {})
    }
    return jsonify(data)

@app.route('/api/notifications/preview', methods=['GET'])
def get_notification_preview():
    """Get upcoming scheduled notifications for dashboard preview"""
    try:
        from notification_preview import get_next_notifications
        notifications = get_next_notifications()
        
        return jsonify({
            "success": True,
            "notifications": notifications,
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
        })
    except Exception as e:
        print(f"[ERROR] Failed to get notification preview: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e),
            "notifications": []
        }), 500

@app.route('/api/settings', methods=['POST'])
def update_settings():
    # 🛡 F-1: admin 가드 — .env / bot_config 변경 + deploy.py 트리거를 운영진으로 제한.
    if not _is_admin_session():
        return jsonify({"status": "error", "message": "운영진 권한이 필요합니다."}), 403

    import copy
    data = request.json
    print(f"[INFO] Received Settings Update: {data}")
    
    # [NEW] Load Old State for Comparison BEFORE updates
    try:
        with open(CONFIG_FILE, 'r') as f:
            old_config = json.load(f)
    except:
        old_config = {}
        
    old_env = load_env_file() # Snapshot .env
    
    # 1. Update .env
    env_updates = {}
    if 'cohortName' in data:
        env_updates['CURRENT_COHORT'] = data['cohortName']
    if 'startDate' in data:
        env_updates['COHORT_START_DATE'] = data['startDate']
    if 'endDate' in data:
        env_updates['COHORT_END_DATE'] = data['endDate']
        
    if env_updates:
        save_env_file(env_updates)
        print("[SUCCESS] .env Updated")
        
    # 2. Update config object
    config = copy.deepcopy(old_config)
        
    if 'holidayStart' in data and 'holidayEnd' in data:
        config['holiday_settings'] = {
            "start": data['holidayStart'],
            "end": data['holidayEnd']
        }

    if 'notificationsEnabled' in data:
        config['notifications_enabled'] = data['notificationsEnabled']
    
    if 'testMode' in data:
        config['test_mode'] = data['testMode']
        
    if 'ui_reminders' not in config:
        config['ui_reminders'] = {}
        
    if 'sfTime1' in data: config['ui_reminders']['sfTime1'] = data['sfTime1']
    if 'sfTime2' in data: config['ui_reminders']['sfTime2'] = data['sfTime2']
    if 'weeklyTime1' in data: config['ui_reminders']['weeklyTime1'] = data['weeklyTime1']
    if 'weeklyTime2' in data: config['ui_reminders']['weeklyTime2'] = data['weeklyTime2']
    
    if 'track_settings' not in config: config['track_settings'] = {}
    
    if 'sfTime2' in data and 'creator_short' in config['track_settings']:
        if isinstance(config['track_settings']['creator_short'], dict):
            config['track_settings']['creator_short']['time'] = data['sfTime2']

    if 'weeklyTime2' in data and 'creator_long' in config['track_settings']:
        if isinstance(config['track_settings']['creator_long'], dict):
            config['track_settings']['creator_long']['time'] = data['weeklyTime2']

    if 'discordChannels' in data:
        config['discord_channels'] = data['discordChannels']

    if 'trackConfig' in data:
        config['trackConfig'] = data['trackConfig']

        # 노션 동기화: 새 트랙을 멤버 마스터 DB + 트랙/조 DB에 반영
        def sync_tracks_to_notion(track_configs):
            try:
                import notion_api
                import importlib
                importlib.reload(notion_api)

                member_db_id = os.getenv('TRACK_JO_DB_ID')
                group_db_id = os.getenv('GROUP_DB_ID')

                for tc in track_configs:
                    notion_name = tc.get('notionName', '')
                    group_name = tc.get('groupDbName', notion_name)
                    if not notion_name:
                        continue

                    # 1. 멤버 마스터 DB에 multi_select 옵션 추가
                    if member_db_id:
                        notion_api.add_multi_select_option(member_db_id, '트랙', notion_name)

                    # 2. 트랙/조 DB에 트랙 페이지 생성
                    if group_db_id and group_name:
                        notion_api.create_track_page_in_group_db(group_db_id, group_name)

                # 3. 조 관리 캐시 갱신
                try:
                    import notion_group_api
                    importlib.reload(notion_group_api)
                    group_result = notion_group_api.get_all_group_tracks()
                    groups_file = os.path.join(BASE_DIR, 'groups_cache.json')
                    with open(groups_file, 'w', encoding='utf-8') as f:
                        json.dump(group_result, f, ensure_ascii=False)
                    print("[SUCCESS] Groups cache refreshed after track sync")
                except Exception as e2:
                    print(f"[WARN] Failed to refresh groups cache: {e2}")

                print("[SUCCESS] Track config synced to Notion")
            except Exception as e:
                print(f"[WARN] Failed to sync tracks to Notion: {e}")

        threading.Thread(target=sync_tracks_to_notion, args=(data['trackConfig'],), daemon=True).start()

    print(f"[DEBUG] Writing to {CONFIG_FILE}. Data keys: {list(data.keys())}")

    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    print("[SUCCESS] bot_config.json Updated")

    # [NEW] Queue Admin Notification with Strict Comparison
    try:
        changes = []
        
        # 1. Cohort / Period (.env diff)
        def _get_env_val(k):
            return str(old_env.get(k, '')).strip()
        def _get_data_val(k):
            return str(data.get(k, '')).strip()

        if 'cohortName' in data and _get_data_val('cohortName') != _get_env_val('CURRENT_COHORT'):
            changes.append(f"- 기수 명칭: {_get_env_val('CURRENT_COHORT')} -> {_get_data_val('cohortName')}")
        if 'startDate' in data and _get_data_val('startDate') != _get_env_val('COHORT_START_DATE'):
            changes.append(f"- 기수 시작일: {_get_env_val('COHORT_START_DATE')} -> {_get_data_val('startDate')}")
        if 'endDate' in data and _get_data_val('endDate') != _get_env_val('COHORT_END_DATE'):
            changes.append(f"- 기수 마감일(숏폼): {_get_env_val('COHORT_END_DATE') or 'N/A'} -> {_get_data_val('endDate')}")
        
        # 2. Holiday
        if 'holidayStart' in data:
            old_h_settings = old_config.get('holiday_settings', {})
            o_s = str(old_h_settings.get('start', '')).strip()
            o_e = str(old_h_settings.get('end', '')).strip()
            n_s = _get_data_val('holidayStart')
            n_e = _get_data_val('holidayEnd')
            if n_s != o_s or n_e != o_e:
                changes.append(f"- 휴무 기간: {n_s} ~ {n_e}")

        # 3. Notifications Enabled
        if 'notificationsEnabled' in data:
            old_notif = old_config.get('notifications_enabled', True)
            if data['notificationsEnabled'] != old_notif:
                status = "ON" if data['notificationsEnabled'] else "OFF"
                changes.append(f"- 전체 알림 설정: {status}")
        
        # 4. Test Mode
        if 'testMode' in data:
            old_test = old_config.get('test_mode', False)
            if data['testMode'] != old_test:
                status = "ON" if data['testMode'] else "OFF"
                changes.append(f"- 테스트 모드: {status}")
 
        if changes:
            summary_msg = "**[관리자 설정 변경 알림]**\n\n" + "\n".join(changes)
            
            cmd = {
                "id": str(time.time()),
                "type": "admin_notification",
                "payload": {
                    "message": summary_msg
                },
                "status": "pending",
                "created_at": time.time()
            }
            with open(COMMAND_QUEUE_FILE, 'w') as f:
                json.dump(cmd, f)
            print(f"[INFO] Admin notification queued. Changes: {len(changes)}")
    except Exception as e:
        print(f"[WARN] Failed to queue admin notification: {e}")
    
    # 3. Trigger Deploy (Async)
    def run_deploy_and_setup():
        # 1. Create Notion Assignments FIRST (before deploy restarts the server)
        if 'cohortName' in data and 'startDate' in data:
            try:
                cohort = data['cohortName']
                start_date = data['startDate']
                short_due = data.get('endDate')

                print(f"[INFO] Creating/Updating Notion Assignments for {cohort}...")
                import notion_api
                from utils.helpers import (
                    calculate_week_sunday,
                    calculate_app_dev_due_date,
                    calculate_self_inquiry_due_date,
                )

                # (1) Short Form Assignment
                notion_api.create_short_form_assignment(cohort, due_date_str=short_due)

                # (2) Week 1 통합 과제 (일요일 마감, 나 탐구 제외)
                week1_due = calculate_week_sunday(start_date, 1)
                if week1_due:
                    title = "1주차 통합 과제"
                    tracks = ['크리에이터 롱폼 트랙', '세일즈 실전 트랙', '빌더 기초 트랙', '빌더 심화 트랙', 'AI 에이전트 트랙']
                    notion_api.create_assignment(title, tracks, "과제", week1_due, cohort=cohort)

                    # 앱 개발 트랙: 기수 시작 주 다음 주 수요일 마감
                    wed_due = calculate_app_dev_due_date(start_date, 1)
                    app_title = "1주차 통합 과제 (앱 개발)"
                    notion_api.create_assignment(app_title, ['앱 개발 트랙'], "과제", wed_due, cohort=cohort, update_if_exists=True)

                    # 나 탐구 트랙: 매 주차 토요일 마감
                    sat_due = calculate_self_inquiry_due_date(start_date, 1)
                    self_title = "1주차 통합 과제 (나 탐구)"
                    notion_api.create_assignment(self_title, ['나 탐구 트랙'], "과제", sat_due, cohort=cohort, update_if_exists=True)

            except Exception as e:
                print(f"[WARN] Failed to create assignments: {e}")

        # 2. Deploy (may restart server, so this must be last)
        print("[INFO] Triggering Deploy...")
        try:
            subprocess.run([sys.executable, "deploy.py"], check=True)
            print("[SUCCESS] Deployment Triggered Successfully")
        except Exception as e:
            print(f"[ERROR] Deployment Failed: {e}")

    threading.Thread(target=run_deploy_and_setup).start()
    
    return jsonify({"status": "success", "message": "Settings saved and processed!"})

@app.route('/api/data', methods=['GET'])
def get_cached_data():
    try:
        data = _load_dashboard_cache_data()
        if data:
            return jsonify({"status": "success", "data": data})
        return jsonify({"status": "error", "message": "Data file not found. Please sync first."}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

_sync_status = {"running": False, "last_completed": None}

def _background_sync():
    """Runs Notion fetch in background thread, writes to Supabase + file."""
    _sync_status["running"] = True
    try:
        import export_dashboard_data
        import importlib
        importlib.reload(export_dashboard_data)

        data = export_dashboard_data.get_dashboard_data()

        # Save to file
        DATA_FILE = 'dashboard_data.json'
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        # Save to Supabase
        try:
            import supabase_client
            supabase_client.upsert_dashboard(data)
        except Exception as e:
            print(f"[WARN] Background Supabase write failed: {e}")

        _sync_status["last_completed"] = time.time()
        print(f"[SUCCESS] Background sync completed.")
    except Exception as e:
        print(f"[ERROR] Background sync failed: {e}")
    finally:
        _sync_status["running"] = False

@app.route('/api/sync', methods=['POST'])
def sync_data():
    """Refresh button: read from Supabase (fast, real-time data from bot)."""
    print("[INFO] Sync Requested — reading from Supabase...")
    try:
        import supabase_client
        data = supabase_client.get_dashboard()
        if data and (data.get('members') or data.get('submissions')):
            return jsonify({"status": "success", "data": data})
    except Exception as e:
        print(f"[WARN] Supabase read failed: {e}")

    # Fallback: local file
    try:
        DATA_FILE = 'dashboard_data.json'
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return jsonify({"status": "success", "data": json.load(f)})
    except Exception:
        pass

    # Last resort: full Notion fetch
    print("[INFO] No cache available. Full Notion fetch...")
    try:
        import export_dashboard_data
        import importlib
        importlib.reload(export_dashboard_data)
        data = export_dashboard_data.get_dashboard_data()

        DATA_FILE = 'dashboard_data.json'
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        try:
            import supabase_client
            supabase_client.upsert_dashboard(data)
        except Exception:
            pass

        return jsonify({"status": "success", "data": data})
    except Exception as e:
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

@app.route('/api/sync-status', methods=['GET'])
def sync_status():
    return jsonify({
        "running": _sync_status["running"],
        "last_completed": _sync_status["last_completed"]
    })

@app.route('/api/full-sync', methods=['POST'])
def full_sync():
    """Cron job endpoint: full Notion fetch → Supabase + file (background reconciliation)."""
    print("[INFO] Full Notion sync triggered (cron)...")
    try:
        import export_dashboard_data
        import importlib
        importlib.reload(export_dashboard_data)

        data = export_dashboard_data.get_dashboard_data()

        DATA_FILE = 'dashboard_data.json'
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)

        try:
            import supabase_client
            supabase_client.upsert_dashboard(data)
        except Exception as e:
            print(f"[WARN] Supabase write failed: {e}")

        # Also refresh groups cache
        try:
            import notion_group_api
            importlib.reload(notion_group_api)
            groups = notion_group_api.get_all_group_tracks()
            with open(os.path.join(BASE_DIR, 'groups_cache.json'), 'w', encoding='utf-8') as f:
                json.dump(groups, f, ensure_ascii=False)
            print(f"[SUCCESS] Groups cache updated ({len(groups)} tracks)")
        except Exception as e:
            print(f"[WARN] Groups cache update failed: {e}")

        print(f"[SUCCESS] Full sync done. Members: {len(data.get('members', []))}, Subs: {len(data.get('submissions', []))}")
        return jsonify({"status": "success", "data": data})
    except Exception as e:
        print(f"[ERROR] Full sync failed: {e}")
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

@app.route('/api/status', methods=['GET'])
def get_bot_status():
    # Use absolute path finding based on this file's location
    HEARTBEAT_FILE = get_bot_heartbeat_file(BASE_DIR, explicit=env_info["env_name"])
    
    # print(f"[DEBUG] Checking heartbeat at: {HEARTBEAT_FILE}") # Optional debug log

    try:
        import time
        if not os.path.exists(HEARTBEAT_FILE):
            return jsonify({"status": "offline", "message": "No heartbeat file found", "last_seen_seconds_ago": -1})
            
        with open(HEARTBEAT_FILE, 'r') as f:
            data = json.load(f)
            
        last_seen = data.get('last_seen', 0)
        now = time.time()
        diff = now - last_seen
        
        status = "offline"
        if diff < 300: # Less than 5 mins
            status = "online" 
        elif diff < 900: # Less than 15 mins
            status = "delayed"
        else:
            status = "offline"
            
        return jsonify({
            "status": status,
            "last_seen_seconds_ago": int(diff),
            "last_seen_timestamp": last_seen
        })
    except Exception as e:
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

# [NEW] Command Queue for IPC
COMMAND_QUEUE_FILE = get_bot_command_queue_file(BASE_DIR, explicit=env_info["env_name"])

@app.route('/api/run-command', methods=['POST'])
def run_command_endpoint():
    """
    Triggers bot commands either via Subprocess (scripts) or IPC (Queue).
    Payload: { "command": "string", "cohort": "string", "force": bool }
    """
    # 🛡 F-3: admin 가드 — subprocess(manual_reassign_groups.py) 실행 + 봇 명령 큐잉을 운영진으로 제한.
    if not _is_admin_session():
        return jsonify({"status": "error", "message": "운영진 권한이 필요합니다."}), 403

    # 입력 검증 — cohort 값이 숫자여야 함 (정상 cohort '6', '9' 등). 옵션-look-alike 차단.
    data = request.json or {}
    raw_cohort = str(data.get('cohort', '6')).strip()
    if not re.fullmatch(r'\d+', raw_cohort):
        return jsonify({"status": "error", "message": "cohort 는 숫자여야 합니다."}), 400

    cmd_type = data.get('command')
    cohort = raw_cohort
    force = data.get('force', False)
    
    print(f"[INFO] Received Run Command: {cmd_type} (Cohort: {cohort}, Force: {force})")
    
    try:
        def run_script_and_notify(cmd, action_name):
            print(f"[BG] Running: {cmd}")
            try:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                
                success = (result.returncode == 0)
                output = result.stdout + "\n" + result.stderr
                
                # Simple summary
                status_emoji = "✅" if success else "❌"
                summary = f"{status_emoji} **{action_name}** {'성공' if success else '실패'}\n"
                
                # Extract last few lines for context
                lines = [l for l in output.split('\n') if l.strip()]
                last_lines = "\n".join(lines[-5:]) if lines else "No output"
                
                summary += f"```\n{last_lines}\n```"
                
                # Notify Admin via IPC
                notification = {
                     "id": str(time.time()),
                     "type": "notify_admin",
                     "payload": {
                         "message": summary,
                         "title": f"봇 명령 실행 결과: {action_name}",
                         "level": "success" if success else "error"
                     },
                     "status": "pending",
                     "created_at": time.time()
                }
                
                # Queue check (simple append logic needed if high traffic, but overwrite ok for now or append?)
                # Actually concurrent writes might be an issue. Let's start with overwrite for simplicity 
                # OR read-modify-write if we care about preservation. 
                # Given low traffic, overwrite is risky if multiple cmds run. 
                # BUT `bot_command_queue.json` is usually single item for this simple IPC.
                # Let's stick to overwrite as per current design.
                with open(COMMAND_QUEUE_FILE, 'w') as f:
                    json.dump(notification, f)
                    
                print(f"[BG] Finished {action_name}. Notification Queued.")
                
            except Exception as e:
                print(f"[BG] Execution Error: {e}")

        if cmd_type == 'reassign_groups':
            script_path = os.path.join(BASE_DIR, 'manual_reassign_groups.py')
            cmd = [sys.executable, script_path, cohort]
            if force:
                cmd.append('--force')
            
            threading.Thread(target=run_script_and_notify, args=(cmd, f"{cohort}기 조 배정")).start()
            return jsonify({"status": "success", "message": f"Started Group Reassignment for {cohort}."})

        elif cmd_type == 'sync_apps':
            script_path = os.path.join(BASE_DIR, 'manual_reassign_groups.py')
            cmd = [sys.executable, script_path, cohort]
            if force:
                cmd.append('--force')
            
            # [FIX] Use --sync-only to prevent accidental group reset
            cmd.append('--sync-only')
                
            threading.Thread(target=run_script_and_notify, args=(cmd, f"{cohort}기 신청서 동기화 (Sync Only)")).start()
            return jsonify({"status": "success", "message": f"Started App Sync for {cohort} (Members Only)."})

        elif cmd_type == 'sync_members':
            # IPC: Write to Queue
            command = {
                "id": str(time.time()),
                "type": "sync_members",
                "payload": {},
                "status": "pending",
                "created_at": time.time()
            }
            with open(COMMAND_QUEUE_FILE, 'w') as f:
                json.dump(command, f)
            return jsonify({"status": "success", "message": "Queued Member Sync command."})
            
        else:
            return jsonify({"status": "error", "message": "Unknown command"}), 400

    except Exception as e:
        print(f"[ERROR] Run Command Failed: {e}")
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

@app.route('/api/test-notification', methods=['POST'])
def trigger_test_notification():
    # 🛡 F-5: admin 가드 — 봇 이름으로 임의 사용자에게 DM 발송하는 기능을 운영진으로 제한.
    if not _is_admin_session():
        return jsonify({"status": "error", "message": "운영진 권한이 필요합니다."}), 403

    try:
        data = request.json
        target_id = data.get('targetId')
        msg_type = data.get('msgType')
        
        command = {
            "id": str(time.time()),
            "type": "test_notification",
            "payload": {
                "target_id": target_id,
                "msg_type": msg_type
            },
            "status": "pending",
            "created_at": time.time()
        }
        
        # Write to queue file (overwrite for simplicity as we handle one at a time mostly)
        with open(COMMAND_QUEUE_FILE, 'w') as f:
            json.dump(command, f)
            
        print(f"[INFO] Test command queued: {msg_type} for {target_id}")
        return jsonify({"status": "success", "message": "Test command queued"})
    except Exception as e:
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

@app.route('/api/groups', methods=['GET'])
def get_group_data():
    """
    Fetch all groups and members for the 'Group Management' tab.
    Structure:
    [
      {
        "trackName": "AI Agent Track",
        "groups": [
          {
            "groupName": "6기 1조",
            "dbId": "...",
            "members": [
              { "name": "...", "discordId": "...", "role": "조장", "profile": "..." }
            ]
          }
        ]
      },
      ...
    ]
    """
    # Priority 1: Local file cache (written by full-sync cron)
    try:
        groups_file = os.path.join(BASE_DIR, 'groups_cache.json')
        if os.path.exists(groups_file):
            with open(groups_file, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            if cached:
                return jsonify({"status": "success", "data": cached})
    except Exception:
        pass

    # Priority 2: Fetch from Notion (slow, first-time only)
    try:
        import notion_group_api
        import importlib
        importlib.reload(notion_group_api)

        result = notion_group_api.get_all_group_tracks()

        # Cache for next time
        try:
            with open(os.path.join(BASE_DIR, 'groups_cache.json'), 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception:
            pass

        return jsonify({"status": "success", "data": result})

    except Exception as e:
        print(f"[ERROR] Failed to fetch group data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500


# ── 비동기 commit job 인프라 ─────────────────────────────────────────
# Notion 순차 호출 + Discord IPC 가 30s+ 걸려서 Vercel 프록시 timeout 에 걸림.
# POST 는 즉시 jobId 반환 + 백그라운드 thread 가 작업, 클라이언트 폴링.
#
# Resilience: PM2 가 프로세스 재시작하면 in-memory dict 가 날아가서 클라이언트가
# '404 job not found' 보게 됨. 디스크에 영속화하고, 시작 시 '진행 중' 이던 job 은
# 'failed (서버 재시작)' 으로 마킹해서 클라가 명확한 메시지 받게 한다.
_COMMIT_JOBS = {}                       # jobId -> dict (status, phase, ...)
_COMMIT_JOBS_LOCK = threading.Lock()
_COMMIT_JOB_TTL_SECONDS = 3600          # 완료된 job 1시간 후 자동 정리
COMMIT_JOBS_FILE = os.path.join(
    BASE_DIR,
    f"commit_jobs_{TRACK_APPLICATION_CACHE_ENV}.json"
)


def _persist_commit_jobs_unlocked():
    """주의: caller 가 _COMMIT_JOBS_LOCK 보유 상태에서만 호출."""
    try:
        with open(COMMIT_JOBS_FILE, 'w', encoding='utf-8') as f:
            json.dump(_COMMIT_JOBS, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] persist commit jobs failed: {e}")


def _load_commit_jobs_on_startup():
    """프로세스 시작 시 호출. 'queued'/'running' 인 job 은 stale 처리."""
    if not os.path.exists(COMMIT_JOBS_FILE):
        return
    try:
        with open(COMMIT_JOBS_FILE, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            return
        now = time.time()
        with _COMMIT_JOBS_LOCK:
            for jid, job in loaded.items():
                if not isinstance(job, dict):
                    continue
                if job.get('status') in (None, 'queued', 'running'):
                    job['status'] = 'failed'
                    job['phase'] = 'failed'
                    job['error'] = '서버가 재시작되어 작업이 중단됐습니다. 다시 시도해주세요.'
                    job['completedAt'] = now
                _COMMIT_JOBS[jid] = job
            stale_count = sum(1 for j in _COMMIT_JOBS.values() if j.get('error') and '재시작' in str(j.get('error') or ''))
        print(f"[INFO] commit jobs restored from disk: {len(_COMMIT_JOBS)} (in-flight→failed: {stale_count})")
    except Exception as e:
        print(f"[WARN] load commit jobs failed: {e}")


def _make_commit_job(owner_user_id):
    job_id = str(uuid.uuid4())
    job = {
        'jobId': job_id,
        'status': 'queued',          # queued | running | completed | failed
        'phase': 'queued',           # notion_members | notion_groups | discord_sync | done
        'phaseDetail': '',
        'tracksProcessed': 0,
        'tracksTotal': 0,
        'startedAt': time.time(),
        'completedAt': None,
        'result': None,
        'error': None,
        'ownerUserId': str(owner_user_id or '').strip(),
    }
    with _COMMIT_JOBS_LOCK:
        _COMMIT_JOBS[job_id] = job
        _persist_commit_jobs_unlocked()
    return job


def _update_commit_job(job_id, **fields):
    with _COMMIT_JOBS_LOCK:
        job = _COMMIT_JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        _persist_commit_jobs_unlocked()


def _get_commit_job(job_id):
    with _COMMIT_JOBS_LOCK:
        job = _COMMIT_JOBS.get(job_id)
        if job is None:
            return None
        return dict(job)


def _cleanup_old_commit_jobs():
    now = time.time()
    with _COMMIT_JOBS_LOCK:
        stale = []
        for jid, j in _COMMIT_JOBS.items():
            ended = j.get('completedAt')
            if ended and (now - ended) > _COMMIT_JOB_TTL_SECONDS:
                stale.append(jid)
        for jid in stale:
            del _COMMIT_JOBS[jid]
        if stale:
            _persist_commit_jobs_unlocked()


# 모듈 로드 시점에 디스크 → 메모리 복원.
_load_commit_jobs_on_startup()


def _run_commit_job_async(job_id, payload):
    """백그라운드 thread 진입점 — 진행상황을 _COMMIT_JOBS 에 기록."""
    def _on_progress(phase, detail, extra):
        update = {'phase': phase, 'phaseDetail': detail or ''}
        if isinstance(extra, dict):
            if 'tracksProcessed' in extra:
                update['tracksProcessed'] = int(extra['tracksProcessed'])
            if 'tracksTotal' in extra:
                update['tracksTotal'] = int(extra['tracksTotal'])
        _update_commit_job(job_id, **update)

    _update_commit_job(job_id, status='running', phase='notion_members',
                       phaseDetail='Notion 처리 시작')
    try:
        result = _commit_group_preview_to_notion(payload, progress_callback=_on_progress)
        _update_commit_job(
            job_id,
            status='completed',
            phase='done',
            phaseDetail='완료',
            result=result,
            completedAt=time.time(),
        )
    except ValueError as e:
        _update_commit_job(
            job_id,
            status='failed',
            error=str(e),
            errorKind='validation',
            completedAt=time.time(),
        )
        print(f"[ERROR] Commit job {job_id} validation failed: {e}")
    except Exception as e:
        _update_commit_job(
            job_id,
            status='failed',
            error=str(e),
            errorKind='internal',
            completedAt=time.time(),
        )
        print(f"[ERROR] Commit job {job_id} failed: {e}")


@app.route('/api/admin/reset-track-applications', methods=['POST'])
def reset_track_applications_mockup():
    """
    [관리자 전용] track-application 관련 서버 캐시를 모두 비웁니다.
    데모 시연 / 테스트 반복 시 '처음부터 다시' 용도.

    비우는 캐시 (test/prod 환경별):
      1) track_applications_cache_*.json
         (실 Discord 사용자가 OAuth 로 제출한 신청서)
      2) track_applications_admin_mock_cache_*.json
         (admin 편집 모달로 추가/수정한 mockup 멤버)

    ⚠️ Discord 채널·역할 / Notion 페이지는 이 endpoint 가 건드리지 않습니다.
       이전 기수 트랙 채널 정리는 디스코드에서 `!채널삭제 <기수>` 봇 명령으로 별도 실행
       (공지·카테고리·역할은 보존됨).
    """
    if not _is_admin_session():
        return jsonify({
            "status": "error",
            "message": "운영진 권한이 필요합니다. 관리자 디스코드 계정으로 로그인하세요."
        }), 403

    try:
        # 1) admin mockup 캐시 — 멤버 row 단위 카운트
        cleared_mock_members = 0
        existing_mock = _read_track_application_admin_mock_cache() or {}
        for _, bucket in (existing_mock.get('cohorts') or {}).items():
            members = (bucket or {}).get('members') or []
            cleared_mock_members += len(members) if isinstance(members, list) else 0
        _write_track_application_admin_mock_cache({'cohorts': {}})

        # 2) 실 사용자 신청 캐시 — application 단위 카운트
        cleared_real_applications = 0
        existing_real = _read_track_application_cache() or {}
        for _, bucket in (existing_real.get('cohorts') or {}).items():
            apps = (bucket or {}).get('applications') or {}
            cleared_real_applications += len(apps) if isinstance(apps, dict) else 0
        _write_track_application_cache_file(TRACK_APPLICATION_CACHE_FILE, {'cohorts': {}})

        total = cleared_mock_members + cleared_real_applications
        return jsonify({
            "status": "success",
            "message": "트랙 신청 캐시 초기화 완료 (실 사용자 + admin mockup)",
            "clearedMembers": total,
            "clearedRealApplications": cleared_real_applications,
            "clearedMockMembers": cleared_mock_members,
            "note": "이전 기수 트랙 채널은 디스코드에서 `!채널삭제 <기수>` 명령으로 별도 정리하세요 (공지·카테고리·역할 보존).",
        })
    except Exception as e:
        print(f"[ERROR] reset-track-applications failed: {e}")
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500


@app.route('/api/mockups/group-preview/commit', methods=['POST'])
def commit_group_preview_mockup():
    """
    조 배정 commit — 비동기 패턴.
    실제 Notion + Discord 작업은 백그라운드 thread 에서 실행되고, 즉시 jobId 만 반환.
    Vercel 프록시 30s timeout 회피 + 진행상황 폴링 가능.
    클라이언트는 GET /api/mockups/group-preview/commit/status?jobId=X 로 폴링.
    """
    if not _is_admin_session():
        return jsonify({
            "status": "error",
            "message": "운영진 권한이 필요합니다. 관리자 디스코드 계정으로 로그인하세요."
        }), 403

    payload = request.get_json(silent=True) or {}

    # 빠른 입력 검증 — 잘못된 payload 면 thread 띄우기 전에 4xx
    if payload.get('target') != 'notion-test':
        return jsonify({"status": "error", "message": "Only the test Notion target is allowed."}), 400
    if not (payload.get('members') or []):
        return jsonify({"status": "error", "message": "No mock members were provided."}), 400
    if not (payload.get('tracks') or []):
        return jsonify({"status": "error", "message": "No group assignment payload was provided."}), 400

    _cleanup_old_commit_jobs()

    user = session.get('discord_user') or {}
    job = _make_commit_job(owner_user_id=user.get('id'))

    thread = threading.Thread(
        target=_run_commit_job_async,
        args=(job['jobId'], payload),
        daemon=True,
        name=f"commit-job-{job['jobId'][:8]}",
    )
    thread.start()

    return jsonify({
        "status": "accepted",
        "jobId": job['jobId'],
        "message": "조 배정 commit 시작 — 진행상황은 status endpoint 로 폴링하세요.",
    }), 202


@app.route('/api/mockups/group-preview/commit/status', methods=['GET'])
def commit_group_preview_status():
    """비동기 commit job 의 현재 상태."""
    if not _is_admin_session():
        return jsonify({
            "status": "error",
            "message": "운영진 권한이 필요합니다."
        }), 403

    job_id = (request.args.get('jobId') or '').strip()
    if not job_id:
        return jsonify({"status": "error", "message": "jobId 파라미터 필요"}), 400

    job = _get_commit_job(job_id)
    if job is None:
        return jsonify({"status": "error", "message": "job not found (만료됐거나 잘못된 jobId)"}), 404

    return jsonify(job)

@app.route('/api/drop-stats', methods=['GET'])
def get_drop_stats():
    """
    트랙별 탈락 현황 통계를 반환합니다.
    마스터 DB에서 기타사항의 탈락 기록과 활동 상태를 기반으로 집계합니다.
    """
    try:
        import notion_api
        import importlib
        importlib.reload(notion_api)
        import re

        all_members = notion_api.get_all_members()
        current_cohort = os.getenv('CURRENT_COHORT', '6')

        from datetime import datetime, date, timedelta
        cohort_start = os.getenv('COHORT_START_DATE', '2026-02-11')
        cohort_start_date = datetime.strptime(cohort_start, '%Y-%m-%d').date()

        # 트랙별 전체 인원 (현재 기수만)
        track_totals = {}   # track_name -> set of member_ids (원래 소속)
        track_dropped = {}  # track_name -> list of dropped member info
        dropped_members = []

        for member in all_members:
            props = member.get('properties', {})

            # 기수 필터
            cohort_prop = props.get('기수', {}).get('select', {})
            cohort_val = cohort_prop.get('name', '') if cohort_prop else ''
            if current_cohort not in cohort_val:
                continue

            member_id = member['id']
            name_prop = props.get('디스코드 닉네임', {}).get('title', [])
            member_name = name_prop[0]['text']['content'] if name_prop else 'Unknown'

            # 현재 트랙
            current_tracks = [t['name'] for t in props.get('트랙', {}).get('multi_select', [])]

            # 활동 상태
            status_prop = props.get('활동 상태', {}).get('status', {})
            activity_status = status_prop.get('name', '') if status_prop else ''

            # 기타사항에서 탈락 기록 파싱
            notes = props.get('기타사항', {}).get('rich_text', [])
            notes_text = ''.join([t.get('plain_text', '') for t in notes]).strip()

            # 🚫 6기 AI 에이전트 트랙 탈락(4주차) 또는 탈락(2026-03-12) 패턴 파싱
            drop_pattern = rf'🚫\s*{current_cohort}기\s+(.+?)\s+탈락\(([^)]+)\)'
            drop_matches = re.findall(drop_pattern, notes_text)

            for track_name, drop_info in drop_matches:
                track_name = track_name.strip()

                # drop_info를 주차 번호로 변환 (날짜 or N주차)
                drop_date = drop_info.strip()
                drop_week = 0
                if re.match(r'\d{4}-\d{2}-\d{2}', drop_date):
                    # 날짜 형식 → 주차 계산
                    try:
                        from datetime import datetime
                        d = datetime.strptime(drop_date, '%Y-%m-%d').date()
                        drop_week = max(1, ((d - cohort_start_date).days // 7) + 1)
                    except:
                        pass
                elif '주차' in drop_date:
                    # N주차 형식
                    week_match = re.search(r'(\d+)', drop_date)
                    if week_match:
                        drop_week = int(week_match.group(1))
                        # 주차를 대표 날짜로 변환
                        drop_date = (cohort_start_date + timedelta(weeks=drop_week-1)).isoformat()

                if track_name not in track_totals:
                    track_totals[track_name] = set()
                track_totals[track_name].add(member_id)

                if track_name not in track_dropped:
                    track_dropped[track_name] = []
                track_dropped[track_name].append({
                    "memberId": member_id,
                    "name": member_name,
                    "track": track_name,
                    "droppedDate": drop_date,
                    "droppedWeek": drop_week,
                    "activityStatus": activity_status
                })

                dropped_members.append({
                    "memberId": member_id,
                    "name": member_name,
                    "track": track_name,
                    "droppedDate": drop_date,
                    "droppedWeek": drop_week,
                    "activityStatus": activity_status
                })

            # 현재 활동 중인 트랙도 전체 인원에 포함
            for t in current_tracks:
                if t not in track_totals:
                    track_totals[t] = set()
                track_totals[t].add(member_id)

        # 트랙별 통계 생성
        track_stats = []
        for track_name, member_ids in track_totals.items():
            total = len(member_ids)
            dropped = len(track_dropped.get(track_name, []))
            active = total - dropped
            rate = (dropped / total * 100) if total > 0 else 0
            track_stats.append({
                "track": track_name,
                "total": total,
                "active": active,
                "dropped": dropped,
                "dropRate": round(rate, 1),
                "droppedMembers": track_dropped.get(track_name, [])
            })

        track_stats.sort(key=lambda x: x['dropRate'], reverse=True)

        total_members = len(set(m['memberId'] for m in dropped_members) |
                          {mid for ids in track_totals.values() for mid in ids})
        total_dropped = len(set(m['memberId'] for m in dropped_members))

        # 주차별 탈락 분석
        weekly_drops = {}  # week_num -> { track_name -> count }
        weekly_totals = {}  # week_num -> total count

        for dm in dropped_members:
            week_num = dm.get('droppedWeek', 0)
            if week_num == 0:
                try:
                    drop_d = datetime.strptime(dm['droppedDate'], '%Y-%m-%d').date()
                    week_num = max(1, ((drop_d - cohort_start_date).days // 7) + 1)
                except:
                    week_num = 0

            if week_num not in weekly_drops:
                weekly_drops[week_num] = {}
                weekly_totals[week_num] = 0

            track = dm['track']
            weekly_drops[week_num][track] = weekly_drops[week_num].get(track, 0) + 1
            weekly_totals[week_num] = weekly_totals[week_num] + 1

        # 전체 주차 범위 계산
        cohort_end = os.getenv('COHORT_END_DATE', '2026-03-15')
        cohort_end_date = datetime.strptime(cohort_end, '%Y-%m-%d').date()
        total_weeks = max(1, ((cohort_end_date - cohort_start_date).days // 7) + 1)

        all_track_names = list(track_totals.keys())
        weekly_analysis = []
        for w in range(1, total_weeks + 1):
            week_start = cohort_start_date + timedelta(weeks=w-1)
            week_end = week_start + timedelta(days=6)
            by_track = weekly_drops.get(w, {})
            weekly_analysis.append({
                "week": w,
                "weekLabel": f"{w}주차",
                "dateRange": f"{week_start.strftime('%m/%d')}~{week_end.strftime('%m/%d')}",
                "total": weekly_totals.get(w, 0),
                "byTrack": {t: by_track.get(t, 0) for t in all_track_names}
            })

        # 위험 주차 분석 (탈락이 가장 많은 주차)
        peak_week = max(weekly_analysis, key=lambda x: x['total']) if weekly_analysis else None

        return jsonify({
            "status": "success",
            "summary": {
                "totalMembers": total_members,
                "totalDropped": total_dropped,
                "overallDropRate": round((total_dropped / total_members * 100) if total_members > 0 else 0, 1),
                "peakWeek": peak_week['weekLabel'] if peak_week and peak_week['total'] > 0 else "-",
                "peakWeekDrops": peak_week['total'] if peak_week else 0
            },
            "trackStats": track_stats,
            "weeklyAnalysis": weekly_analysis,
            "allTracks": all_track_names,
            "recentDrops": sorted(dropped_members, key=lambda x: x['droppedDate'], reverse=True)[:20]
        })

    except Exception as e:
        print(f"[ERROR] Drop stats failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

def _prefetch_member_info(member_id):
    """Notion 페이지 ID로 멤버 이름/Discord 정보를 조회 (Discord 처리용)"""
    try:
        import requests as req
        notion_headers = {
            "Authorization": f"Bearer {os.getenv('NOTION_TOKEN')}",
            "Notion-Version": "2022-06-28",
        }
        resp = req.get(
            f"https://api.notion.com/v1/pages/{member_id}",
            headers=notion_headers,
        )
        if not resp.ok:
            return None
        props = resp.json().get("properties", {})
        # Use '이름' (rich_text) for dropout_handler search, not '디스코드 닉네임'
        name_parts = props.get("이름", {}).get("rich_text", [])
        name = name_parts[0]["text"]["content"] if name_parts else None
        if not name:
            # Fallback to 디스코드 닉네임 (title)
            title_parts = props.get("디스코드 닉네임", {}).get("title", [])
            name = title_parts[0]["text"]["content"] if title_parts else None
        return name
    except Exception as e:
        print(f"[WARN] _prefetch_member_info failed: {e}")
        return None


def _prefetch_group_info(member_id, track_name):
    """groups_cache.json에서 해당 멤버의 조 번호와 조장 Discord ID를 조회"""
    import re as _re
    group_str = ""
    leader_discord_id = ""
    try:
        groups_file = os.path.join(BASE_DIR, "groups_cache.json")
        if not os.path.exists(groups_file):
            return group_str, leader_discord_id
        with open(groups_file, "r", encoding="utf-8") as f:
            all_groups = json.load(f)

        # trackConfig에서 track_name에 해당하는 groupDbName 조회
        group_db_name = track_name  # 기본값
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            for tc in config.get("trackConfig", []):
                if tc.get("notionName") == track_name:
                    group_db_name = tc.get("groupDbName", track_name)
                    break
        except Exception:
            pass

        for track_data in all_groups:
            if track_data.get("trackName") != group_db_name:
                continue
            for group in track_data.get("groups", []):
                member_found = False
                for m in group.get("members", []):
                    if m.get("id") == member_id:
                        member_found = True
                        break
                if not member_found:
                    continue
                # 조 번호 추출 (예: "6기 1조" → "1")
                match = _re.search(r"(\d+)조", group.get("groupName", ""))
                if match:
                    group_str = match.group(1)
                # 같은 조에서 조장 찾기
                for lm in group.get("members", []):
                    if lm.get("role") == "조장" and lm.get("id") != member_id:
                        leader_discord_id = lm.get("discordId", "")
                        break
                return group_str, leader_discord_id
    except Exception as e:
        print(f"[WARN] _prefetch_group_info failed: {e}")
    return group_str, leader_discord_id


@app.route('/api/drop-track', methods=['POST'])
def drop_member_from_track():
    """
    특정 멤버를 지정된 트랙에서 탈락 처리합니다.
    - 마스터 DB에서 해당 트랙만 제거 (다른 트랙은 유지)
    - 조 DB에서 해당 멤버 삭제
    - Discord 역할 제거 + 탈락자/조장 DM 발송
    - 대시보드 캐시 갱신

    Payload: {"memberId": "...", "trackName": "AI 에이전트 트랙"}
    """
    # 🛡 F-4: admin 가드 — 멤버 탈락(Notion 변경 + Discord 역할 박탈 + DM 발송)은 운영진 전용.
    if not _is_admin_session():
        return jsonify({"status": "error", "message": "운영진 권한이 필요합니다."}), 403

    try:
        data = request.json
        member_id = data.get('memberId')
        track_name = data.get('trackName')

        if not member_id or not track_name:
            return jsonify({"status": "error", "message": "memberId와 trackName이 필요합니다."}), 400

        print(f"[DROP] Processing drop request: member={member_id}, track={track_name}")

        # 0. Discord 처리를 위한 정보 사전 조회 (Notion 변경 전에!)
        member_name = _prefetch_member_info(member_id)
        group_str, leader_discord_id = _prefetch_group_info(member_id, track_name)
        print(f"[DROP] Pre-fetched: name={member_name}, group={group_str}, leader={leader_discord_id}")

        import notion_api
        import importlib
        importlib.reload(notion_api)

        # 1. 마스터 DB에서 해당 트랙 제거
        result = notion_api.drop_member_from_track(member_id, track_name)
        if not result['success']:
            return jsonify({"status": "error", "message": result['message']}), 400

        dropped_tracks = [track_name]

        # 1-1. 연동 탈락 처리 (trackConfig의 linkedDropTracks 기반)
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            track_configs = config.get('trackConfig', [])
            linked = []
            for tc in track_configs:
                if tc['notionName'] == track_name:
                    linked = tc.get('linkedDropTracks', [])
                    break
            for linked_track in linked:
                linked_result = notion_api.drop_member_from_track(member_id, linked_track)
                if linked_result['success']:
                    dropped_tracks.append(linked_track)
                    print(f"[DROP] Also dropped linked track '{linked_track}': {linked_result['message']}")
                    result['remaining_tracks'] = linked_result['remaining_tracks']
                else:
                    print(f"[DROP] Linked drop '{linked_track}' skipped: {linked_result['message']}")
        except Exception as e:
            print(f"[WARN] Failed to load trackConfig for linked drops: {e}")

        # 2. 조 DB에서 멤버 삭제 (크리에이터 숏폼/롱폼 모두 같은 '크리에이터 트랙' 조 DB)
        group_result = notion_api.find_and_remove_from_group_db(member_id, track_name)
        print(f"[DROP] Group DB result: {group_result['message']}")

        # 3. Discord 처리: 역할 제거 + 탈락자 DM + 조장 DM (백그라운드)
        if member_name:
            generation = os.getenv('CURRENT_COHORT', '6') + '기'

            def run_discord_dropout():
                try:
                    script_path = os.path.join(BASE_DIR, 'dropout_handler.py')
                    if not os.path.exists(script_path):
                        print(f"[WARN] dropout_handler.py not found at {script_path}")
                        return

                    cmd = [
                        sys.executable, script_path,
                        member_name,
                        '--track', track_name,
                        '--discord-only',
                        '--generation', generation,
                    ]
                    if group_str:
                        cmd.extend(['--group', group_str])

                    print(f"[DROP] Running Discord dropout: {' '.join(cmd)}")
                    proc = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=120,
                    )
                    if proc.stdout:
                        print(f"[DROP] Discord stdout:\n{proc.stdout}")
                    if proc.stderr:
                        print(f"[DROP] Discord stderr:\n{proc.stderr}")
                    if proc.returncode == 0:
                        print(f"[DROP] Discord processing completed for {member_name}")
                    else:
                        print(f"[DROP] Discord processing failed (rc={proc.returncode})")
                except subprocess.TimeoutExpired:
                    print(f"[WARN] Discord dropout timed out for {member_name}")
                except Exception as e:
                    print(f"[WARN] Discord dropout failed: {e}")

            threading.Thread(target=run_discord_dropout, daemon=True).start()
        else:
            print(f"[WARN] Skipping Discord processing: member name not found for {member_id}")

        # 4. 대시보드 캐시 갱신 (백그라운드)
        def refresh_cache():
            try:
                import export_dashboard_data
                importlib.reload(export_dashboard_data)
                dashboard_data = export_dashboard_data.get_dashboard_data()

                with open(os.path.join(BASE_DIR, 'dashboard_data.json'), 'w', encoding='utf-8') as f:
                    json.dump(dashboard_data, f, indent=4, ensure_ascii=False)

                try:
                    import supabase_client
                    supabase_client.upsert_dashboard(dashboard_data)
                except Exception:
                    pass

                # 조 캐시도 갱신
                try:
                    import notion_group_api
                    importlib.reload(notion_group_api)
                    groups = notion_group_api.get_all_group_tracks()
                    with open(os.path.join(BASE_DIR, 'groups_cache.json'), 'w', encoding='utf-8') as f:
                        json.dump(groups, f, ensure_ascii=False)
                except Exception:
                    pass

                print(f"[DROP] Cache refreshed after drop.")
            except Exception as e:
                print(f"[WARN] Cache refresh failed: {e}")

        threading.Thread(target=refresh_cache, daemon=True).start()

        return jsonify({
            "status": "success",
            "message": result['message'],
            "remainingTracks": result['remaining_tracks'],
            "droppedTracks": dropped_tracks,
            "groupMessage": group_result['message']
        })

    except Exception as e:
        print(f"[ERROR] Drop member failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

@app.route('/api/member/<user_id>', methods=['GET'])
def get_member_detail(user_id):
    """멤버 상세 정보 API (개인정보 + 조 + 제출 내역).

    🛡 IDOR 가드: 본인(viewer.id == user_id) 또는 운영진(_is_admin) 만 조회 허용.
    무인증/타인 조회 차단 — Discord ID enumerate 로 PII 수집되는 사고 방지.
    """
    viewer = _get_authenticated_discord_user()
    if not viewer:
        return jsonify({"status": "error", "message": "Authentication required."}), 401
    viewer_id = str(viewer.get('id', '')).strip()
    target_id = str(user_id or '').strip()
    if viewer_id != target_id and not _is_admin(viewer):
        return jsonify({"status": "error", "message": "Forbidden."}), 403

    try:
        import notion_api
        import importlib
        importlib.reload(notion_api)

        detail = notion_api.get_member_detail(user_id)
        if not detail:
            return jsonify({"status": "error", "message": "멤버를 찾을 수 없습니다."}), 404

        return jsonify({"status": "success", "data": detail})

    except Exception as e:
        print(f"[ERROR] Member detail failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": _safe_error_message(e)}), 500

@app.route('/member/<user_id>')
def member_detail_page(user_id):
    """멤버 상세 페이지 서빙"""
    html_path = os.path.join(BASE_DIR, 'static', 'member_detail.html')
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return f.read()
    return "Page not found", 404

if __name__ == '__main__':
    print("[INFO] Admin Server Starting...")

    print("[INFO] Admin Server Running on http://0.0.0.0:8000")
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8001')), debug=False)
