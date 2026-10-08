# 일정 갱신 가이드 (13기 기준)

ASC 일정은 **노션 '구글 캘린더용 DB' 가 원본**이다. 거기만 고치고 스크립트를 돌린다.
HTML·설정을 직접 고치면 네 곳 중 일부만 바뀌어 사용자에게 서로 다른 날짜가 보인다.

## 일정이 들어가는 네 곳

| | 사용자가 보는 곳 | 실제 데이터 | 갱신 방법 |
|---|---|---|---|
| ① | 신청 폼 `일정 확인하기` 표 | `static/track-apply.html` 의 `NOTION_SCHEDULE` 블록 | 아래 스크립트 |
| ② | `트랙 상세보기` | 같은 파일 `PREOPEN_TRACKS[].weeks` | 아래 스크립트 |
| ③ | 신청 폼 하단 `공통 일정` | **서버** `cohort_config_test.json` 의 `commonSchedule` | 아래 스크립트(서버에서) |
| ④ | ③ 목록의 HTML 폴백 | `track-apply.html` 의 `ul.js-common-schedule` (뷰마다 하나) | 아래 스크립트 |
| ⑤ | 구글 캘린더 | 구글 캘린더 6개 | `gcal_sync.py` |

①②③④ 는 `scripts/sync_schedule_from_notion.py`, ⑤ 는 `gcal_sync.py` 가 담당한다.
④ 는 평소 ③ 이 덮어쓰지만 설정이 비거나 API 가 죽으면 그대로 보이므로 같이 맞춘다.
둘 다 트랙 목록·노션 DB id 를 `gcal_schedule.SOURCES` 에서 가져오므로 원본은 하나다.

## 일정을 바꿀 때

```bash
# 0) 노션 '구글 캘린더용 DB' 에서 일정 수정

# 1) 신청 페이지 ①② — 먼저 미리보기로 무엇이 바뀌는지 확인
python3 scripts/sync_schedule_from_notion.py
python3 scripts/sync_schedule_from_notion.py --apply
bash scripts/push-and-deploy.sh --light

# 2) 공통 일정 ③ — 서버 설정이라 서버에서 (ASC_ENV 필수)
ssh -i <키> ubuntu@168.107.16.76 \
  'cd ~/asc-track-bot && ASC_ENV=test python3 scripts/sync_schedule_from_notion.py --apply'

# 3) 구글 캘린더 ④
python3 gcal_sync.py --dry-run --cohort 13
python3 gcal_sync.py --apply --cohort 13
```

전부 **미리보기가 기본**이고 `--apply` 를 줘야 바뀐다. 두 번 돌려도 결과가 같다(멱등).

## 밟기 쉬운 지뢰

**하단 '공통 일정' 은 HTML 을 고쳐도 안 바뀐다.**
서버 설정(`cohort-config.commonSchedule`)이 있으면 JS 가 통째로 덮어쓴다. HTML 값은
설정이 비었을 때만 쓰는 폴백이다. 대시보드 **기수·기간 설정**에서 저장해도 바뀌므로,
스크립트로 넣은 값이 거기서 덮어써질 수 있다 — **둘 중 한 곳에서만** 관리할 것.

**서버에서 돌릴 땐 `ASC_ENV=test` 를 붙인다.**
없으면 `.env.test` 를 안 읽어 노션 토큰·구글 자격증명을 못 찾고, 설정 파일 경로도
못 잡는다. pm2 는 이미 그 환경으로 돌기 때문에 Slack(Hermes) 경로는 영향 없다.

**시각이 같아도 제목이 다르면 다른 일정이다.**
13기 10/14(수) 20–22시에 공통 `세일즈 고민상담소` 와 빌더 1주차 강의가 겹쳤는데,
시간만 보고 중복 제거하다 빌더 1주차 강의가 캘린더에서 통째로 사라진 적이 있다.
지금은 제목까지 비교한다(`gcal_schedule.exclude_common`).

**트랙 표에는 공통 일정 중 OT 만 넣는다.**
나머지 공통 일정(네트워킹·특강 등)은 하단 `공통 일정` 목록에만. 트랙 표에 전부 넣으면
같은 일정이 트랙 수만큼 반복돼 표가 길어진다.

**노션 DB 하나가 막히면 전체가 멈춘다.**
소스를 순차 조회하다 404 가 나면 그대로 예외가 올라간다. 13기에 나 탐구 DB 가
통합에서 빠지면서 동기화 전체가 멈춰 있었다 — 지금은 `SOURCES` 에서 제외해 뒀다.
트랙을 추가·제외할 땐 `gcal_schedule.SOURCES` 한 곳만 고치면 ①②③④ 가 같이 따라간다.

## 커밋하면 안 되는 파일

`.gitignore` 로 막아 뒀지만 알아둘 것:

- `cohort_config_*.json` — **서버 런타임 상태**. 커밋하면 다른 사람 서버 값을 덮어쓴다
- `gcal_calendars.*.json` — 캘린더 id. 지우거나 바꾸면 **캘린더가 새로 또 만들어진다**
- `.env.test`, `client_secret_*.json`, `*credentials*.json` — 자격증명

## 구글 캘린더 인증

OAuth(사용자 계정 `aisolopreneurclub@gmail.com`) + refresh token 방식. 토큰은 서버
`.env.test` 의 `GOOGLE_OAUTH_*` 세 개에 있다. 만료되면 **캘린더와 일정은 그대로 남고
동기화만 멈춘다.**

재발급: `python3 scripts/gcal-sync/authorize.py <client_secret.json>` (브라우저 승인 1회)

> ⚠️ GCP 동의 화면이 **"테스트"** 상태면 refresh token 이 **7일 뒤 만료**된다.
> 콘솔 → Google 인증 플랫폼 → 대상 → **앱 게시**로 프로덕션 전환하면 만료가 없어진다.
> 전환 후에는 토큰을 **한 번 더 재발급**해야 한다(테스트 때 받은 토큰은 그대로 만료).

## 되돌리기

- 신청 페이지: `git revert <커밋>` 후 `bash scripts/push-and-deploy.sh --light`
- 공통 일정: 서버 `cohort_config_test.json` 을 복구 (API 가 매 요청 파일을 새로 읽어 재시작 불필요)
- 구글 캘린더: 노션을 되돌리고 `gcal_sync.py --apply` 재실행. 캘린더를 지우지 말 것
  (`gcal_calendars.*.json` 의 id 가 끊기면 새 캘린더가 만들어지고 멤버 구독이 깨진다)
