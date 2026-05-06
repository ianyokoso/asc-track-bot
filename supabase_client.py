import os
import requests
from datetime import datetime, timezone

def _get_config():
    url = os.getenv('SUPABASE_URL', 'https://jtmbwqmdbgncjmjgdmis.supabase.co')
    key = os.getenv('SUPABASE_SERVICE_KEY', '')
    return url, key

def _headers(upsert=False):
    _, key = _get_config()
    h = {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }
    if upsert:
        h['Prefer'] = 'resolution=merge-duplicates,return=minimal'
    return h

def upsert_dashboard(data):
    """Write dashboard data to Supabase dashboard_cache table."""
    url, key = _get_config()
    if not key:
        print("[SUPABASE] No service key configured, skipping write.")
        return False

    # Refuse to overwrite a populated cache with an empty payload — Notion fetch
    # failures must not nuke the dashboard. Allow the very first write (when the
    # row is missing) to seed an empty cache.
    members = data.get('members') if isinstance(data, dict) else None
    submissions = data.get('submissions') if isinstance(data, dict) else None
    if not members and not submissions:
        existing = get_dashboard()
        if existing and (existing.get('members') or existing.get('submissions')):
            print("[SUPABASE] Skipping write: incoming payload is empty and cache is populated.")
            return False

    endpoint = f'{url}/rest/v1/dashboard_cache'
    payload = {
        'key': 'dashboard',
        'data': data,
        'updated_at': datetime.now(timezone.utc).isoformat()
    }
    try:
        resp = requests.post(endpoint, json=payload, headers=_headers(upsert=True), timeout=15)
        if resp.status_code < 300:
            print(f"[SUPABASE] Dashboard data written successfully.")
            return True
        else:
            print(f"[SUPABASE] Write failed: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"[SUPABASE] Write error: {e}")
        return False

def append_submission(submission):
    """Append a single submission to the cached dashboard data (real-time update)."""
    _, key = _get_config()
    if not key:
        return False
    try:
        data = get_dashboard()
        if not data:
            return False
        subs = data.get('submissions', [])
        subs.append(submission)
        data['submissions'] = subs
        data['lastUpdated'] = datetime.now(timezone.utc).isoformat()
        return upsert_dashboard(data)
    except Exception as e:
        print(f"[SUPABASE] Append submission error: {e}")
        return False

def get_dashboard():
    """Read dashboard data from Supabase dashboard_cache table."""
    url, key = _get_config()
    if not key:
        print("[SUPABASE] No service key configured, skipping read.")
        return None

    endpoint = f'{url}/rest/v1/dashboard_cache?key=eq.dashboard&select=data,updated_at'
    try:
        resp = requests.get(endpoint, headers=_headers(), timeout=10)
        if resp.status_code == 200:
            rows = resp.json()
            if rows and rows[0].get('data'):
                data = rows[0]['data']
                if data.get('members') or data.get('submissions'):
                    print(f"[SUPABASE] Dashboard data loaded (updated: {rows[0].get('updated_at', 'N/A')})")
                    return data
        print(f"[SUPABASE] No cached data found.")
        return None
    except Exception as e:
        print(f"[SUPABASE] Read error: {e}")
        return None
