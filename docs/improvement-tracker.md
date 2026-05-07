# Improvement Tracker

asc-track-bot 의 사용자 흐름·아키텍처 검토 결과 + 개선 진행 상황.
하나씩 처리하면서 status 갱신.

**Status legend**: `🔲 todo` · `🚧 in-progress` · `✅ done` · `⏸️ paused` · `❌ wontfix`

**Last reviewed**: 2026-05-07

---

## 🔴 Critical — 데모 막히는 것

### #1. Vercel 프록시 30초 타임아웃 vs Notion 순차 호출
- **Status**: ✅ done (2026-05-07)
- **증상**: 조 배정 완료 클릭 → 브라우저는 alert "실패", 백엔드는 200 성공
- **원인**: Vercel 무료 rewrite 30s timeout. 9개 트랙 × 조 × 멤버 row 추가 = 순차 Notion API 가 30s+ 소요
- **영향**: 데모 시연 때마다 false-fail alert. 사용자는 실패로 인식
- **구현**:
  - POST `/api/mockups/group-preview/commit` → 즉시 `{jobId}` (202)
  - 백그라운드 daemon thread 가 `_commit_group_preview_to_notion` 실행
  - 진행상황은 _COMMIT_JOBS dict (threading.Lock 보호) 에 phase/phaseDetail/tracksProcessed 기록
  - GET `/api/mockups/group-preview/commit/status?jobId=X` → job snapshot
  - 클라: 2초 폴링 (최대 10분), `onProgress(phase, detail, job)` 콜백으로 modal step 갱신
  - 완료 job 1시간 후 자동 정리 (`_cleanup_old_commit_jobs`)
- **검증**:
  - 빠른 입력 검증 (target / members / tracks 누락) 은 thread 띄우기 전 4xx
  - admin 권한 체크 양 endpoint 공통 적용

### #2. Notion 마스터 DB 스키마 미스매치 (트랙명 multi_select 없음)
- **Status**: 🔲 todo
- **증상**: 매 commit 마다 `Could not find property with name or id: 트랙명` 400 spam
- **원인**: 사용자 마스터 DB 에 `트랙명` multi_select column 없음. `이름` title 폴백으로만 동작
- **영향**: 로그 노이즈 + Notion 호출 1회 낭비 + 향후 prod 워크스페이스로 옮길 때 동작 안 할 위험
- **해결안 A**: 마스터 DB 에 `트랙명` multi_select column 추가 (Notion UI 작업)
- **해결안 B**: 코드에서 1차 쿼리 (트랙명 multi_select) 제거하고 title 부터 시도
- **관련 파일**: `admin_server.py` (`_find_track_page_in_master_db`)

---

## 🟡 High — 곧 문제 될 것

