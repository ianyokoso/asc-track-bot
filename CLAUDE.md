# CLAUDE.md — asc-track-bot 운영/배포 가이드

> 이 파일은 매 세션 자동 로드된다. 새 세션에서 아키텍처를 다시 탐색하지 말고 **여기부터 읽어라.**
> 시크릿(토큰/키 값)은 절대 여기 적지 않는다. 값은 서버의 `.env.test`(gitignore)에 있다.

## 한 줄 요약
ASC 트랙 신청 → 멤버 마스터 DB 확정 → Discord 트랙별 역할/채널 자동 생성 봇.
운영 봇 `asc-discord-bot`(출석/휴가/라운지)과 **완전 분리**된 별도 repo/봇/워크스페이스.

## ⚠️ 가장 중요한 사실 (여기서 막히면 다 막힘)
- **로컬(이 맥, `Ian.local` / 192.168.0.28)에서는 봇/서버가 안 돈다.** 전부 **Oracle 서버에 올라가 있다.**
- 그래서 로컬 코드 수정은 **배포 전까지 라이브에 반영 안 됨.** "왜 반영 안 되냐" 의 99%는 미배포 때문.
- 배포는 **git 기반**: 맥에서 push → 서버에서 git pull + pm2 restart.

## 런타임 토폴로지
- **Oracle 서버**: `168.107.16.76` (Ubuntu). repo 경로 `~/asc-track-bot`.
- **PM2 프로세스** (서버에서 `pm2 list`):
  - `track-bot-api` — `admin_server.py` (Flask 백엔드, port **8001**). `track-apply.html` 서빙 + API.
  - `track-bot` — `track_bot.py` (Discord 봇). 채널/역할 실제 생성 담당.
  - (그 외 `asc-bot`, `asc-lounge`, `dashboard-api/ui` 는 **다른 프로젝트** — 건드리지 말 것)
- **Vercel**: `https://asc-track-bot.vercel.app` 이 `/api/*`, `/apply`, `/static/*` 를 Oracle `:8001` 로 rewrite ([vercel.json](vercel.json)). 사용자는 Vercel URL 로 접속.
- **SSH 접속**: `ssh -i ~/Downloads/env.test/ssh-key-2026-01-25.key ubuntu@168.107.16.76`

## Discord 길드 (서버)
| 용도 | 길드 ID | 이름 | 비고 |
|---|---|---|---|
| **prod (실제 생성 대상)** | `1383082575500677142` | AI 솔로프리너 클럽 (AI Solopreneur Club) | 채널/역할은 **여기** 생긴다. 코드 블랙리스트에 있음 |
| test | `1500842736364814396` | 이안/스탭님의 서버 | 테스트용 |
- 봇 계정: `ASC_TRACK_BOT#9225` (ID 1500854825347190855). **두 길드 모두 가입돼 있음.**
- `env_utils.PROD_DISCORD_GUILD_BLACKLIST = {1383082575500677142}` — 안전장치. test 모드 기본 경로는 이 길드를 거부.
- **그런데 prod 서버(1383)에 만들어야 하므로** `DISCORD_TARGET_GUILD_ID` override 로 블랙리스트를 우회한다 (의도된 동작).

