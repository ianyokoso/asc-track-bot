import requests
import os
import re
import json
from datetime import datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from env_utils import load_backend_env

# Load Environment (Ensure imports are above)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_INFO = load_backend_env(BASE_DIR)

def create_retry_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

SESSION = create_retry_session()
TIMEOUT = 20

# Dashboard Enum Mapping (Synced with export_dashboard_data.py)
TRACK_MAPPING = {
    "크리에이터 숏폼 트랙": "Shortform",
    "숏폼 과제": "Shortform",
    "크리에이터 롱폼 트랙": "Longform",
    "롱폼 과제": "Longform",
    "빌더 기초 트랙": "Builder Basic",
    "빌더 기초 과제": "Builder Basic",
    "빌더 심화 트랙": "Builder Advanced",
    "빌더 심화 과제": "Builder Advanced",
    "세일즈 실전 트랙": "Sales",
    "세일즈 과제": "Sales",
    "세일즈 실전 과제": "Sales",
    "AI 에이전트 트랙": "AI Agent",
    "에이전트 과제": "AI Agent",
    "AI 에이전트 과제": "AI Agent",
    "앱 개발 트랙": "App Dev",
    "앱 개발 과제": "App Dev",
    "나 탐구 트랙": "Self discovery",
    "나 탐구 과제": "Self discovery",
    "크리에이터 라이트 트랙 (숏폼)": "Shortform",
    "크리에이터 라이트 트랙 (롱폼)": "Longform",
    "빌더 라이트 트랙 (기초)": "Builder Basic",
    "빌더 라이트 트랙 (심화)": "Builder Advanced"
}

# Display names for reports
DISPLAY_TRACK_MAPPING = {
    "Shortform": "숏폼",
    "Longform": "롱폼",
    "Builder Basic": "빌더 기초",
    "Builder Advanced": "빌더 심화",
    "Sales": "세일즈 실전",
    "AI Agent": "AI 에이전트",
    "App Dev": "앱 개발",
    "Self discovery": "나 탐구"
}

def get_track_display_name(internal_name):
    """Returns Korean display name for an internal track ID"""
    return DISPLAY_TRACK_MAPPING.get(internal_name, internal_name)

def map_track(notion_track_name):
    """Maps Notion track string to Dashboard internal ID"""
    if not notion_track_name: return "Unassigned"
    for key, value in TRACK_MAPPING.items():
        if key in notion_track_name:
            return value
    return "Unassigned"

# Configuration Constants
TIMEOUT = 20
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
TRACK_JO_DB_ID = os.getenv('TRACK_JO_DB_ID')
SUBMISSIONS_DB_ID = os.getenv('SUBMISSIONS_DB_ID')

def get_page_title(page):
    """노션 페이지의 타이틀 속성 값을 반환 (속성명 무관)"""
    props = page.get('properties', {})
    for prop_data in props.values():
        if prop_data.get('type') == 'title':
            title_list = prop_data.get('title', [])
            if title_list:
                return title_list[0].get('text', {}).get('content', '')
    return None

def get_property_value(page, *keys):
    """
    Safely retrieves a property value from a Notion page object.
    Tries keys in order. Returns the first found content or "Unknown".
    Supports: Title, RichText, MultiSelect (joined by comma).
    """
    props = page.get('properties', {})
    for key in keys:
        if key not in props:
            continue
        
        prop_data = props[key]
        prop_type = prop_data['type']
        
        try:
            if prop_type in ['title', 'rich_text']:
                content_list = prop_data.get(prop_type, [])
                if content_list:
                    return content_list[0].get('text', {}).get('content', '')
            elif prop_type == 'select':
                sel = prop_data.get('select')
                return sel.get('name') if sel else None
            elif prop_type == 'multi_select':
                options = prop_data.get('multi_select', [])
                return ", ".join([opt['name'] for opt in options])
            elif prop_type == 'status':
                stat = prop_data.get('status')
                return stat.get('name') if stat else None
            elif prop_type == 'relation':
                 # Not typically used for text retrieval
                 pass
        except (IndexError, KeyError):
            continue
            
    return None

def get_headers():
    return {
        'Authorization': f"Bearer {NOTION_TOKEN}",
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }

def fetch_all_pages(url, payload):
    """
    Pagination Helper to fetch ALL pages from Notion API.
    Handles 'has_more' and 'next_cursor'.
    """
    all_results = []
    has_more = True
    next_cursor = None
    
    while has_more:
        if next_cursor:
            payload["start_cursor"] = next_cursor
            
        try:
            response = SESSION.post(url, headers=get_headers(), json=payload, timeout=TIMEOUT)
            
            if response.status_code != 200:
                print(f"[ERROR] API Request Failed ({url}): {response.status_code} - {response.text}")
                break
                
            data = response.json()
            results = data.get('results', [])
            all_results.extend(results)
            
            has_more = data.get('has_more', False)
            next_cursor = data.get('next_cursor')
            
        except Exception as e:
            print(f"[ERROR] Exception during API fetch: {e}")
            break
            
    return all_results