### #3. 조 배정 commit 비-멱등 (idempotency 없음)
- **Status**: 🔲 todo
- **증상**: 더블클릭 / 타임아웃 후 재시도 → 같은 트랙에 inline DB 중복 생성 가능
- **현재**: `_archive_track_page_inline_dbs` 가 기존 inline DB 삭제 후 재생성하므로 부분 idempotent
- **위험**: 부분 실패 (Notion 일부 + Discord 실패) 시 rollback 없음. stale 상태 누적
- **해결안**:
  - server-side commit lock (트랙 페이지 단위)
  - 또는 client-side: commit 진행 중 버튼 disable + 동일 jobId 재사용 (#1 의 비동기 패턴 도입 시 자연 해결)

### #4. Mock 데이터와 실 사용자 데이터 혼합
- **Status**: 🔲 todo
- **증상**: HTML 에 120명 mock 멤버 하드코딩. 서버 캐시는 별도. 실 사용자 신청 시 admin 페이지에 mock + real 혼재
- **영향**: 운영진이 demo 데이터인지 실 데이터인지 헷갈림
- **해결안**:
  - prod 모드 (`ASC_ENV=prod`) 에서는 mock 멤버 비활성화 토글
  - admin 페이지에 mock row 시각 구분 (배지 "MOCK" 또는 회색 처리)
  - "Mock 숨기기" 토글 버튼

### #5. 운영자 역할 변경 시 즉시 반영 안 됨
- **Status**: 🔲 todo
- **증상**: Discord 에서 운영자 역할 부여해도 5분 role ID 캐시 + 기존 세션 만료까지 isAdmin 갱신 안 됨
- **현재 동작**: `/api/auth/me` 는 live admin 체크하지만, 클라이언트 `IS_VIEWER_ADMIN` 은 OAuth 콜백 query param 만 봄
- **해결안**:
  - 페이지 로드 시 `/api/auth/me` 호출 → `data.isAdmin` 으로 `IS_VIEWER_ADMIN` 갱신
  - 역할 캐시 TTL 짧게 (5분 → 30초)

### #6. 세션 쿠키 cross-origin 취약성
- **Status**: 🚧 partially fixed (commit fetch 에 credentials 추가됨)
- **증상**: 페이지 = Vercel 도메인, API = Vercel rewrite → Oracle. 세션 쿠키는 Oracle 발급
- **위험**: SameSite/Secure 잘못되면 일부 fetch 에서 쿠키 미전송 → 401/403
- **해결안**:
  - 모든 fetch 호출에 `credentials: 'include'` 명시 (전체 grep & fix)
  - 쿠키 SameSite=None, Secure=True 확인

### #7. Flask dev server 가 prod 서빙
- **Status**: 🔲 todo
- **증상**: pm2 로그에 매번 `WARNING: This is a development server.` 경고
- **위험**: 동시 요청 처리 ↓, traffic spike 에 취약, 일부 보안 이슈
- **해결안**:
  - `gunicorn -w 4 -b 0.0.0.0:8001 admin_server:app` 로 띄우기
  - PM2 ecosystem 변경
  - requirements.txt 에 gunicorn 추가

---

## 🟢 Medium — 개선하면 좋음

### #8. "전체 초기화" 가 Notion/Discord 안 건드림
- **Status**: 🚧 partially done (서버 캐시 + localStorage 만 wipe)
- **현재**: 사용자가 `!테스트초기화` Discord 명령 별도 실행 필요 (안내는 alert 에 있음)
- **이상적**: 단일 버튼이 IPC 로 `cleanup_test_guild` 트리거 + Notion 마스터 DB 트랙 페이지 archive
- **블로커**: admin.py 의 cleanup_test_server 핵심 로직을 ctx 없이 호출 가능하도록 refactor 필요 (이전 시도 시 권한 거부됨)
- **해결안**:
  - admin.py 에 `run_test_guild_cleanup_logic(guild)` 헬퍼 추가 (ctx 의존 제거)
  - cogs/ipc.py 에 `cleanup_test_guild` 핸들러 추가
  - admin_server.py 의 `/api/admin/reset-track-applications` 에서 IPC 트리거

### #9. URL query param 으로 viewer 정보 영속
- **Status**: 🔲 todo
- **증상**: discordUserId / isAdmin / handle / avatar 가 URL 에 박힌 채 새로고침 / 북마크 / 공유에 그대로
- **위험**: 실수로 URL 공유하면 그 사람이 admin UI 봄 (실제 권한은 세션 기반이라 우회 안 되지만 UX 혼란)
- **해결안**:
  - OAuth 콜백 받은 직후 `history.replaceState({}, '', '/apply')` 로 깨끗한 URL 로 교체
  - viewer 정보는 그 시점에 이미 const 로 캡쳐된 상태

### #10. 동시 admin 작업 race condition
- **Status**: 🔲 todo
- **시나리오**: admin A 가 commit 중일 때 admin B 가 reset → 캐시 비었지만 Notion/Discord 부분 생성
- **현재 영향**: 운영진 1명이라 거의 발생 안 함. 운영진 늘면 위험
- **해결안**: server-side lock (file-based or in-memory) — commit 진행 중 reset / 다른 commit 거부

### #11. Mock 데이터에 시각 구분 없음
- **Status**: 🔲 todo
- **상세**: #4 에 포함

---

## ✅ Done — 최근 해결

### #1. 조 배정 commit 비동기 패턴 (Vercel timeout 우회)
- **Date**: 2026-05-07
- 위 #1 항목 참고. POST 202 + jobId / GET status 폴링.

### Group preview 자동 분할 알고리즘 재정의 (max 7 우선)
- **Date**: 2026-05-07
- **Commit**: `514dc61`
- max 7 hard / 4-5명 lump / min 5 soft / 균등 분배
- localStorage cache v3 → v4

### 운영진 권한을 Discord '운영자' 역할 기반으로 변경
- **Date**: 2026-05-06
- **Commit**: `7f87688`
- ADMIN_DISCORD_ROLE_NAME env (default '운영자')
- 길드 역할 ID 5분 캐시
- 화이트리스트 (ADMIN_DISCORD_USER_IDS) fallback 유지

### 시크릿 진입 시 로그인 화면 노출 + Mockup States 패널 숨김
- **Date**: 2026-05-06
- **Commit**: `647c108`
- VIEWER_USER_ID 만 검사 (handle placeholder 무시)
- `?devPanel=1` 옵트인

### 봇 토큰 중복 사용 정리 (asc-bot-test 제거)
- **Date**: 2026-05-06
- track-bot 단독 로그인 확인
- `~/asc-discord-bot/.env.test` → `.env.test.disabled.20260506` rename

### 자동 push + 배포 스크립트
- **Date**: 2026-05-06
- `scripts/push-and-deploy.sh` (--light 옵션)
- Mac → GitHub → Oracle 한 줄 배포

---

## 📋 다음 작업 제안 (우선순위 순)

1. ~~#1 비동기 commit 패턴~~ ✅ done
2. **#2 Notion 스키마 정리** ← 빠르게 끝남, 로그 깨끗
3. **#5 isAdmin 갱신 로직** ← 운영진 추가/제거 즉시 반영
4. **#7 gunicorn 전환** ← prod 안정성
5. **나머지** 데모 사용 패턴 보면서 우선순위 조정

---

## 🏗️ Architecture Bird's Eye

```
[Browser]
   ↓ HTTPS
[Vercel asc-track-bot.vercel.app]
   ↓ rewrite (max 30s)        ← bottleneck (#1)
[Oracle 168.107.16.76:8001 Flask dev server]   ← #7 prod-grade WSGI 필요
   ↓
[Notion API]   ← 트랙명 schema 미스매치 (#2)
[Discord API via Bot IPC]   ← bot 토큰 단독 사용 OK
```
