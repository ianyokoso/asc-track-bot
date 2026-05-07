#!/bin/bash
# Mac 로컬 → GitHub → Oracle 서버 자동 배포 (one-shot).
#
# 흐름:
#   1) 로컬 변경 git push origin main
#   2) Oracle 서버에 ssh 접속해서 deploy-oracle.sh 실행
#
# 전제:
#   - Mac 의 ~/Downloads/env.test/ssh-key-2026-01-25.key 가 Oracle 인스턴스의 ubuntu 키
#   - 168.107.16.76 가 Oracle 인스턴스 외부 IP
#   - Oracle 측 ~/asc-track-bot 에 이미 clone 돼있고 .env.test 가 채워져 있음
#
# 실행 (Mac 에서):
#   cd /Users/tuemarz/Downloads/ASC/asc-track-bot
#   bash scripts/push-and-deploy.sh
#
#   또는 가벼운 변경만 있을 때:
#   bash scripts/push-and-deploy.sh --light

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SSH_KEY="$HOME/Downloads/env.test/ssh-key-2026-01-25.key"
SSH_HOST="ubuntu@168.107.16.76"
SERVER_REPO="~/asc-track-bot"

cd "$REPO_DIR"

echo "==> [1/2] git push origin main"
git push origin main

if [ "$1" = "--light" ]; then
  REMOTE_CMD="cd ${SERVER_REPO} && git pull origin main && pm2 restart track-bot-api"
  echo "==> [2/2] Oracle 서버 light deploy (git pull + pm2 restart)"
else
  REMOTE_CMD="cd ${SERVER_REPO} && bash scripts/deploy-oracle.sh"
  echo "==> [2/2] Oracle 서버 full deploy (deploy-oracle.sh)"
fi

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_HOST" "$REMOTE_CMD"

echo ""
echo "✅ 전체 배포 완료. https://asc-track-bot.vercel.app/apply"