## 환경변수 (핵심)
서버는 `ASC_ENV=test` 로 돈다 (test 워크스페이스/토큰 사용, 단 Discord 는 override 로 prod 길드 타겟).
- **`.env.test` 는 서버에서 손으로 관리** (gitignore, 시크릿 포함). 로컬 `.env.test` 와 **별개 파일**이다. 로컬에서 고쳐도 서버에 안 올라감 → 서버에서 직접 수정해야 함.
- `.env.test` 는 `load_backend_env` 가 **`override=True`** 로 로드 → pm2 ecosystem env 보다 **우선** ([env_utils.py:81](env_utils.py#L81)).
- **`DISCORD_TARGET_GUILD_ID`** = 채널/역할 생성 대상 길드. **반드시 `1383082575500677142`.** (한때 `1500…`(테스트)로 잘못 박혀 prod 대신 테스트에 생기는 사고 있었음.)
- 길드 선택 로직 `resolve_active_guild` ([env_utils.py:126](env_utils.py#L126)) 우선순위: ① `DISCORD_TARGET_GUILD_ID`(override, 블랙리스트 무시) → ② test 모드+`TEST_DISCORD_GUILD_ID` → ③ `bot.guilds[0]`.
- `CURRENT_COHORT` = 현재 기수 숫자 (예 `10`). 대시보드 "기수·기간 설정" 에서 변경 → `.env` 에 저장. 프론트 `COHORT_LABEL` 도 여기서 옴.

## 배포 (맥에서 실행)
```bash
cd /Users/tuemarz/Downloads/ASC/asc-track-bot
bash scripts/push-and-deploy.sh --light   # git push + 서버 git pull + track-bot-api(admin)만 재시작
bash scripts/push-and-deploy.sh           # full: deploy-oracle.sh (pip 설치 + ecosystem 재생성 + 봇까지 재시작)
```
- **프론트(`track-apply.html`)만 바꿨으면 `--light` 로 충분.** admin 이 요청마다 파일을 새로 읽어 서빙(no-cache)하므로 사실 git pull 만으로도 적용됨.
- **봇 코드(`cogs/`, `track_bot.py`) 바꿨으면 봇 재시작 필요** → full 또는 `pm2 restart track-bot`.
- 봇 재시작 시 Discord 에 **"🚀 Server Restarted"** 메시지가 옴. 프론트만 배포(--light)면 안 옴 — **정상**.
- 배포 검증: 서버 pull 로그에 최신 커밋 해시 보이는지 / 브라우저 하드리로드(`Cmd+Shift+R`) 후 동작 확인.
- ⚠️ `full` 배포가 만드는 `ecosystem.config.js` 는 봇 env 에 `TEST_DISCORD_GUILD_ID=1500…` 를 박지만, `.env.test` 의 `DISCORD_TARGET_GUILD_ID=1383…`(override=True)가 이긴다. **즉 prod 타겟은 서버 `.env.test` 가 지킨다.**

## "일괄 반영" (조 안 나눔 워크플로우 — 2026 개편)
대시보드 `/apply` (group-preview 뷰) 의 **일괄 반영** 버튼 흐름:
1. 프론트 `runBulkApply` → `POST /api/mockups/group-preview/commit` ([static/track-apply.html](static/track-apply.html))
2. admin `_commit_group_preview_to_notion` ([admin_server.py:3456](admin_server.py#L3456)) → 노션 멤버 마스터 DB 갱신 → `bot_command_queue.test.json` 에 `group_preview_sync_discord` 명령 기록
3. 봇 `cogs/ipc.py` → `cogs/admin.py: sync_discord_group_preview` → `resolve_active_guild` 길드에 역할/채널 생성
- **데이터 모델**: **트랙 신청 DB = 정답.** 멤버 마스터 DB 는 거기서 채워나가는 DB. → 일괄 반영은 `autoCreateMissing: true` 로 보내 마스터 DB 에 없는 신청자를 신규 row 로 생성(트랙·Discord ID 저장 → 다음 실행부터 매칭, 중복 X). 트랙 1개 이상 신청자만 반영(미신청자 제외).
- 노션만 반영하려면 "노션만 반영"(`notionOnly`) 버튼 — Discord skip.
- 봇이 안 떠 있거나 길드 못 찾으면 confirm 후 timeout/에러. 토스트의 "역할 N건 부여" + alert 메시지로 진단.

## 새 기수 작업 절차 (11기, 12기 …)
1. 대시보드 "기수·기간 설정" 에서 **기수/신청 기간 변경** (`CURRENT_COHORT` 갱신).
2. (이전 기수 정리 필요 시) Discord 에서 `!채널검사 <N>` 로 점검 후 `!채널삭제 <N>` 실행 — prod 길드(1383)에서.
3. 신청 받기 → 대시보드에서 신청자 목록 로드.
4. **일괄 반영** 클릭 → 노션 마스터 DB 반영 + prod 길드에 트랙별 역할/채널 생성.
5. 코드 변경이 있었으면 먼저 `bash scripts/push-and-deploy.sh` 로 배포.

## 운영 명령어 (Discord, prod 길드에서)
- `!채널검사 <기수>` — 삭제 전 read-only 점검 ([cogs/admin.py:1259](cogs/admin.py#L1259))
- `!채널삭제 <기수>` — 해당 기수 트랙 채널/역할 정리 (공지·카테고리·다른 기수 보존) ([cogs/admin.py:1459](cogs/admin.py#L1459))
- `!역할삭제 <기수>` — 역할만 정리 ([cogs/admin.py:1725](cogs/admin.py#L1725))

## 함정 (footguns) 모음
- **미배포**: 로컬 수정 = 라이브 아님. 항상 배포 후 하드리로드.
- **잘못된 머신에서 git pull**: `tuemarz@Ian`(맥)에서 pull 하면 "Already up to date" 만 뜨고 서버엔 안 감. 배포는 `push-and-deploy.sh`.
- **`pm2 restart … --update-env` 금지**: 셸 env 로 덮어써서 `ASC_ENV=test` 가 날아가 봇이 legacy/prod 모드로 뜰 수 있음. 평범하게 `pm2 restart <name>` 만.
- **`.env.test` 로컬 vs 서버 혼동**: 시크릿/길드 설정은 **서버 파일**이 진짜. 로컬 건 참고용.
- **`DISCORD_TARGET_GUILD_ID` 가 테스트 길드로 박히면** prod 대신 테스트에 채널 생성됨. 항상 1383 확인.
- **프론트 에러 조용히 삼킴**: `runBulkApply()` 는 `.catch` 로 표면화돼 있음 — alert 안 뜨면 confirm 단계 전에서 죽은 것.
