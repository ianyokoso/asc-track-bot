#!/bin/bash
# Oracle Cloud (=EC2) 배포 스크립트.
#
# 전제:
#   - ubuntu 사용자로 ssh 접속한 상태
#   - 이 repo 가 ~/asc-track-bot/ 에 clone 돼있음
#   - .env.test 는 별도로 손으로 채워둘 것 (secrets 포함이라 git 에 안 들어감)
#
# 실행:
#   cd ~/asc-track-bot && bash scripts/deploy-oracle.sh

set -e

REPO_DIR="$HOME/asc-track-bot"
PORT=8001

cd "$REPO_DIR"

echo "==> [1/6] git pull (latest main)"
git pull origin main

echo "==> [2/6] Python venv + 의존성 설치"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "==> [3/6] .env.test 존재 확인"
if [ ! -f .env.test ]; then
  echo "❌ .env.test 가 없습니다. 로컬 .env.test 를 scp 로 올린 뒤 다시 실행하세요."
  echo "   예: scp -i ~/Downloads/env.test/ssh-key-2026-01-25.key /Users/tuemarz/Downloads/ASC/asc-track-bot/.env.test ubuntu@168.107.16.76:~/asc-track-bot/.env.test"
  exit 1
fi

# Public URL 환경변수 자동 보정 (8001 포트 + 외부 IP 로 OAuth 콜백 잡기)
PUBLIC_HOST="$(curl -s ifconfig.me 2>/dev/null || echo '168.107.16.76')"
PUBLIC_BASE="http://${PUBLIC_HOST}:${PORT}"
sed -i "s|^DASHBOARD_APP_BASE_URL=.*|DASHBOARD_APP_BASE_URL=${PUBLIC_BASE}|" .env.test
sed -i "s|^DASHBOARD_API_BASE_URL=.*|DASHBOARD_API_BASE_URL=${PUBLIC_BASE}|" .env.test
sed -i "s|^DISCORD_REDIRECT_URI=.*|DISCORD_REDIRECT_URI=${PUBLIC_BASE}/api/auth/discord/callback|" .env.test
echo "    public URL: ${PUBLIC_BASE}"

echo "==> [4/6] iptables 포트 ${PORT} 오픈"
if ! sudo iptables -C INPUT -p tcp --dport ${PORT} -j ACCEPT 2>/dev/null; then
  sudo iptables -I INPUT 6 -p tcp --dport ${PORT} -j ACCEPT
  sudo netfilter-persistent save 2>/dev/null || sudo sh -c "iptables-save > /etc/iptables/rules.v4"
  echo "    iptables ${PORT} ACCEPT 추가됨"
else
  echo "    이미 열려있음"
fi

echo "==> [5/6] PM2 프로세스 등록"
# 기존 프로세스 있으면 재시작, 없으면 새로 시작
pm2 delete track-bot-api 2>/dev/null || true
pm2 delete track-bot 2>/dev/null || true

pm2 start "${REPO_DIR}/.venv/bin/python" \
  --name track-bot-api \
  --interpreter none \
  -- admin_server.py \
  --env-file ./.env.test \
  --cwd "${REPO_DIR}"

# admin_server / track_bot 모두 ASC_ENV=test 로 띄움 (현재는 test 워크스페이스 / test 길드 전용).
# pm2 는 .env 파일 자동 로드 안 해서 명시 export 필요 → ecosystem 파일로 관리.
cat > ecosystem.config.js <<EOF
module.exports = {
  apps: [
    {
      name: 'track-bot-api',
      script: '${REPO_DIR}/.venv/bin/python',
      args: 'admin_server.py',
      cwd: '${REPO_DIR}',
      interpreter: 'none',
      env: {
        ASC_ENV: 'test',
        TEST_DISCORD_GUILD_ID: '1500842736364814396',
        PYTHONUNBUFFERED: '1',
      },
    },
    {
      name: 'track-bot',
      script: '${REPO_DIR}/.venv/bin/python',
      args: 'track_bot.py',
      cwd: '${REPO_DIR}',
      interpreter: 'none',
      env: {
        ASC_ENV: 'test',
        TEST_DISCORD_GUILD_ID: '1500842736364814396',
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
EOF

pm2 delete track-bot-api 2>/dev/null || true
pm2 start ecosystem.config.js
pm2 save

echo "==> [6/6] 동작 확인"
sleep 3
echo "--- /apply HTTP status ---"
curl -s -o /dev/null -w "  HTTP %{http_code}\n" "http://localhost:${PORT}/apply"
echo "--- /api/auth/me ---"
curl -s "http://localhost:${PORT}/api/auth/me?next=/apply" | head -c 200
echo ""
echo ""
echo "✅ 배포 완료."
echo "   외부 접속 URL: ${PUBLIC_BASE}/apply"
echo ""
echo "🔔 Discord Developer Portal 에서 OAuth2 → Redirects 에 추가하세요:"
echo "   ${PUBLIC_BASE}/api/auth/discord/callback"