def get_user_by_discord_id(discord_id):
    """사용자 ID로 멤버 DB에서 사용자 정보 조회"""
    db_id = TRACK_JO_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    data = {"filter": {"property": "사용자 ID", "rich_text": {"equals": str(discord_id)}}}
    response = SESSION.post(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    results = response.json().get('results', [])
    if results:
        return results[0]
    return None

def find_existing_submission(assignment_page_id, member_page_id, date_str, assignment_type=None):
    """
    같은 유저 + 같은 과제 + 같은 날짜 + 같은 과제 타입에 이미 제출한 기록이 있는지 조회.
    있으면 가장 최근 제출물의 page_id를 반환, 없으면 None.
    """
    db_id = SUBMISSIONS_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    filters = [
        {"property": "과제 페이지 DB", "relation": {"contains": assignment_page_id}},
        {"property": "제출자", "relation": {"contains": member_page_id}},
        {"property": "제출 날짜", "date": {"equals": date_str}},
    ]
    if assignment_type:
        filters.append({"property": "과제 타입", "multi_select": {"contains": assignment_type}})
    payload = {
        "filter": {"and": filters},
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        "page_size": 1
    }
    try:
        response = SESSION.post(url, headers=get_headers(), json=payload, timeout=TIMEOUT)
        if response.status_code == 200:
            results = response.json().get('results', [])
            if results:
                return results[0]['id']
    except Exception as e:
        print(f"[WARN] find_existing_submission failed: {e}")
    return None

def update_submission(page_id, link, username, assignment_type, content="", images=None):
    """기존 제출물을 최신 내용으로 업데이트"""
    url = f'https://api.notion.com/v1/pages/{page_id}'

    link_prop = {"url": link} if link else None
    content_prop = [{"text": {"content": content}}] if content else []

    data = {
        "properties": {
            "제출물 제목": {"title": [{"text": {"content": f"[{username}] {assignment_type}"}}]},
            "과제 타입": {"multi_select": [{"name": assignment_type}]},
            "과제 내용 ": {"rich_text": content_prop},
        }
    }

    if link_prop:
        data["properties"]["링크"] = link_prop

    if images:
        files_data = []
        for img_url in images:
            files_data.append({
                "name": img_url.split('/')[-1].split('?')[0][:50],
                "type": "external",
                "external": {"url": img_url}
            })
        data["properties"]["이미지"] = {"files": files_data}

    response = SESSION.patch(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    if response.status_code != 200:
        print(f"[ERROR] Notion Submission Update Failed: {response.text}")
        return False

    print(f"[UPDATE] Existing submission updated for {username} ({page_id})")
    return True

def create_submission(assignment_page_id, member_page_id, link, username, assignment_type, content="", images=None):
    """제출 DB에 새로운 페이지 생성"""
    today_kst = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y-%m-%d')

    db_id = SUBMISSIONS_DB_ID
    url = 'https://api.notion.com/v1/pages'

    link_prop = {"url": link} if link else None
    content_prop = [{"text": {"content": content}}] if content else []

    data = {
        "parent": {"database_id": db_id},
        "properties": {
            "제출물 제목": {"title": [{"text": {"content": f"[{username}] {assignment_type}"}}]},
            "제출자": {"relation": [{"id": member_page_id}]},
            "과제 페이지 DB": {"relation": [{"id": assignment_page_id}]},
            "과제 타입": {"multi_select": [{"name": assignment_type}]},
            "과제 내용 ": {"rich_text": content_prop},
            "제출 날짜": {"date": {"start": today_kst}},
        }
    }

    if link_prop:
        data["properties"]["링크"] = link_prop

    if images:
        files_data = []
        for img_url in images:
            files_data.append({
                "name": img_url.split('/')[-1].split('?')[0][:50],
                "type": "external",
                "external": {"url": img_url}
            })
        data["properties"]["이미지"] = {"files": files_data}

    response = SESSION.post(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    if response.status_code != 200:
        print(f"[ERROR] Notion Submission Failed: {response.text}")
        return None

    # 생성된 페이지 ID 반환 (캐시용)
    created_page_id = response.json().get('id')
    _update_supabase_cache(member_page_id, today_kst, assignment_type, link, content, images, username)
    return created_page_id

_TRACK_DUE_WEEKDAY_PY = {
    # Python weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    "App Development Track": 2,  # Wednesday
    "Self Inquiry Track": 5,     # Saturday
    "Builder Basic": 6,          # Sunday
    "Builder Advanced": 6,
    "Sales": 6,
    "AI Agent": 6,
    "Longform": 6,
    # Shortform (daily) → no alignment, keep today_kst
}


def _align_dashboard_date(track_display_name, today_kst_str):
    """대시보드 컬럼에 매칭되도록 제출 날짜를 트랙별 마감 요일로 정렬.
    - Shortform/미매핑 트랙: 그대로 반환
    - 그 외 weekly 트랙: 해당 주차의 due 요일로 정렬 (코호트 기준)
    """
    target_wd = _TRACK_DUE_WEEKDAY_PY.get(track_display_name)
    if target_wd is None:
        return today_kst_str

    try:
        from utils.helpers import calculate_week_number, calculate_week_sunday, calculate_self_inquiry_due_date, calculate_app_dev_due_date
        cohort_start = os.getenv('COHORT_START_DATE')
        if not cohort_start:
            return today_kst_str
        today_dt = datetime.strptime(today_kst_str, '%Y-%m-%d').date()
        week_num = calculate_week_number(cohort_start, today_dt)
        if week_num < 1:
            return today_kst_str

        if track_display_name == "Self Inquiry Track":
            return calculate_self_inquiry_due_date(cohort_start, week_num) or today_kst_str
        if track_display_name == "App Development Track":
            # 앱개발은 week N due 가 cohort week N+1 의 수요일이라 주차 1:1 매핑이 안 됨.
            # 제출일 기준 '다음 도래하는 수요일 due' 를 타겟으로.
            for w in range(1, 21):
                due = calculate_app_dev_due_date(cohort_start, w)
                if not due:
                    break
                if today_dt <= datetime.strptime(due, '%Y-%m-%d').date():
                    return due
            return today_kst_str
        # Sunday-due weekly 트랙
        return calculate_week_sunday(cohort_start, week_num) or today_kst_str
    except Exception:
        return today_kst_str


def _update_supabase_cache(member_page_id, today_kst, assignment_type, link, content, images, username):
    """Real-time Supabase cache update (non-blocking, best-effort)"""
    try:
        import supabase_client
        from export_dashboard_data import map_track
        track = map_track(assignment_type)
        aligned_date = _align_dashboard_date(track, today_kst) if track != "Unassigned" else today_kst
        submission = {
            "memberId": member_page_id,
            "date": aligned_date,
            "status": "submitted",
            "tracks": [track] if track != "Unassigned" else [],
            "link": link if link else None,
            "content": content if content else None,
            "images": images if images else None
        }
        supabase_client.append_submission(submission)
        if aligned_date != today_kst:
            print(f"[SUPABASE] Real-time submission added for {username} ({track}: {today_kst}→{aligned_date})")
        else:
            print(f"[SUPABASE] Real-time submission added for {username}")
    except Exception as e:
        print(f"[WARN] Supabase real-time update failed (non-critical): {e}")

def _get_this_week_sunday_kst():
    """오늘이 포함된 KST 주의 일요일 날짜 (YYYY-MM-DD).
    월(0)~일(6) 기준: 오늘이 일요일이면 오늘, 아니면 이번 주 일요일."""
    kst_now = datetime.utcnow() + timedelta(hours=9)
    days_to_sunday = (6 - kst_now.weekday()) % 7
    target = kst_now + timedelta(days=days_to_sunday)
    return target.strftime('%Y-%m-%d')


def get_weekly_assignment_items(target_date=None, cohort=None):
    """지정한 날짜(기본: 이번 주 일요일) 마감인 '과제' 타입 항목 조회.
    cohort 미지정 시 CURRENT_COHORT env 를 사용 (기수 교차 오염 방지)."""
    ts = target_date if target_date else _get_this_week_sunday_kst()
    db_id = os.getenv('NOTION_ASSIGNMENTS_DB')
    url = f'https://api.notion.com/v1/databases/{db_id}/query'

    filters = [
        {"property": "마감일", "date": {"equals": ts}},
        {"property": "타입", "multi_select": {"contains": "과제"}},
    ]
    cohort_value = _normalize_cohort(cohort if cohort is not None else os.getenv('CURRENT_COHORT'))
    if cohort_value:
        filters.append({"property": "기수", "select": {"equals": cohort_value}})

    data = {"filter": {"and": filters}}
    response = SESSION.post(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    results = response.json().get('results', [])
    return results if results else None

def _normalize_cohort(cohort):
    """'8', '8기', 8 → '8기'. 파싱 불가 시 None."""
    if cohort is None:
        return None
    s = str(cohort).strip().replace('기', '').strip()
    if not s.isdigit():
        return None
    return f"{s}기"


def find_assignment_by_title(title, cohort=None):
    """과제명(+옵션 기수)으로 기존 과제 검색.
    같은 과제명이 기수별로 존재하므로 cohort 지정을 권장."""
    db_id = os.getenv('NOTION_ASSIGNMENTS_DB')
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    filters = [{"property": "과제명", "title": {"equals": title}}]
    cohort_value = _normalize_cohort(cohort)
    if cohort_value:
        filters.append({"property": "기수", "select": {"equals": cohort_value}})
    data = {"filter": {"and": filters}} if len(filters) > 1 else {"filter": filters[0]}
    response = SESSION.post(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    results = response.json().get('results', [])
    return results[0] if results else None


def update_assignment(page_id, title, tracks, assignment_type, due_date, cohort=None):
    """Update an existing assignment page."""
    url = f'https://api.notion.com/v1/pages/{page_id}'

    if due_date:
        try:
            dt = datetime.strptime(due_date, "%Y-%m-%d")
            due_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            print(f"[WARNING] Invalid date format: {due_date}. Sending as-is.")

    track_options = [{"name": t} for t in tracks]

    properties = {
        "과제명": {"title": [{"text": {"content": title}}]},
        "트랙": {"multi_select": track_options},
        "타입": {"multi_select": [{"name": assignment_type}]},
        "마감일": {"date": {"start": due_date}}
    }
    cohort_value = _normalize_cohort(cohort)
    if cohort_value:
        properties["기수"] = {"select": {"name": cohort_value}}

    data = {"properties": properties}
    response = SESSION.patch(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    if response.status_code != 200:
        print(f"[ERROR] Update Assignment Failed: {response.text}")
    return response.status_code == 200


def create_assignment(title, tracks, assignment_type, due_date, cohort=None, update_if_exists=False):
    """통합 과제 생성 (tracks는 리스트). 동일 (과제명+기수) 존재 시 기본적으로 스킵.

    title 은 기수 접두어 없이 '1주차 통합 과제', '숏폼 과제' 형태로 전달.
    cohort 는 '8', '8기', 8 모두 허용. 미전달 시 경고.
    """
    if cohort is None:
        print(f"[WARN] create_assignment called without cohort: {title!r}. "
              f"Duplicate detection will be title-only (collision risk).")

    existing = find_assignment_by_title(title, cohort=cohort)
    if existing:
        if update_if_exists:
            print(f"[UPDATE] Assignment already exists, syncing properties: {title} ({cohort})")
            return update_assignment(existing['id'], title, tracks, assignment_type, due_date, cohort=cohort)
        print(f"[SKIP] Assignment already exists: {title} ({cohort})")
        return True

    db_id = os.getenv('NOTION_ASSIGNMENTS_DB')
    url = 'https://api.notion.com/v1/pages'

    # [FIX] Sanitize date format to YYYY-MM-DD
    if due_date:
        try:
            dt = datetime.strptime(due_date, "%Y-%m-%d")
            due_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            print(f"[WARNING] Invalid date format: {due_date}. Sending as-is.")

    track_options = [{"name": t} for t in tracks]

    properties = {
        "과제명": {"title": [{"text": {"content": title}}]},
        "트랙": {"multi_select": track_options},
        "타입": {"multi_select": [{"name": assignment_type}]},
        "마감일": {"date": {"start": due_date}}
    }
    cohort_value = _normalize_cohort(cohort)
    if cohort_value:
        properties["기수"] = {"select": {"name": cohort_value}}

    data = {
        "parent": {"database_id": db_id},
        "properties": properties
    }
    response = SESSION.post(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    if response.status_code != 200:
        print(f"[ERROR] Create Assignment Failed: {response.text}")
    return response.status_code == 200


def create_short_form_assignment(cohort_number, due_date_str=None):
    """[Kickoff] 기수 숏폼 통합 과제 생성.

    - cohort_number: '6', '6기', 6 모두 허용
    - due_date_str: 'YYYY-MM-DD' (옵션). 없으면 시작일 + 28일 자동 계산
    """
    title = "숏폼 과제"
    tracks = ['크리에이터 숏폼 트랙']
    assignment_type = "과제"

    if due_date_str:
        due_date = due_date_str
    else:
        start_date_str = os.getenv('COHORT_START_DATE', datetime.now().strftime('%Y-%m-%d'))
        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            due_dt = start_dt + timedelta(days=28)
            due_date = due_dt.strftime('%Y-%m-%d')
        except ValueError:
            print(f"[ERROR] Invalid COHORT_START_DATE format: {start_date_str}")
            return False

    cohort_value = _normalize_cohort(cohort_number)
    print(f"[Kickoff] Creating Short Form Assignment: {title} ({cohort_value}) (Due: {due_date})")
    return create_assignment(title, tracks, assignment_type, due_date, cohort=cohort_number)


def get_submitted_users(assignment_id):
    """
    특정 과제에 연결된 모든 제출물 조회
    Returns: {user_id: set([assignment_type, ...]), ...}
    """
    db_id = SUBMISSIONS_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    payload = {
        "filter": {
            "property": "과제 페이지 DB", # [FIX] Renamed from '연결 과제'
            "relation": {
                "contains": assignment_id
            }
        }
    }
    # Use fetch_all_pages for safety (in case >100 submissions)
    submissions = fetch_all_pages(url, payload)
    
    submission_map = {}
    
    for submission in submissions:
        # Get User ID
        # Get User ID
        member_relation = submission['properties'].get('제출자', {}).get('relation', []) # [FIX] Removed trailing space
        if not member_relation:
            continue
        user_id = member_relation[0]['id']
        
        # Get Assignment Type (Multi-select)
        types = submission['properties'].get('과제 타입', {}).get('multi_select', [])
        type_names = [t['name'] for t in types]
        
        if user_id not in submission_map:
            submission_map[user_id] = set()
        
        submission_map[user_id].update(type_names)
            
    return submission_map

def get_submitted_users_on_date(assignment_id, date_str):
    """
    특정 과제에 연결되고 특정 날짜(YYYY-MM-DD)에 제출된 모든 사용자 ID 조회
    """
    db_id = SUBMISSIONS_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    payload = {
        "filter": {
            "and": [
                {
                    "property": "과제 페이지 DB",
                    "relation": {
                        "contains": assignment_id
                    }
                },
                {
                    "property": "제출 날짜",
                    "date": {
                        "equals": date_str
                    }
                }
            ]
        }
    }
    submissions = fetch_all_pages(url, payload)
    
    submitted_ids = set()
    for sub in submissions:
        member_relation = sub['properties'].get('제출자', {}).get('relation', [])
        if member_relation:
            submitted_ids.add(member_relation[0]['id'])
            
    return submitted_ids

def get_submission_counts(assignment_id, exclude_dates=None):
    """
    특정 과제에 대한 멤버별 제출 횟수 카운트
    exclude_dates: ["YYYY-MM-DD", ...] 형태의 휴무일 리스트. 해당 날짜 제출 건은 카운트 제외.
    Returns: {user_id: count, ...}
    """
    if exclude_dates is None:
        exclude_dates = []

    db_id = SUBMISSIONS_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    payload = {
        "filter": {
            "property": "과제 페이지 DB",
            "relation": {
                "contains": assignment_id
            }
        }
    }
    submissions = fetch_all_pages(url, payload)
    
    count_map = {}
    for sub in submissions:
        # Check if submitted during holiday
        manual_date_prop = sub['properties'].get('제출 날짜', {}).get('date')
        if manual_date_prop and manual_date_prop.get('start'):
            sub_date = manual_date_prop['start']
        else:
            try:
                # Fallback to created_time
                utc_str = sub['created_time'].replace('Z', '+00:00')
                utc_dt = datetime.fromisoformat(utc_str)
                kst_dt = utc_dt + timedelta(hours=9)
                sub_date = kst_dt.strftime('%Y-%m-%d')
            except Exception:
                sub_date = sub['created_time'].split('T')[0]

        if sub_date in exclude_dates:
            continue

        member_relation = sub['properties'].get('제출자', {}).get('relation', [])
        if member_relation:
            uid = member_relation[0]['id']
            count_map[uid] = count_map.get(uid, 0) + 1
            
    return count_map

def get_short_form_submitters_today():
    """
    [숏폼 트랙 전용] 제목에 '숏폼 과제'가 포함된 모든 과제에 대해
    '오늘' 제출한 사용자 ID 목록 조회.
    
    Returns:
        - Set of user_ids: If assignments found and checked.
        - None: If NO assignment containing '숏폼 과제' is found (to skip reminder).
    """
    # 1. '숏폼 과제' 키워드가 포함된 모든 과제 Page ID 찾기
    all_assignments = get_all_assignments()
    target_asm_ids = []
    
    if all_assignments:
        for asm in all_assignments:
             title_list = asm['properties']['과제명']['title']
             if title_list:
                title = title_list[0]['text']['content']
                # [UPDATE] Cohort-agnostic keyword search
                if "숏폼 과제" in title:
                    target_asm_ids.append(asm['id'])
    
    if not target_asm_ids:
        print("[Info] '숏폼 과제'가 포함된 과제를 찾을 수 없습니다. (리마인드 스킵)")
        return None

    # 2. 오늘 날짜 구하기 (KST 기준)
    utc_now = datetime.now()
    today_str = utc_now.strftime('%Y-%m-%d')
    
    submitted_user_ids = set()
    
    # 3. 각 과제 ID별로 Submissions DB 조회
    # (Note: Relation filter only accepts one ID or 'contains', normally we iterate)
    db_id = SUBMISSIONS_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    
    for asm_id in target_asm_ids:
        payload = {
            "filter": {
                "and": [
                    {
                        "property": "과제 페이지 DB",
                        "relation": {
                            "contains": asm_id
                        }
                    },
                    {
                        "property": "제출 날짜",
                        "date": {
                            "on_or_after": today_str
                        }
                    }
                ]
            }
        }
        
        submissions = fetch_all_pages(url, payload)
        
        for sub in submissions:
            member_relation = sub['properties'].get('제출자', {}).get('relation', [])
            if member_relation:
                submitted_user_ids.add(member_relation[0]['id'])
            
    return submitted_user_ids

def check_member_exists(discord_id):
    """사용자 ID로 멤버 존재 여부 확인"""
    db_id = TRACK_JO_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    data = {"filter": {"property": "사용자 ID", "rich_text": {"equals": str(discord_id)}}}
    response = SESSION.post(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    results = response.json().get('results', [])
    return len(results) > 0

def fetch_all_members_indexed():
    """Notion 멤버 마스터 DB 전체를 한 번에 조회하여
    {사용자ID: page, 디스코드ID: page, 닉네임: page, 이름: page} 인덱스로 반환.
    _sync_members_logic에서 멤버별 개별 조회 대신 사용."""
    db_id = os.getenv('TRACK_JO_DB_ID')
    url = f'https://api.notion.com/v1/databases/{db_id}/query'

    all_pages = []
    has_more = True
    start_cursor = None

    while has_more:
        payload = {"page_size": 100}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        try:
            resp = SESSION.post(url, headers=get_headers(), json=payload, timeout=TIMEOUT)
            if resp.status_code != 200:
                print(f"[ERROR] fetch_all_members_indexed: {resp.status_code}")
                break
            data = resp.json()
            all_pages.extend(data.get('results', []))
            has_more = data.get('has_more', False)
            start_cursor = data.get('next_cursor')
        except Exception as e:
            print(f"[ERROR] fetch_all_members_indexed: {e}")
            break

    # Build index: multiple keys → same page
    by_user_id = {}      # "123456789" → page
    by_discord_id = {}   # "ian.yokoso" → page
    by_nickname = {}     # "이안/부운영자" → page
    by_name = {}         # "이안" → page

    for page in all_pages:
        props = page.get('properties', {})

        uid_parts = props.get('사용자 ID', {}).get('rich_text', [])
        uid = uid_parts[0].get('plain_text', '').strip() if uid_parts else ''
        if uid:
            by_user_id[uid] = page

        did_parts = props.get('디스코드 ID', {}).get('rich_text', [])
        did = did_parts[0].get('plain_text', '').strip() if did_parts else ''
        if did:
            by_discord_id[did] = page

        nick_parts = props.get('디스코드 닉네임', {}).get('title', [])
        nick = nick_parts[0].get('plain_text', '').strip() if nick_parts else ''
        if nick:
            by_nickname[nick] = page

        name_parts = props.get('이름', {}).get('rich_text', [])
        name = name_parts[0].get('plain_text', '').strip() if name_parts else ''
        if name:
            by_name[name] = page

    print(f"[INDEX] Built member index: {len(all_pages)} pages, {len(by_user_id)} user IDs, {len(by_discord_id)} discord IDs")
    return {
        'by_user_id': by_user_id,
        'by_discord_id': by_discord_id,
        'by_nickname': by_nickname,
        'by_name': by_name,
        'total': len(all_pages),
    }


def find_member_by_discord_id(discord_id):
    """사용자 ID(숫자) 또는 디스코드 ID(핸들)로 멤버 찾기 (Page 객체 반환)"""
    db_id = os.getenv('TRACK_JO_DB_ID')
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    discord_id_str = str(discord_id).strip()

    # 사용자 ID(숫자)와 디스코드 ID(핸들) 모두 검색
    data = {
        "filter": {
            "or": [
                {"property": "사용자 ID", "rich_text": {"equals": discord_id_str}},
                {"property": "디스코드 ID", "rich_text": {"equals": discord_id_str}},
            ]
        }
    }
    try:
        response = SESSION.post(url, headers=get_headers(), json=data, timeout=TIMEOUT)
        if response.status_code != 200:
            print(f"[ERROR] find_member_by_discord_id: {response.status_code} - {response.text}")
            return None
        results = response.json().get('results', [])
        if results:
            return results[0]
    except Exception as e:
        print(f"[ERROR] Exception in find_member_by_discord_id: {e}")
    return None

def find_member_by_name(name_str):
    """이름 또는 디스코드 닉네임 유저 검색"""
    if not name_str:
        return None
        
    db_id = os.getenv('TRACK_JO_DB_ID')
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    
    # Try finding by '디스코드 닉네임' OR '이름'
    # Notion API 'or' filter
    payload = {
        "filter": {
            "or": [
                {
                    "property": "디스코드 닉네임",
                    "title": {
                        "equals": name_str
                    }
                },
                {
                    "property": "이름",
                    "rich_text": {
                        "equals": name_str
                    }
                },
                {
                    "property": "디스코드 ID",
                    "rich_text": {
                        "equals": name_str
                    }
                }
            ]
        }
    }
    
    response = SESSION.post(url, headers=get_headers(), json=payload, timeout=TIMEOUT)
    results = response.json().get('results', [])
    if results:
        return results[0] # Return first match
    return None

def create_member(username, discord_id, handle=None, avatar_url=None, joined_at=None):
    """새로운 멤버 추가 (중복 방지: discord_id로 기존 멤버 검색 후 있으면 업데이트)"""
    db_id = os.getenv('TRACK_JO_DB_ID')

    if not db_id:
        print("[ERROR] TRACK_JO_DB_ID is missing!")
        return False

    # [FIX] 중복 방지: discord_id로 기존 멤버 검색
    if discord_id:
        existing = find_member_by_discord_id(discord_id)
        if existing:
            print(f"[DEDUP] Member already exists: {username} (ID: {discord_id}). Updating instead of creating.")
            props = {
                "디스코드 닉네임": {"title": [{"text": {"content": username}}]},
            }
            if handle:
                props["디스코드 ID"] = {"rich_text": [{"text": {"content": handle}}]}
            if avatar_url:
                props["프로필 이미지"] = {"url": avatar_url}
            if joined_at:
                props["가입일"] = {"date": {"start": joined_at}}
            return update_page_properties(existing['id'], props)

    print(f"[DEBUG] create_member: Creating {username} (ID: {discord_id}) in DB: {db_id}")

    url = 'https://api.notion.com/v1/pages'

    props = {
        "디스코드 닉네임": {"title": [{"text": {"content": username}}]},
        "사용자 ID": {"rich_text": [{"text": {"content": str(discord_id)}}]},
        "디스코드 ID": {"rich_text": [{"text": {"content": str(discord_id)}}]}
    }

    if handle:
        props["디스코드 ID"] = {"rich_text": [{"text": {"content": handle}}]}

    if avatar_url:
        props["프로필 이미지"] = {"url": avatar_url}

    if joined_at:
        props["가입일"] = {"date": {"start": joined_at}}

    data = {
        "parent": {"database_id": db_id},
        "properties": props
    }

    try:
        response = SESSION.post(url, headers=get_headers(), json=data, timeout=TIMEOUT)
        if response.status_code != 200:
            print(f"[ERROR] Member Creation Failed: {response.status_code} - {response.text}")
            return False
        print(f"[DEBUG] Successfully created member: {username}")
        return True
    except Exception as e:
        print(f"[ERROR] Exception in create_member: {e}")
        return False

def update_page_properties(page_id, properties, archived=None):
    """페이지 속성 업데이트 (archived 옵션 지원)"""
    url = f'https://api.notion.com/v1/pages/{page_id}'
    data = {"properties": properties}
    
    if archived is not None:
        data["archived"] = archived
    
    response = SESSION.patch(url, headers=get_headers(), json=data, timeout=TIMEOUT)
    if response.status_code != 200:
        print(f"[ERROR] Page Update Failed: {response.text}")
    return response.status_code == 200

def get_member_submissions(member_page_id):
    """특정 멤버의 전체 제출 내역 조회"""
    db_id = SUBMISSIONS_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    payload = {
        "filter": {
            "property": "제출자",
            "relation": {"contains": member_page_id}
        },
        "sorts": [{"property": "제출 날짜", "direction": "descending"}],
        "page_size": 100
    }
    results = fetch_all_pages(url, payload)

    submissions = []
    for sub in results:
        props = sub.get('properties', {})

        # 제출물 제목
        title_list = props.get('제출물 제목', {}).get('title', [])
        title = title_list[0]['plain_text'] if title_list else ''

        # 과제 타입
        types = props.get('과제 타입', {}).get('multi_select', [])
        type_names = [t['name'] for t in types]

        # 링크
        link = props.get('링크', {}).get('url', '')

        # 제출 날짜
        date_prop = props.get('제출 날짜', {}).get('date', {})
        date = date_prop.get('start', '') if date_prop else ''

        # 과제 내용
        content_rt = props.get('과제 내용 ', {}).get('rich_text', [])
        content = ''.join([t.get('plain_text', '') for t in content_rt]).strip()

        # 이미지
        images = []
        img_files = props.get('이미지', {}).get('files', [])
        for f in img_files:
            if f.get('type') == 'external':
                images.append(f.get('external', {}).get('url', ''))
            elif f.get('type') == 'file':
                images.append(f.get('file', {}).get('url', ''))

        submissions.append({
            "title": title,
            "types": type_names,
            "link": link or '',
            "date": date,
            "content": content,
            "images": images
        })

    return submissions

def get_member_detail(user_id):
    """사용자 ID로 멤버 상세 정보 조회 (개인정보 + 조 + 제출 내역)"""
    import re

    # 1. 멤버 기본 정보
    member_page = find_member_by_discord_id(user_id)
    if not member_page:
        return None

    props = member_page.get('properties', {})
    page_id = member_page['id']

    # 이름
    name_rt = props.get('이름', {}).get('rich_text', [])
    name = name_rt[0]['plain_text'] if name_rt else ''

    # 디스코드 닉네임
    nick_title = props.get('디스코드 닉네임', {}).get('title', [])
    nickname = nick_title[0]['plain_text'] if nick_title else ''

    # 사용자 ID
    uid_rt = props.get('사용자 ID', {}).get('rich_text', [])
    uid = uid_rt[0]['plain_text'] if uid_rt else ''

    # 디스코드 ID
    did_rt = props.get('디스코드 ID', {}).get('rich_text', [])
    discord_id = did_rt[0]['plain_text'] if did_rt else ''

    # 프로필 이미지
    avatar = props.get('프로필 이미지', {}).get('url', '')

    # 기수
    cohort_val = props.get('기수', {}).get('select', {})
    cohort = cohort_val.get('name', '') if cohort_val else ''

    # 트랙
    tracks = [t['name'] for t in props.get('트랙', {}).get('multi_select', [])]

    # 활동 상태
    status_val = props.get('활동 상태', {}).get('status', {})
    status = status_val.get('name', '') if status_val else ''

    # 기타사항
    notes_rt = props.get('기타사항', {}).get('rich_text', [])
    notes = ''.join([t.get('plain_text', '') for t in notes_rt]).strip()

    # 탈락 주차
    drop_week = ''
    if status == '탈락':
        week_match = re.search(r'(\d+)주차', notes)
        if week_match:
            drop_week = f"{week_match.group(1)}주차"

    # 2. 조 정보
    group_info = []
    group_relations = props.get('조', {}).get('relation', [])
    for rel in group_relations:
        try:
            group_page_url = f"https://api.notion.com/v1/pages/{rel['id']}"
            resp = SESSION.get(group_page_url, headers=get_headers(), timeout=TIMEOUT)
            if resp.status_code == 200:
                gprops = resp.json().get('properties', {})
                gtitle = gprops.get('조', {}).get('title', [])
                gname = gtitle[0]['plain_text'] if gtitle else ''
                if gname:
                    group_info.append(gname)
        except:
            pass

    # 3. 제출 내역
    submissions = get_member_submissions(page_id)

    return {
        "pageId": page_id,
        "name": name,
        "nickname": nickname,
        "userId": uid,
        "discordId": discord_id,
        "avatar": avatar,
        "cohort": cohort,
        "tracks": tracks,
        "status": status,
        "notes": notes,
        "dropWeek": drop_week,
        "groups": group_info,
        "submissions": submissions
    }

def get_all_submissions(filter_payload=None):
    """모든 제출물 조회 (최신순, Pagination Applied)"""
    db_id = SUBMISSIONS_DB_ID
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    payload = {
        "page_size": 100,
        "sorts": [{"timestamp": "created_time", "direction": "descending"}]
    }
    if filter_payload:
        payload["filter"] = filter_payload
        
    return fetch_all_pages(url, payload)

def get_all_assignments():
    """모든 과제 조회 (최신순, Pagination Applied)"""
    db_id = os.getenv('NOTION_ASSIGNMENTS_DB')
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    payload = {
         "page_size": 100,
         "sorts": [{"property": "마감일", "direction": "descending"}]
    }
    return fetch_all_pages(url, payload)
    
def get_all_members():
    """모든 멤버 조회 (Pagination Applied)"""
    db_id = os.getenv('TRACK_JO_DB_ID')
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    return fetch_all_pages(url, {"page_size": 100})

def find_assignment(assignment_type):
    """
    트랙별 과제 매칭 로직
    1. 숏폼 트랙: 현재 기수의 '숏폼 과제' 고정 매칭 (마감일 무시)
    2. 일반 트랙: 현재 기수 과제 중 오늘 날짜 <= 마감일 인 가장 가까운 과제 매칭

    기수 구분은 '과제 페이지 DB'의 '기수' Select 속성으로 수행.
    과제명에는 기수 접두어가 더 이상 포함되지 않음.
    """
    today_str = datetime.now().strftime('%Y-%m-%d')
    current_cohort_raw = os.getenv('CURRENT_COHORT', '6')
    expected_cohort = _normalize_cohort(current_cohort_raw)

    def _asm_cohort(asm):
        sel = asm['properties'].get('기수', {}).get('select') or {}
        return sel.get('name')

    # [1] 숏폼 트랙 전용 로직 (상시 제출)
    # bot.py에서 넘겨주는 assignment_type이 "크리에이터 숏폼 과제"여야 함
    if assignment_type == "크리에이터 숏폼 과제":
        all_assignments = get_all_assignments()
        if not all_assignments:
            return None, "❌ 등록된 과제가 하나도 없습니다."

        shortform_candidates = []
        for asm in all_assignments:
            if expected_cohort and _asm_cohort(asm) != expected_cohort:
                continue
            title_list = asm['properties']['과제명']['title']
            if title_list:
                title = title_list[0]['text']['content']
                if "숏폼 과제" in title:
                    shortform_candidates.append(asm)

        if not shortform_candidates:
             return None, f"❌ {expected_cohort or current_cohort_raw} 숏폼 과제를 찾을 수 없습니다."

        # 마감일(deadline) 기준 내림차순 정렬 (가장 미래의 것 선택)
        target_assignment = sorted(
            shortform_candidates,
            key=lambda x: x['properties'].get('마감일', {}).get('date', {}).get('start') or '0000-00-00',
            reverse=True
        )[0]

        t_title = target_assignment['properties']['과제명']['title'][0]['text']['content']
        return target_assignment, f"✅ [숏폼 트랙] 최신 과제 매칭: {expected_cohort} {t_title}"

    # [2] 일반 트랙 (세일즈, 빌더 등) 로직
    all_assignments = get_all_assignments()
    if not all_assignments:
        return None, "❌ 등록된 과제가 하나도 없습니다."

    valid_assignments = []

    for asm in all_assignments:
        title_list = asm['properties']['과제명']['title']
        if not title_list:
             continue
        t_val = title_list[0]['text']['content']

        # [FIX] 기수 속성으로 현재 기수 필터링 (과거 과제명 접두어 방식 폐기)
        if expected_cohort and _asm_cohort(asm) != expected_cohort:
            continue

        # [FIX] Always Exclude "Short Form" from General Matching
        if "숏폼" in t_val:
            continue

        due_date = asm['properties'].get('마감일', {}).get('date', {}).get('start')
        if not due_date:
            continue

        # [FIX] Filter by track: match assignment_type to 과제's 트랙 multi_select
        submitted_track = map_track(assignment_type)
        if submitted_track != "Unassigned":
            asm_tracks = asm['properties'].get('트랙', {}).get('multi_select', [])
            asm_track_ids = {map_track(t['name']) for t in asm_tracks}
            if submitted_track not in asm_track_ids:
                continue

        # Valid Assignment for General Track
        valid_assignments.append(asm)

    if not valid_assignments:
         return None, "❌ 제출 가능한 일반 과제가 없습니다."

    # Sort by due_date
    # 1. Look for upcoming due dates (today <= due_date) - Sorted Ascending
    upcoming = [a for a in valid_assignments if today_str <= a['properties']['마감일']['date']['start']]
    if upcoming:
        upcoming.sort(key=lambda x: x['properties']['마감일']['date']['start'])
        target = upcoming[0]
        title = target['properties']['과제명']['title'][0]['text']['content']
        due = target['properties']['마감일']['date']['start']
        return target, f"✅ [매칭 성공] {expected_cohort} {title} (마감: {due})"

    # 2. If no upcoming, allow Late Submission for the most recent past assignment - Sorted Descending
    # (Allow submission even if deadline passed)
    valid_assignments.sort(key=lambda x: x['properties']['마감일']['date']['start'], reverse=True)
    target = valid_assignments[0]
    title = target['properties']['과제명']['title'][0]['text']['content']
    due = target['properties']['마감일']['date']['start']
    return target, f"⚠️ [지각 제출] {expected_cohort} {title} (마감일 지남: {due})"

def get_unprocessed_applications(cohort, include_processed=False):
    """신청서 가져오기 (include_processed가 True면 봇 처리 완료된 것도 포함)"""
    db_id = os.getenv('TRACK_APPLICATION_DB_ID')
    if not db_id:
        print("[Error] TRACK_APPLICATION_DB_ID not configured.")
        return []

    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    
    filter_list = [
        {
            "property": "기수",
            "rich_text": {"equals": str(cohort)}
        }
    ]
    
    if not include_processed:
        filter_list.append({
            "property": "봇 처리 완료",
            "checkbox": {"equals": False}
        })

    payload = {"filter": {"and": filter_list}}
    return fetch_all_pages(url, payload)

def find_member_in_db(db_id, discord_id):
    """특정 데이터베이스(조 DB 등)에 해당 디스코드 ID를 가진 멤버가 있는지 확인"""
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    payload = {
        "filter": {
            "property": "디스코드 ID",
            "rich_text": {
                "equals": str(discord_id)
            }
        }
    }
    try:
        pages = fetch_all_pages(url, payload)
        return pages[0] if pages else None
    except Exception as e:
        print(f"[Exception] find_member_in_db: {e}")
        return None

def fetch_block_children(block_id):
    """블록의 자식 요소 조회 (Inline DB ID 찾기용) - Pagination Support"""
    url = f"https://api.notion.com/v1/blocks/{block_id}/children"
    headers = get_headers()
    
    all_results = []
    has_more = True
    next_cursor = None
    
    while has_more:
        params = {}
        if next_cursor:
            params['start_cursor'] = next_cursor
            
        try:
            response = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
        
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                all_results.extend(results)
                
                has_more = data.get('has_more', False)
                next_cursor = data.get('next_cursor')
            else:
                print(f"[Error] fetch_block_children failed: {response.text}")
                break
        except Exception as e:
            print(f"[Exception] fetch_block_children: {e}")
            break
            
    return all_results

def get_group_status(cohort, track_name=None):
    """
    [Phase 3 - Nested DB]
    1. Master DB(GROUP_DB_ID)에서 track_name에 해당하는 페이지 찾기
    2. 해당 페이지 내부의 Child Database ID 찾기
    3. Child DB에서 해당 기수(cohort)가 포함된 조 검색
    """
    master_db_id = os.getenv('GROUP_DB_ID')
    if not master_db_id:
        print("[Error] GROUP_DB_ID not configured.")
        return []

    if not track_name:
        print("[Error] Track name required for nested lookup.")
        return []

    # 1. Find Track Page in Master DB
    payload = {
        "filter": {
            "property": "트랙명", # Master DB의 Multi-Select 속성
            "multi_select": {
                "contains": track_name
            }
        }
    }
    track_pages = fetch_all_pages(f'https://api.notion.com/v1/databases/{master_db_id}/query', payload)
    
    if not track_pages:
        # Fallback: Try searching by Title if Track Name prop fails
        payload = {"filter": {"property": "이름", "title": {"contains": track_name}}}
        track_pages = fetch_all_pages(f'https://api.notion.com/v1/databases/{master_db_id}/query', payload)
        
    if not track_pages:
        print(f"[Warning] Master DB에서 '{track_name}' 페이지를 찾을 수 없습니다.")
        return []
        
    track_page_id = track_pages[0]['id']
    
    # 2. Find Child Database inside Track Page
    children = fetch_block_children(track_page_id)
    child_db_id = None
    for block in children:
        if block['type'] == 'child_database':
            child_db_id = block['id']
            break
            
    if not child_db_id:
        print(f"[Warning] '{track_name}' 페이지 안에 조 DB(Inline)가 없습니다.")
        return []
        
    # 3. Query the Child Group DB
    # Filter by Cohort in Title (e.g. "6기 1조")
    payload = {
        "filter": {
            "property": "이름", 
            "title": {
                "contains": str(cohort)
            }
        }
    }
    
    pages = fetch_all_pages(f'https://api.notion.com/v1/databases/{child_db_id}/query', payload)
    
    results = []
    for p in pages:
        p_id = p['id']
        title_prop = p['properties'].get('이름', {}).get('title', [])
        name = title_prop[0]['text']['content'] if title_prop else "Unknown"
        
        members_rel = []
        for key in ['조원', 'Members', 'Team Members', '멤버']:
            if key in p['properties']:
                members_rel = p['properties'][key].get('relation', [])
                break
                
        results.append({
            'id': p_id,
            'name': name,
            'count': len(members_rel)
        })
        
    return results

def assign_member_to_group(member_page_id, group_name, group_url=None):
    """멤버의 '소속 조' 정보를 텍스트로 업데이트"""
    content = group_name
    link = None
    if group_url:
        link = {"url": group_url}
        
    props = {
        "소속 조": {
            "rich_text": [
                {
                    "text": {"content": content, "link": link}
                }
            ]
        }
    }
    return update_page_properties(member_page_id, props)

def mark_application_processed(app_page_id):
    """신청서 '봇 처리 완료' 체크"""
    props = {
        "봇 처리 완료": {"checkbox": True}
    }
    return update_page_properties(app_page_id, props)


def delete_database(db_id):
    """데이터베이스 삭제 (Archive)"""
    url = f"https://api.notion.com/v1/databases/{db_id}"
    headers = get_headers()
    payload = {"archived": True}
    
    try:
        resp = requests.patch(url, headers=headers, json=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            print(f"[Success] Archived Database: {db_id}")
            return True
        else:
            print(f"[Error] Failed to archive Database {db_id}: {resp.text}")
            return False
    except Exception as e:
        print(f"[Exception] delete_database: {e}")
        return False

def create_inline_database(parent_page_id, title, related_db_id=None):
    """트랙 페이지 내부에 새로운 인라인 데이터베이스(조) 생성"""
    url = "https://api.notion.com/v1/databases"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    props = {
        "ID": {"title": {}}, # Renamed from '이름' to allow '이름' to be a Relation
        "디스코드 ID": {"rich_text": {}},
        "트랙": {"select": {}},
        "기수": {"select": {}},
        "직책": {"select": {}}
    }
    
    # [FEATURE] Add Relation Property
    if related_db_id:
        props["이름"] = { # Renamed from '팀원' to '이름' per user request
            "relation": {
                "database_id": related_db_id,
                "single_property": {}
            }
        }

    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "is_inline": True,
        "properties": props
    }
    
    try:
        resp = SESSION.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            db_id = resp.json().get('id')
            print(f"[Success] Created inline DB: {title} (ID: {db_id})")
            return db_id
        else:
            print(f"[Error] Failed to create inline DB: {resp.text}")
            return None
    except Exception as e:
        print(f"[Exception] create_inline_database: {e}")
        return None

def update_database_schema(db_id, properties):
    """
    Update database schema (add/modify properties).
    properties: dict (e.g., {"직책": {"select": {}}})
    """
    url = f"https://api.notion.com/v1/databases/{db_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    payload = {
        "properties": properties
    }
    try:
        resp = SESSION.patch(url, headers=headers, json=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            return True
        else:
            print(f"[Warn] Failed to update schema for {db_id}: {resp.text}")
            return False
    except Exception as e:
        print(f"[Error] update_database_schema: {e}")
        return False

def add_multi_select_option(db_id, property_name, option_name):
    """
    multi_select 속성에 새로운 옵션을 추가합니다.
    이미 존재하면 스킵합니다.
    """
    url = f"https://api.notion.com/v1/databases/{db_id}"
    try:
        resp = SESSION.get(url, headers=get_headers(), timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"[Error] Failed to read DB schema: {resp.text}")
            return False

        db = resp.json()
        prop = db.get('properties', {}).get(property_name)
        if not prop or prop.get('type') != 'multi_select':
            print(f"[Error] Property '{property_name}' is not multi_select")
            return False

        existing_options = prop.get('multi_select', {}).get('options', [])
        if any(opt['name'] == option_name for opt in existing_options):
            print(f"[Info] Option '{option_name}' already exists in '{property_name}'")
            return True

        existing_options.append({"name": option_name})
        payload = {
            "properties": {
                property_name: {
                    "multi_select": {"options": existing_options}
                }
            }
        }
        resp = SESSION.patch(url, headers=get_headers(), json=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            print(f"[Success] Added option '{option_name}' to '{property_name}'")
            return True
        else:
            print(f"[Error] Failed to add option: {resp.text}")
            return False
    except Exception as e:
        print(f"[Exception] add_multi_select_option: {e}")
        return False

def create_track_page_in_group_db(group_db_id, track_name):
    """
    트랙/조 DB에 새 트랙 페이지를 생성합니다.
    이미 존재하면 스킵합니다.
    """
    try:
        pages = fetch_all_pages(f'https://api.notion.com/v1/databases/{group_db_id}/query', {})
        for page in pages:
            props = page.get('properties', {})
            for val in props.values():
                if val.get('type') == 'title':
                    title_list = val.get('title', [])
                    if title_list and title_list[0]['text']['content'] == track_name:
                        print(f"[Info] Track page '{track_name}' already exists in group DB")
                        return page['id']

        title_prop_name = None
        url = f"https://api.notion.com/v1/databases/{group_db_id}"
        resp = SESSION.get(url, headers=get_headers(), timeout=TIMEOUT)
        if resp.status_code == 200:
            for pname, pval in resp.json().get('properties', {}).items():
                if pval.get('type') == 'title':
                    title_prop_name = pname
                    break

        if not title_prop_name:
            title_prop_name = '트랙명'

        props = {
            title_prop_name: {"title": [{"text": {"content": track_name}}]}
        }
        page_id = add_row_to_database(group_db_id, props)
        if page_id:
            print(f"[Success] Created track page '{track_name}' in group DB")
        return page_id
    except Exception as e:
        print(f"[Exception] create_track_page_in_group_db: {e}")
        return None

def get_inline_databases(page_id):
    """페이지 내부의 모든 인라인 데이터베이스 목록 가져오기"""
    blocks = fetch_block_children(page_id)
    databases = []
    for block in blocks:
        if block['type'] == 'child_database':
            databases.append({
                'id': block['id'],
                'title': block['child_database']['title']
            })
    return databases

def add_row_to_database(db_id, properties):
    """데이터베이스에 새로운 행(페이지) 추가"""
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    payload = {
        "parent": {"type": "database_id", "database_id": db_id},
        "properties": properties
    }
    
    try:
        resp = SESSION.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            page_id = resp.json().get('id')
            print(f"[Success] Added row to DB {db_id}: {page_id}")
            return page_id
        else:
            print(f"[Error] Failed to add row to {db_id}: {resp.text}")
            return None
    except Exception as e:
        print(f"[Exception] add_row_to_database: {e}")
        return None


def get_database(db_id):
    """Retrieve database object (including schema)"""
    url = f"https://api.notion.com/v1/databases/{db_id}"
    try:
        resp = SESSION.get(url, headers=get_headers(), timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[Error] get_database: {e}")
    return None

def drop_member_from_track(member_page_id, track_to_remove):
    """
    특정 멤버의 마스터 DB에서 지정된 트랙만 제거합니다.
    멀티트랙 멤버의 경우 해당 트랙만 빠지고 나머지는 유지됩니다.

    Args:
        member_page_id: 멤버의 Notion page ID
        track_to_remove: 제거할 트랙 이름 (예: "AI 에이전트 트랙", "빌더 기초 트랙")

    Returns:
        dict: {"success": bool, "remaining_tracks": list, "message": str}
    """
    # 1. 현재 멤버 정보 조회
    url = f"https://api.notion.com/v1/pages/{member_page_id}"
    try:
        resp = SESSION.get(url, headers=get_headers(), timeout=TIMEOUT)
        if resp.status_code != 200:
            return {"success": False, "remaining_tracks": [], "message": f"멤버 조회 실패: {resp.status_code}"}

        page = resp.json()
        current_tracks = page['properties'].get('트랙', {}).get('multi_select', [])
        current_track_names = [t['name'] for t in current_tracks]

        print(f"[DROP] Current tracks for {member_page_id}: {current_track_names}")

        # 2. 해당 트랙이 실제로 있는지 확인
        if track_to_remove not in current_track_names:
            return {"success": False, "remaining_tracks": current_track_names,
                    "message": f"'{track_to_remove}' 트랙이 이미 없습니다."}

        # 3. 해당 트랙만 제거한 새 리스트 생성
        new_tracks = [{"name": t} for t in current_track_names if t != track_to_remove]

        # 4. 기타사항에 탈락 기록 추가 (기수 + 트랙 + 주차)
        from utils.helpers import calculate_week_number
        cohort = os.getenv('CURRENT_COHORT', '6')
        start_date = os.getenv('COHORT_START_DATE', '')
        MAX_WEEK = 4
        week = calculate_week_number(start_date) if start_date else 0
        if week > MAX_WEEK:
            week = MAX_WEEK
        week_str = f"({week}주차)" if week > 0 else ""
        drop_note = f"🚫 {cohort}기 {track_to_remove} 탈락{week_str}"

        existing_notes = page['properties'].get('기타사항', {}).get('rich_text', [])
        existing_text = ''.join([t.get('plain_text', '') for t in existing_notes]).strip()
        if existing_text:
            new_note_text = f"{existing_text}\n{drop_note}"
        else:
            new_note_text = drop_note

        # 5. 마스터 DB 업데이트 (트랙 제거 + 기타사항 기록 + 활동 상태 변경)
        update_data = {
            "properties": {
                "트랙": {"multi_select": new_tracks},
                "기타사항": {"rich_text": [{"text": {"content": new_note_text}}]},
                "활동 상태": {"status": {"name": "탈락"}}
            }
        }

        update_resp = SESSION.patch(url, headers=get_headers(), json=update_data, timeout=TIMEOUT)
        if update_resp.status_code != 200:
            return {"success": False, "remaining_tracks": current_track_names,
                    "message": f"트랙 업데이트 실패: {update_resp.text}"}

        remaining = [t['name'] for t in new_tracks]
        print(f"[DROP] Removed '{track_to_remove}' from {member_page_id}. Remaining: {remaining}")
        print(f"[DROP] Added note: {drop_note}")

        return {"success": True, "remaining_tracks": remaining,
                "message": f"'{track_to_remove}' 트랙에서 탈락 처리 완료."}

    except Exception as e:
        print(f"[Exception] drop_member_from_track: {e}")
        return {"success": False, "remaining_tracks": [], "message": str(e)}

def archive_cohort_to_db(cohort):
    """
    기수 마감 3단계 파이프라인:

    Step 1 — 트랙/조 DB의 실제 참여자를 아카이브 DB(ARCHIVE_DB_ID)에 복사
      - GROUP_DB_ID 의 각 트랙 페이지 하위 '{cohort}기' 프리픽스 인라인 DB 순회
      - 디스코드 ID로 중복 제거 (멀티트랙은 한 행으로 병합)
      - 완주/탈락 판정은 멤버 마스터 '기타사항'에서 '{cohort}기 ... 탈락(N주차)' 패턴 우선 파싱
        (활동 상태는 사이클 간 리셋될 수 있어 신뢰 불가)

    Step 2 — GROUP_DB_ID 각 트랙 페이지 하위의 '모든' 인라인 DB archive (휴지통)
      - 해당 기수 뿐 아니라 이전 기수·부스러기 DB 포함 전멸 (노션 기준 클린업)

    Step 3 — 멤버 마스터 DB 전원 초기화
      - 트랙(multi_select) 비우기, 기타사항(rich_text) 비우기
      - 활동 상태(status) = '휴식'
      - 탈락자도 휴식으로 전환 (이력은 이미 아카이브 DB에 보존됨)

    컬럼: 기수, 이름, 디스코드 닉네임, 사용자 ID, 완주/탈락, 탈락 주차, 기타사항
    """
    import re

    archive_db_id = os.getenv('ARCHIVE_DB_ID')
    master_db_id = os.getenv('GROUP_DB_ID')
    member_db_id = os.getenv('TRACK_JO_ID') or os.getenv('TRACK_JO_DB_ID')

    if not archive_db_id:
        return {"success": False, "message": "ARCHIVE_DB_ID 환경변수가 설정되지 않았습니다."}
    if not master_db_id:
        return {"success": False, "message": "GROUP_DB_ID 환경변수가 설정되지 않았습니다."}
    if not member_db_id:
        return {"success": False, "message": "TRACK_JO_DB_ID 환경변수가 설정되지 않았습니다."}

    cohort_clean = ''.join(filter(str.isdigit, str(cohort)))
    if not cohort_clean:
        return {"success": False, "message": f"올바르지 않은 기수 값: {cohort}"}

    print(f"[기수마감-{cohort_clean}기] Step 1 시작: 조 DB 기준 참여자 수집")

    # ---------- Step 1: Collect participants from group DBs ----------
    track_pages = fetch_all_pages(f'https://api.notion.com/v1/databases/{master_db_id}/query', {})
    participants = {}  # discord_id (or name fallback) -> info

    for tp in track_pages:
        dbs = get_inline_databases(tp['id'])
        cohort_dbs = [
            d for d in dbs
            if d['title'].startswith(f"{cohort_clean}기") or d['title'].startswith(f"{cohort_clean} ")
        ]
        for cdb in cohort_dbs:
            rows = fetch_all_pages(f"https://api.notion.com/v1/databases/{cdb['id']}/query", {})
            for row in rows:
                p = row['properties']
                id_title = p.get('ID', {}).get('title', []) or p.get('이름', {}).get('title', [])
                row_name = id_title[0]['plain_text'] if id_title else ''
                did_rt = p.get('디스코드 ID', {}).get('rich_text', [])
                discord_id = did_rt[0]['plain_text'] if did_rt else ''
                track_sel = p.get('트랙', {}).get('select') or {}
                role_sel = p.get('직책', {}).get('select') or {}
                rel = p.get('이름', {}).get('relation', [])
                key = discord_id or f"name:{row_name}"
                if key not in participants:
                    participants[key] = {
                        'name': row_name, 'discord_id_row': discord_id,
                        'tracks': set(), 'groups': set(), 'role': '',
                        'member_rel_id': rel[0]['id'] if rel else '',
                    }
                if track_sel.get('name'):
                    participants[key]['tracks'].add(track_sel['name'])
                participants[key]['groups'].add(cdb['title'])
                if role_sel.get('name') == '조장':
                    participants[key]['role'] = '조장'
                if rel and not participants[key]['member_rel_id']:
                    participants[key]['member_rel_id'] = rel[0]['id']

    print(f"[기수마감-{cohort_clean}기] 유니크 참여자: {len(participants)}명")

    # Write archive rows
    created, failed = 0, 0
    completed_count, dropped_count = 0, 0
    drop_re = re.compile(rf"{cohort_clean}기[^\n]*?탈락[^\n]*?\((\d+)주차\)")

    for _, info in participants.items():
        # Resolve member master
        m = None
        if info['member_rel_id']:
            try:
                r = SESSION.get(
                    f"https://api.notion.com/v1/pages/{info['member_rel_id']}",
                    headers=get_headers(), timeout=TIMEOUT,
                )
                if r.status_code == 200:
                    m = r.json()
            except Exception:
                m = None
        if not m and info['discord_id_row']:
            m = find_member_by_discord_id(info['discord_id_row'])

        nick = info['name']
        real_name = ''
        user_id = info['discord_id_row']
        notes = ''
        if m:
            mp = m.get('properties', {})
            nt = mp.get('디스코드 닉네임', {}).get('title', [])
            if nt: nick = nt[0]['plain_text']
            rn = mp.get('이름', {}).get('rich_text', [])
            if rn: real_name = rn[0]['plain_text']
            ur = mp.get('사용자 ID', {}).get('rich_text', [])
            if ur: user_id = ur[0]['plain_text']
            nr = mp.get('기타사항', {}).get('rich_text', [])
            notes = ''.join([t.get('plain_text', '') for t in nr]).strip()

        # 탈락 판정 — 기타사항에서 "{cohort}기 ... 탈락(N주차)" 우선
        m_drop = drop_re.search(notes)
        if m_drop:
            result = '탈락'
            drop_week = f"{m_drop.group(1)}주차"
        else:
            result = '완주'
            drop_week = ''

        if result == '완주':
            completed_count += 1
        else:
            dropped_count += 1

        tracks_str = ', '.join(sorted(info['tracks']))
        groups_str = ', '.join(sorted(info['groups']))
        role = info['role'] or '조원'
        meta_suffix = f"[트랙: {tracks_str} | 조: {groups_str} | 직책: {role}]"
        combined_notes = (f"{notes} {meta_suffix}" if notes else meta_suffix)[:2000]

        page_data = {
            "parent": {"database_id": archive_db_id},
            "properties": {
                "이름": {"title": [{"text": {"content": real_name or nick}}]},
                "기수": {"select": {"name": f"{cohort_clean}기"}},
                "디스코드 닉네임": {"rich_text": [{"text": {"content": nick}}]},
                "사용자 ID": {"rich_text": [{"text": {"content": user_id}}]},
                "완주/탈락": {"select": {"name": result}},
                "탈락 주차": {"rich_text": [{"text": {"content": drop_week}}]},
                "기타사항": {"rich_text": [{"text": {"content": combined_notes}}]}
            }
        }
        try:
            resp = SESSION.post('https://api.notion.com/v1/pages', headers=get_headers(), json=page_data, timeout=TIMEOUT)
            if resp.status_code == 200:
                created += 1
            else:
                failed += 1
                print(f"[WARN] archive write failed for {nick}: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            failed += 1
            print(f"[ERROR] archive write exception for {nick}: {e}")

    print(f"[기수마감-{cohort_clean}기] Step 1 완료: created={created} failed={failed}")

    # ---------- Step 2: Archive ALL inline DBs under each track page ----------
    print(f"[기수마감-{cohort_clean}기] Step 2 시작: 트랙 페이지 하위 모든 인라인 DB 삭제")
    wiped_dbs = 0
    for tp in track_pages:
        dbs = get_inline_databases(tp['id'])
        for db in dbs:
            if delete_database(db['id']):
                wiped_dbs += 1
    print(f"[기수마감-{cohort_clean}기] Step 2 완료: archived {wiped_dbs} inline DBs")

    # ---------- Step 3: Reset ALL members (tracks, notes, status=휴식) ----------
    print(f"[기수마감-{cohort_clean}기] Step 3 시작: 멤버 마스터 전원 초기화")
    all_members = get_all_members()
    reset_ok, reset_fail = 0, 0
    for mem in all_members:
        if mem.get('archived', False):
            continue
        ok = update_page_properties(mem['id'], {
            "트랙": {"multi_select": []},
            "기타사항": {"rich_text": []},
            "활동 상태": {"status": {"name": "휴식"}},
        })
        if ok:
            reset_ok += 1
        else:
            reset_fail += 1
    print(f"[기수마감-{cohort_clean}기] Step 3 완료: reset {reset_ok}, fail {reset_fail}")

    return {
        "success": True,
        "message": f"{cohort_clean}기 마감 완료: 아카이브 {created}명 / 인라인DB {wiped_dbs}개 정리 / 멤버 {reset_ok}명 휴식 전환",
        "total": created + failed,
        "completed": completed_count,
        "dropped": dropped_count,
        "failed": failed,
        "wiped_dbs": wiped_dbs,
        "members_reset": reset_ok,
        "members_reset_failed": reset_fail,
    }

def set_member_intro(member_page_id, intro_text):
    """멤버 Notion 페이지 본문에 자기소개 블록 추가 (기존 본문 교체)"""
    # 1. 기존 블록 삭제 (본문 초기화)
    children_url = f"https://api.notion.com/v1/blocks/{member_page_id}/children"
    try:
        resp = SESSION.get(children_url, headers=get_headers(), timeout=TIMEOUT)
        if resp.status_code == 200:
            for block in resp.json().get('results', []):
                delete_url = f"https://api.notion.com/v1/blocks/{block['id']}"
                SESSION.delete(delete_url, headers=get_headers(), timeout=TIMEOUT)
    except:
        pass

    # 2. 자기소개 블록 추가
    # Notion rich_text는 2000자 제한이 있으므로 분할
    chunks = [intro_text[i:i+2000] for i in range(0, len(intro_text), 2000)]

    blocks = [
        {
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "📝 자기소개"}}]
            }
        }
    ]

    for chunk in chunks:
        blocks.append({
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            }
        })

    payload = {"children": blocks}

    try:
        resp = SESSION.patch(children_url, headers=get_headers(), json=payload, timeout=TIMEOUT)
        return resp.status_code == 200
    except Exception as e:
        print(f"[ERROR] set_member_intro failed: {e}")
        return False

def find_and_remove_from_group_db(member_page_id, track_name):
    """
    조 DB(인라인 DB)에서 해당 멤버를 찾아 아카이브(삭제)합니다.

    Args:
        member_page_id: 멤버의 Notion page ID
        track_name: 트랙 이름 (예: "AI 에이전트 트랙") — 해당 트랙의 조 DB만 검색

    Returns:
        dict: {"success": bool, "message": str}
    """
    group_db_id = os.getenv('GROUP_DB_ID')
    if not group_db_id:
        return {"success": False, "message": "GROUP_DB_ID 환경변수가 없습니다."}

    # 동적으로 groupDbName 매핑 (bot_config.json의 trackConfig에서 로드)
    search_name = track_name
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot_config.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        for tc in config.get('trackConfig', []):
            if tc['notionName'] == track_name:
                search_name = tc.get('groupDbName', track_name)
                break
    except Exception as e:
        print(f"[WARN] Failed to load trackConfig for group mapping: {e}")

    try:
        # 1. 트랙 페이지 목록 조회
        track_pages = fetch_all_pages(f'https://api.notion.com/v1/databases/{group_db_id}/query', {})

        target_page = None
        for page in track_pages:
            props = page.get('properties', {})
            for key, val in props.items():
                if val.get('type') == 'title':
                    title_list = val.get('title', [])
                    if title_list:
                        page_track_name = title_list[0]['text']['content']
                        if page_track_name == search_name:
                            target_page = page
                            break
            if target_page:
                break

        if not target_page:
            return {"success": False, "message": f"트랙 '{search_name}'(원본: '{track_name}')을 찾을 수 없습니다."}

        # 2. 해당 트랙의 인라인 DB(조) 목록 조회
        inline_dbs = get_inline_databases(target_page['id'])

        # 3. 각 조 DB에서 멤버 검색 후 삭제
        for db in inline_dbs:
            members = fetch_all_pages(f"https://api.notion.com/v1/databases/{db['id']}/query", {})
            for m in members:
                if m['id'] == member_page_id:
                    # 직접 매칭
                    archive_page(m['id'])
                    return {"success": True, "message": f"조 '{db['title']}'에서 멤버 삭제 완료."}

                # relation으로 연결된 경우도 체크
                for prop_val in m.get('properties', {}).values():
                    if prop_val.get('type') == 'relation':
                        relations = prop_val.get('relation', [])
                        for rel in relations:
                            if rel.get('id') == member_page_id:
                                archive_page(m['id'])
                                return {"success": True, "message": f"조 '{db['title']}'에서 멤버 삭제 완료."}

        return {"success": True, "message": "조 DB에서 해당 멤버를 찾지 못했습니다 (이미 제거됨)."}

    except Exception as e:
        print(f"[Exception] find_and_remove_from_group_db: {e}")
        return {"success": False, "message": str(e)}

def archive_page(page_id):
    """Archives (deletes) a page"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"archived": True}
    try:
        resp = SESSION.patch(url, headers=get_headers(), json=payload, timeout=TIMEOUT)
        if resp.status_code == 200:
            print(f"[Success] Archived page: {page_id}")
            return True
        else:
            print(f"[Error] Failed to archive page {page_id}: {resp.text}")
            return False
    except Exception as e:
        print(f"[Exception] archive_page: {e}")
        return False
def get_active_members_deduplicated():
    """활동중인(6기) 멤버를 대시보드 기준으로 중복 제거하여 상위 데이터만 반환"""
    raw_members = get_all_members() 
    
    target_cohort = os.getenv('CURRENT_COHORT', '6')
    target_c_num = ''.join(filter(str.isdigit, str(target_cohort)))
    
    unique_map = {}
    for m in raw_members:
        try:
            name = get_property_value(m, '이름', '디스코드 닉네임')
            d_id = get_property_value(m, '사용자 ID')
            if not name and not d_id: continue
            
            # Extract Cohort
            cohort_val = ""
            for k, v in m['properties'].items():
                if any(x in k for x in ['기수', 'Cohort', '기']):
                    if v['type'] == 'select': cohort_val = v['select'].get('name') if v['select'] else ""
                    elif v['type'] == 'multi_select': cohort_val = v['multi_select'][0].get('name') if v['multi_select'] else ""
                    elif v['type'] == 'rich_text' and v['rich_text']: cohort_val = v['rich_text'][0].get('plain_text', "")
                    elif v['type'] == 'title' and v['title']: cohort_val = v['title'][0].get('plain_text', "")
                    if cohort_val: break
            
            # Map Tracks (ONLY from '트랙' property)
            mapped_tracks = []
            if '트랙' in m['properties']:
                prop = m['properties']['트랙']
                if prop['type'] in ['select', 'multi_select']:
                    opts = []
                    if prop['type'] == 'multi_select': opts = prop['multi_select']
                    elif prop.get('select'): opts = [prop['select']]
                    for opt in opts:
                        mapped = map_track(opt['name'])
                        if mapped != "Unassigned" and mapped not in mapped_tracks:
                            mapped_tracks.append(mapped)

            # Dashboard Filter: Must have at least one valid track.
            if not mapped_tracks:
                continue

            m_obj = {
                "notion_id": m['id'],
                "name": name,
                "discord_id": d_id,
                "tracks": mapped_tracks,
                "cohort": cohort_val,
                "created_time": m['created_time']
            }

            key = d_id if d_id and d_id != "Unknown" else name
            
            if key not in unique_map:
                unique_map[key] = m_obj
            else:
                existing = unique_map[key]
                e_c6 = target_c_num in str(existing['cohort'])
                m_c6 = target_c_num in str(m_obj['cohort'])
                
                # Priority: Target Cohort > Latest
                if m_c6 and not e_c6:
                    unique_map[key] = m_obj
                elif e_c6 and not m_c6:
                    continue
                else:
                    if m_obj['created_time'] > existing['created_time']:
                        unique_map[key] = m_obj
        except: continue
        
    return list(unique_map.values())

def get_normalized_submissions(target_sunday):
    """지정한 일요일 날짜에 해당하는 모든 유효 제출물을 정규화하여 반환"""
    try:
        dt = datetime.strptime(target_sunday, '%Y-%m-%d')
        # Buffer to catch early/late submissions
        cutoff_date = (dt - timedelta(days=14)).strftime('%Y-%m-%d')
    except:
        cutoff_date = (datetime.now() - timedelta(days=21)).strftime('%Y-%m-%d')

    submission_filter = {
        "or": [
            {"property": "제출 날짜", "date": {"on_or_after": cutoff_date}},
            {"timestamp": "created_time", "created_time": {"on_or_after": cutoff_date}}
        ]
    }
    raw_subs = get_all_submissions(filter_payload=submission_filter)
    
    # Pre-fetch assignments for mapping
    all_ag = get_all_assignments()
    ag_map = {}
    for ag in all_ag:
        due = ag['properties'].get('마감일', {}).get('date', {}).get('start', '')
        ts = ag['properties'].get('타입', {}).get('multi_select', [])
        is_sf = any("숏폼" in t['name'] for t in ts)
        tracks = [map_track(t['name']) for t in ag['properties'].get('트랙', {}).get('multi_select', [])]
        ag_map[ag['id']] = {"date": due, "is_shortform": is_sf, "tracks": tracks}

    normalized = []
    for sub in raw_subs:
        try:
            submitter_rel = sub['properties'].get('제출자', {}).get('relation', [])
            if not submitter_rel: continue
            
            m_id = submitter_rel[0]['id']
            
            # Date Alignment
            manual_date = None
            m_date_prop = sub['properties'].get('제출 날짜', {})
            if m_date_prop and m_date_prop.get('date'):
                manual_date = m_date_prop['date'].get('start')
            
            created_date = sub['created_time'].split('T')[0]
            
            # Match Dashboard's Cohort Start Date filter
            # original_submission_date = manual_date if manual_date else created_date
            # skip if original_submission_date < 2026-02-11
            actual_sub_date = manual_date if manual_date else created_date
            cohort_start = os.getenv('COHORT_START_DATE', '2026-02-11')
            if actual_sub_date < cohort_start:
                continue

            # Dashboard logic: Assignment Due Date > Manual > Created (Aligned to Sunday)
            final_date = manual_date if manual_date else created_date
            
            linked = sub['properties'].get('과제 페이지 DB', {}).get('relation', [])
            if linked:
                ag_id = linked[0]['id']
                if ag_id in ag_map and not ag_map[ag_id]['is_shortform'] and ag_map[ag_id]['date']:
                    final_date = ag_map[ag_id]['date']

            # Weekly Align Fix
            if final_date:
                fdt = datetime.strptime(final_date, '%Y-%m-%d')
                days_to_sun = (6 - fdt.weekday()) % 7
                aligned_date = (fdt + timedelta(days=days_to_sun)).strftime('%Y-%m-%d')
                
                if aligned_date == target_sunday:
                    # Dashboard Logic: If Submission has explicit track tags, use ONLY those.
                    # Fallback to linked assignment tracks ONLY if submission tracks are empty.
                    sub_tracks = []
                    
                    # Dashboard-Aligned Priority Logic:
                    # 1. Submission Properties (트랙 or 과제 타입) take precedence.
                    # 2. Linked Assignment tracks are used ONLY if submission properties are empty.
                    sub_tracks = []
                    
                    # Check '트랙' property
                    raw_sub_track = get_property_value(sub, '트랙')
                    if raw_sub_track:
                        sub_tracks = [map_track(t.strip()) for t in raw_sub_track.split(',')]
                    
                    # Check '과제 타입' if '트랙' was empty
                    if not sub_tracks:
                        raw_sub_type = get_property_value(sub, '과제 타입')
                        if raw_sub_type:
                            sub_tracks = [map_track(t.strip()) for t in raw_sub_type.split(',')]
                    
                    # Fallback to Linked Assignment ONLY if still empty
                    if not sub_tracks and linked:
                        ag_id = linked[0]['id']
                        if ag_id in ag_map:
                            sub_tracks = ag_map[ag_id].get('tracks', [])
                    
                    # Final fallback to Shortform
                    if not sub_tracks:
                        sub_tracks = ["Shortform"]

                    normalized.append({
                        "member_notion_id": m_id,
                        "assignment_id": linked[0]['id'] if linked else None,
                        "tracks": sub_tracks
                    })
        except: continue
        
    return normalized
