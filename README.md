# asc-track-bot

ASC 트랙 신청 + 조 배정 + Discord 채널/역할 자동 프로비저닝 전용 봇.

기존 [asc-discord-bot](https://github.com/ianyokoso/asc-discord-bot) 에서 트랙 관련 기능만 분리해 별도 repo / 별도 Discord 봇 / 별도 워크스페이스로 운영합니다. 운영 봇과 코드 / 토큰 / 길드 / Notion 워크스페이스가 완전히 분리돼 있어서 한쪽 변경이 다른 쪽에 영향을 주지 않습니다.

## 책임 범위

- **트랙 신청** — Discord OAuth 로그인 → apply form → Supabase + Notion master DB 동기화
- **조 배정 시뮬레이션** — admin 페이지에서 그룹 미리 배정 → "조 배정 완료" 클릭 시 commit
- **Notion 자동 생성** — master DB 안 트랙 페이지 row 자동 생성, 트랙 페이지 안 inline 조 DB 자동 생성, 멤버 row 추가
- **Discord 자동 생성** — `=====트랙명=====` 카테고리, `{prefix}-{cohort}기-공지`, `*-과제-인증`, `*-조장`, `*-N조`, `*-N조-화상미팅` 채널 + 트랙별 / 조별 / 조장 역할 자동 생성
- **라이트 트랙 처리** — 라이트 트랙 멤버는 parent 트랙의 공지 + 과제-인증 채널에만 read 권한 부여 (조/조장/화상미팅 미공개)
- **운영 명령어** — `!테스트초기화` (test 길드 채널/카테고리/역할 일괄 정리)

## 분리되지 않는 것

- 출석 / 휴가 / 업무요청 / 라운지 예약 / announcement scheduler / 멤버 데이터 sync — 기존 운영 봇 (`asc-discord-bot`) 에 그대로 유지

## 안전 가드

- `env_utils.PROD_DISCORD_GUILD_BLACKLIST` 에 운영 디스코드 길드 ID 가 박혀있어, 어떤 환경 / 설정 실수에도 운영 길드의 채널 / 역할을 절대 건드리지 않음
- test 모드는 `TEST_DISCORD_GUILD_ID` 와 일치하는 길드만 사용
- Notion 토큰은 매 요청 종료 시 prod 토큰으로 강제 복원되어 워크스페이스 누수 차단

## 실행

```bash
pip install -r requirements.txt
cp .env.example .env.test   # 또는 .env.prod
# 값 채우고:
ASC_ENV=test python3 admin_server.py     # Flask 백엔드 (port 8000)
ASC_ENV=test python3 track_bot.py        # Discord 봇
```

## 디렉토리 구조

```
.
├── admin_server.py          # Flask 백엔드 (track-apply API)
├── track_bot.py             # Discord 봇 엔트리포인트
├── notion_api.py            # Notion REST 헬퍼
├── env_utils.py             # 환경변수 로드 + PROD 블랙리스트
├── config.py                # bot_config.json + .env 로드
├── supabase_client.py       # 신청 캐시 저장소
├── bot_config.json          # 트랙 / 조 / 채널 메타데이터 (cohort 별)
├── cogs/
│   ├── ipc.py               # admin_server → bot 명령 큐 처리
│   └── admin.py             # 트랙 / 조 채널 생성 + 라이트 트랙 + cleanup
└── utils/
    └── helpers.py           # 날짜 / 주차 계산
```

## 관련 프로젝트

- `asc-discord-bot` — 운영 ASC 봇 (출석 / 휴가 / 업무요청 / 라운지)
- `asc-bot-admin-dashboard` — admin / personal dashboard, apply page 호스팅
