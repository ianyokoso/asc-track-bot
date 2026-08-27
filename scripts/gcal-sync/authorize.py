"""구글 캘린더 접근 권한을 1회 받아 refresh token 을 저장한다.

브라우저 승인은 계정 주인만 할 수 있으니 딱 한 번은 사람이 눌러야 한다.
대신 여기서 받은 refresh token 을 `.env.test` 에 넣어두면 그 뒤로는 무인으로 돈다
(Slack 명령·대시보드 버튼·크론 전부 사람 개입 없이).

    python3 scripts/gcal-sync/authorize.py ~/Downloads/client_secret_....json
"""
from __future__ import annotations

import http.server
import json
import pathlib
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

SCOPE = "https://www.googleapis.com/auth/calendar"
PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

ENV_PATH = pathlib.Path(__file__).resolve().parents[2] / ".env.test"

_received: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib 규약
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _received.update({k: v[0] for k, v in query.items()})
        body = (
            "<h2>승인 완료</h2><p>터미널로 돌아가세요.</p>"
            if "code" in _received
            else f"<h2>승인 실패</h2><p>{_received.get('error', '알 수 없는 오류')}</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # 접속 로그로 화면 어지럽히지 않는다
        pass


def _upsert_env(key: str, value: str) -> None:
    """.env.test 의 키를 갈아끼운다. 없으면 덧붙인다."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("사용법: authorize.py <client_secret_....json>")

    raw = json.loads(pathlib.Path(sys.argv[1]).expanduser().read_text())
    client = raw.get("installed") or raw.get("web")
    if not client:
        sys.exit("OAuth 클라이언트 JSON 이 아닙니다 (installed/web 키 없음)")

    params = urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",   # refresh token 을 받기 위해 필수
            "prompt": "consent",        # 재승인 때도 refresh token 을 다시 준다
        }
    )
    print(f"프로젝트: {client.get('project_id')}")
    print("\n아래 주소를 브라우저에서 열고 승인하세요:\n")
    print(f"{AUTH_URL}?{params}\n")

    server = http.server.HTTPServer(("localhost", PORT), _Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=300)
    server.server_close()

    if "code" not in _received:
        sys.exit(f"승인 실패 또는 시간 초과: {_received.get('error', '응답 없음')}")

    body = urllib.parse.urlencode(
        {
            "code": _received["code"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(TOKEN_URL, data=body), timeout=60
        ) as resp:
            token = json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"토큰 교환 실패 {exc.code}: {exc.read().decode()[:300]}")
    if "refresh_token" not in token:
        sys.exit("refresh_token 이 안 왔습니다. 기존 승인을 해제하고 다시 시도하세요.")

    _upsert_env("GOOGLE_OAUTH_CLIENT_ID", client["client_id"])
    _upsert_env("GOOGLE_OAUTH_CLIENT_SECRET", client["client_secret"])
    _upsert_env("GOOGLE_OAUTH_REFRESH_TOKEN", token["refresh_token"])
    print(f"✅ refresh token 을 {ENV_PATH} 에 저장했습니다. 이제 무인 실행됩니다.")


if __name__ == "__main__":
    main()
