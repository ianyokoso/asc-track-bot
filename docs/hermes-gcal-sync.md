# Hermes Agent — Slack 명령 → ASC 트랙 캘린더 동기화

Hermes 가 Slack 명령을 받아 노션 '구글 캘린더용 DB' 를 구글 캘린더에 반영하는 워크플로우 정의서.
견적서 연동([`hermes-quotation-from-meeting.md`](../../Joshua-Automation/docs/ai-agents/hermes-quotation-from-meeting.md))과
같은 구조 — **에이전트는 판단만 하고, 실제 작업은 엔드포인트가 한다.**

이 문서는 두 가지로 쓴다:

1. Hermes 의 `SOUL.md` / 스킬 노트에 그대로 발췌해 넣는 **운영 가이드**
2. asc-track-bot 측에서 계약을 보존하기 위한 **API 문서**

계약 변경 시 이 문서를 같이 갱신할 것.

---

## 1. 트리거 (언제 동작하나)

- `@Hermes 캘린더 동기화` / `캘린더 갱신해줘` / `일정 반영해줘`
- `@Hermes 12기 캘린더 만들어줘` — 기수 숫자가 같이 오면 그 값을 쓴다
- 노션 일정을 고쳤다는 언급 + 반영 요청 (`"노션 고쳤어, 반영해줘"`)
- `@Hermes 캘린더 링크` — 구독 링크만 요청

**모호하면 `dryRun: true` 로 먼저 돌려서 무엇이 바뀌는지 보여주고 확인을 받는다.**
견적서와 달리 이 작업은 **캘린더에서 일정을 지우기도** 하므로, 애매할 때 그냥 실행하면 안 된다.

---

## 2. 엔드포인트

```http
POST https://asc-track-bot.vercel.app/api/gcal/sync
Authorization: Bearer ${GCAL_SYNC_TOKEN}
Content-Type: application/json
```

```json
{ "dryRun": false, "cohort": 12, "year": 2026 }
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `dryRun` | 아니오 | `true` 면 아무것도 바꾸지 않고 계획만 돌려준다. 기본 `false` |
| `cohort` | 아니오 | 기수 숫자. 생략하면 서버 기본값(현재 12) |
| `year` | 아니오 | 일정 연도. 생략하면 노션 날짜의 요일로 자동 판별 |

구독 링크만 필요하면 인증 없이:

```http
GET https://asc-track-bot.vercel.app/api/gcal/links
```

---

## 3. 성공 응답

```json
{
  "status": "ok",
  "summary": "ASC 12기 · 2026년 · 반영 완료\n• 공통 일정: 변경 없음 (3건 유지)\n• 빌더 트랙: 추가 1 / 수정 0 / 삭제 0",
  "report": { "cohort": "ASC 12기", "year": 2026, "tracks": { "builder": { "label": "빌더 트랙", "created": 1, "updated": 0, "deleted": 0, "kept": 7 } } },
  "links": { "builder": { "add": "https://calendar.google.com/...", "ics": "https://..." } }
}
```

**Slack 에는 `summary` 를 그대로 붙인다.** 직접 문장을 지어내지 말 것 — 숫자를 틀리게 옮기는 것보다 낫다.

구독 링크를 물어봤을 때만 `links` 를 같이 낸다. 멤버 안내 문구는:

> 자기 트랙 캘린더 + **공통 캘린더** 두 개를 구독하세요.
> 공통(OT·네트워킹·특강)은 트랙 캘린더에 중복해서 넣지 않았습니다.

---

## 4. 실패 응답

| 상황 | 응답 | Slack 에 뭐라고 할 것인가 |
|---|---|---|
| 인증 만료 | `503` + `"reason": "credentials_expired"` | **"구글 인증이 만료됐습니다. 운영자가 재승인해야 합니다. 캘린더와 일정은 그대로 있고 동기화만 멈춘 상태입니다."** 재시도하지 말 것 |
| 토큰 불일치 | `401` | 설정 문제다. 재시도하지 말고 운영자를 호출 |
| 노션 표기 오류 | `500` + 사유 | 사유를 그대로 옮긴다. 예: `요일 불일치: "9/9 (수)" 는 2026년 기준 화요일` → **노션 데이터 오타**이니 고쳐달라고 안내 |

`500` 은 대개 노션 쪽 데이터 문제라 **에이전트가 고칠 수 없다.** 사유를 그대로 전달하는 게 최선이다.

---

## 5. 하지 말 것

- **응답 숫자를 각색하지 말 것.** `summary` 를 그대로 쓴다
- **실패했는데 재시도 반복하지 말 것.** 한 번 실패하면 사유를 알리고 멈춘다
- **`dryRun` 없이 "다 지워줘" 같은 요청을 실행하지 말 것.** 이 엔드포인트는 삭제 API 가 아니다.
  캘린더를 비우려면 노션에서 행을 지우고 동기화해야 하므로, 그런 요청은 운영자에게 넘긴다
- **구독 링크를 외부에 뿌리지 말 것.** ASC 멤버 채널 안에서만

---

## 6. 서버 설정 (운영자용)

서버 `.env.test` 에 아래가 있어야 동작한다. gitignore 라 배포로 안 따라가므로 **직접 넣어야 한다.**

```bash
GCAL_SYNC_TOKEN=<헤르메스와 나눠 갖는 공유 시크릿>
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_REFRESH_TOKEN=...
```

시크릿 생성:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

⚠️ 동의 화면이 '테스트' 상태면 `GOOGLE_OAUTH_REFRESH_TOKEN` 이 **7일마다 만료**된다.
자동화를 붙이기 전에 [`scripts/gcal-sync/README.md`](../scripts/gcal-sync/README.md) 의
'앱 게시' 또는 '서비스 계정' 중 하나로 정리할 것.
