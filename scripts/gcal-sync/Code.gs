/**
 * ASC 트랙 일정 → 구글 캘린더 동기화
 * ─────────────────────────────────────────────────────────────
 * 노션 '구글 캘린더용 DB' 페이지의 인라인 DB들을 읽어서
 * 트랙별 구글 캘린더를 만들고(공개), 일정을 멱등하게 반영한다.
 *
 * 멤버는 트랙 캘린더 하나만 구독하면 트랙 일정 + 공통 일정을 다 받는다.
 *
 * 최초 1회:  setSecrets_() → setup() → syncAll()
 * 이후 갱신:  syncAll()  (노션 고치고 다시 돌리면 됨)
 * 구독 링크:  listLinks()
 *
 * ⚠️ 서비스 > 'Calendar API' (고급 서비스) 를 켜야 동작한다. README 참고.
 */

// ─── 설정 ────────────────────────────────────────────────────
const PROPS = PropertiesService.getScriptProperties();

const NOTION_VERSION = '2022-06-28';
const TIMEZONE = 'Asia/Seoul';
const COHORT_LABEL = 'ASC 11기';

// 이 스크립트가 만든 일정만 골라내는 표식. 손으로 넣은 일정은 건드리지 않는다.
const SYNC_TAG = 'asc-track-cal';

const COMMON_KEY = 'common';

// 노션 '구글 캘린더용 DB' (2f76400e926880428159d8a651a703d3) 안의 인라인 DB들
const SOURCES = [
  { key: 'builder',      label: '빌더 트랙',        dbId: '2f76400e92688085ae02c6b3165f6947' },
  { key: 'sales',        label: '세일즈 실전 트랙',  dbId: '2f76400e9268805c881ee9d772eef5ea' },
  { key: 'ai_agent',     label: 'AI 에이전트 트랙',  dbId: '2f76400e926880868000c05be3250a45' },
  { key: 'design',       label: '디자인 트랙',       dbId: '31a6400e926880668c52c0c2a720c245' },
  { key: 'self_inquiry', label: '나 탐구 트랙',      dbId: '3376400e926881e9a6b6f47ebe0b6c73' },
  { key: COMMON_KEY,     label: '공통 일정',         dbId: '3ba6400e926880349370fd63ef70e35d' },
];

const WEEKDAYS = '일월화수목금토'; // Date.getDay() 인덱스와 맞춤

/** 최초 1회만 실행하고 본문을 다시 주석 처리할 것 (토큰 평문 노출 방지). */
function setSecrets_() {
  // PROPS.setProperty('NOTION_TOKEN', 'ntn_...');
  // Logger.log('NOTION_TOKEN 등록 완료');
}

// ─── 1단계: 캘린더 생성 ──────────────────────────────────────
/**
 * 트랙별 캘린더를 만들고 '링크가 있는 모든 사용자 읽기'로 공개한다.
 * 이미 만든 캘린더가 있으면 건너뛴다(중복 생성 방지).
 */
function setup() {
  SOURCES.filter(s => s.key !== COMMON_KEY).forEach(src => {
    const propKey = calendarPropKey_(src.key);
    const existing = PROPS.getProperty(propKey);
    if (existing) {
      Logger.log(`이미 있음 · ${src.label} → ${existing}`);
      return;
    }
    const created = Calendar.Calendars.insert({
      summary: `${COHORT_LABEL} · ${src.label}`,
      description: `${COHORT_LABEL} ${src.label} 일정입니다. 공통 일정(OT·네트워킹·특강)도 함께 들어 있습니다.\n원본: 노션 '구글 캘린더용 DB'`,
      timeZone: TIMEZONE,
    });
    // 링크를 아는 사람은 누구나 읽기 — 멤버 구독용
    Calendar.Acl.insert({ role: 'reader', scope: { type: 'default' } }, created.id);
    PROPS.setProperty(propKey, created.id);
    Logger.log(`생성 · ${src.label} → ${created.id}`);
  });
  listLinks();
}

// ─── 2단계: 동기화 ───────────────────────────────────────────
/** 노션을 읽어 모든 트랙 캘린더를 최신 상태로 맞춘다. 몇 번 돌려도 결과가 같다. */
function syncAll() {
  const year = resolveYear_();
  Logger.log(`기준 연도: ${year}년`);

  const bySource = {};
  SOURCES.forEach(src => {
    bySource[src.key] = fetchEvents_(src, year);
    Logger.log(`노션 · ${src.label}: ${bySource[src.key].length}건`);
  });

  const commonEvents = bySource[COMMON_KEY] || [];
  const summary = [];

  SOURCES.filter(s => s.key !== COMMON_KEY).forEach(src => {
    const calendarId = PROPS.getProperty(calendarPropKey_(src.key));
    if (!calendarId) {
      Logger.log(`건너뜀 · ${src.label}: 캘린더 없음. setup() 먼저 실행할 것`);
      return;
    }
    const merged = mergeWithCommon_(bySource[src.key], commonEvents);
    const stats = syncCalendar_(calendarId, merged);
    summary.push(`${src.label}: 추가 ${stats.created} / 수정 ${stats.updated} / 삭제 ${stats.deleted} / 유지 ${stats.kept}`);
  });

  Logger.log('\n' + summary.join('\n'));
}

/**
 * 공통 일정을 트랙 일정에 합친다.
 * 오리엔테이션은 트랙 DB 와 공통 DB 양쪽에 적혀 있어 그대로 합치면 두 번 뜬다.
 * 시작·종료가 똑같을 때만 같은 일정으로 보고 공통 쪽을 버린다.
 * 단순히 시간이 겹친다고 버리면 안 된다 — 9/30 외부연사특강(19–21시)과
 * 빌더 4주차 강의(20–22시)처럼 겹치기만 하는 별개 일정이 실제로 있다.
 */
function mergeWithCommon_(trackEvents, commonEvents) {
  const extras = commonEvents.filter(c =>
    !trackEvents.some(t => t.startMs === c.startMs && t.endMs === c.endMs)
  );
  return trackEvents.concat(extras.map(e => Object.assign({}, e, {
    summary: `[공통] ${e.summary}`,
  })));
}

/** 캘린더 하나를 목표 상태로 맞춘다 (노션 page id 기준 upsert + 사라진 건 삭제). */
function syncCalendar_(calendarId, events) {
  const existing = listSyncedEvents_(calendarId);
  const stats = { created: 0, updated: 0, deleted: 0, kept: 0 };
  const seen = {};

  events.forEach(ev => {
    const body = toGoogleEvent_(ev);
    const prev = existing[ev.notionPageId];
    seen[ev.notionPageId] = true;
    if (!prev) {
      Calendar.Events.insert(body, calendarId);
      stats.created++;
    } else if (needsUpdate_(prev, body)) {
      Calendar.Events.update(body, calendarId, prev.id);
      stats.updated++;
    } else {
      stats.kept++;
    }
  });

  Object.keys(existing).forEach(pageId => {
    if (!seen[pageId]) {
      Calendar.Events.remove(calendarId, existing[pageId].id);
      stats.deleted++;
    }
  });

  return stats;
}

/** 이 스크립트가 넣은 일정만 노션 page id → 이벤트로 모은다. */
function listSyncedEvents_(calendarId) {
  const found = {};
  let pageToken = null;
  do {
    const resp = Calendar.Events.list(calendarId, {
      privateExtendedProperty: `syncTag=${SYNC_TAG}`,
      maxResults: 2500,
      showDeleted: false,
      singleEvents: true,
      pageToken: pageToken,
    });
    (resp.items || []).forEach(item => {
      const pageId = item.extendedProperties &&
        item.extendedProperties.private &&
        item.extendedProperties.private.notionPageId;
      if (pageId) found[pageId] = item;
    });
    pageToken = resp.nextPageToken;
  } while (pageToken);
  return found;
}

function toGoogleEvent_(ev) {
  return {
    summary: ev.summary,
    description: ev.description,
    start: { dateTime: ev.startIso, timeZone: TIMEZONE },
    end: { dateTime: ev.endIso, timeZone: TIMEZONE },
    extendedProperties: {
      private: { syncTag: SYNC_TAG, notionPageId: ev.notionPageId },
    },
  };
}

/** 내용이 실제로 달라졌을 때만 update 를 날린다 (API 호출·수정이력 절약). */
function needsUpdate_(prev, body) {
  const prevStart = (prev.start && prev.start.dateTime || '').slice(0, 19);
  const prevEnd = (prev.end && prev.end.dateTime || '').slice(0, 19);
  return prev.summary !== body.summary ||
    (prev.description || '') !== body.description ||
    prevStart !== body.start.dateTime ||
    prevEnd !== body.end.dateTime;
}

// ─── 노션 읽기 ───────────────────────────────────────────────
function fetchEvents_(src, year) {
  const events = [];
  let cursor = null;
  do {
    const payload = { page_size: 100 };
    if (cursor) payload.start_cursor = cursor;
    const resp = UrlFetchApp.fetch(
      `https://api.notion.com/v1/databases/${dashed_(src.dbId)}/query`,
      {
        method: 'post',
        contentType: 'application/json',
        headers: {
          Authorization: `Bearer ${notionToken_()}`,
          'Notion-Version': NOTION_VERSION,
        },
        payload: JSON.stringify(payload),
        muteHttpExceptions: true,
      }
    );
    if (resp.getResponseCode() !== 200) {
      throw new Error(`노션 조회 실패 (${src.label}): ${resp.getContentText().slice(0, 300)}`);
    }
    const data = JSON.parse(resp.getContentText());
    data.results.forEach(page => events.push(toEvent_(page, src, year)));
    cursor = data.has_more ? data.next_cursor : null;
  } while (cursor);

  events.sort((a, b) => a.startMs - b.startMs);
  return events;
}

function toEvent_(page, src, year) {
  const props = page.properties;
  const rawDate = plain_(props['날짜']);
  const rawTime = plain_(props['시간']);
  const content = plain_(props['내용']);
  const attendance = plain_(props['참가옵션']);

  const date = parseDate_(rawDate, year, src.label);
  const range = parseTimeRange_(rawTime, src.label);

  const startIso = isoLocal_(date, range.start);
  const endIso = isoLocal_(date, range.end);
  const notionUrl = `https://www.notion.so/${page.id.replace(/-/g, '')}`;

  const descriptionParts = [content];
  if (attendance) descriptionParts.push(`참가: ${attendance}`);
  descriptionParts.push(`— ${COHORT_LABEL} ${src.label}`, notionUrl);

  return {
    notionPageId: page.id.replace(/-/g, ''),
    summary: firstLine_(content),
    description: descriptionParts.join('\n\n'),
    startIso: startIso,
    endIso: endIso,
    startMs: new Date(startIso + '+09:00').getTime(),
    endMs: new Date(endIso + '+09:00').getTime(),
  };
}

// ─── 표기 해석 ───────────────────────────────────────────────
/**
 * 노션 날짜는 '9/9 (수)' 처럼 연도가 없다.
 * 괄호 요일이 실제로 맞는 연도를 골라 쓴다 — 기수가 바뀌어도 손 볼 필요가 없다.
 */
function resolveYear_() {
  const override = PROPS.getProperty('SCHEDULE_YEAR');
  if (override) return Number(override);

  const samples = [];
  SOURCES.forEach(src => {
    const resp = UrlFetchApp.fetch(
      `https://api.notion.com/v1/databases/${dashed_(src.dbId)}/query`,
      {
        method: 'post',
        contentType: 'application/json',
        headers: {
          Authorization: `Bearer ${notionToken_()}`,
          'Notion-Version': NOTION_VERSION,
        },
        payload: JSON.stringify({ page_size: 20 }),
        muteHttpExceptions: true,
      }
    );
    if (resp.getResponseCode() !== 200) return;
    JSON.parse(resp.getContentText()).results.forEach(p => {
      samples.push(plain_(p.properties['날짜']));
    });
  });

  const thisYear = new Date().getFullYear();
  let best = thisYear, bestScore = -1;
  [thisYear, thisYear + 1, thisYear - 1].forEach(year => {
    let score = 0;
    samples.forEach(raw => {
      const m = raw.match(/(\d{1,2})\s*\/\s*(\d{1,2})\s*\(\s*([월화수목금토일])\s*\)/);
      if (!m) return;
      const d = new Date(year, Number(m[1]) - 1, Number(m[2]));
      if (WEEKDAYS[d.getDay()] === m[3]) score++;
    });
    if (score > bestScore) { bestScore = score; best = year; }
  });
  if (bestScore <= 0) {
    throw new Error('연도를 특정할 수 없음. 스크립트 속성 SCHEDULE_YEAR 를 지정할 것');
  }
  return best;
}

/** '9/9 (수)' → {month, day}. 요일이 어긋나면 조용히 넘기지 않고 멈춘다. */
function parseDate_(raw, year, label) {
  const m = String(raw).match(/(\d{1,2})\s*\/\s*(\d{1,2})\s*(?:\(\s*([월화수목금토일])\s*\))?/);
  if (!m) throw new Error(`날짜 해석 실패 (${label}): "${raw}"`);
  const month = Number(m[1]), day = Number(m[2]), weekday = m[3];
  const d = new Date(year, month - 1, day);
  if (d.getMonth() !== month - 1 || d.getDate() !== day) {
    throw new Error(`없는 날짜 (${label}): "${raw}"`);
  }
  if (weekday && WEEKDAYS[d.getDay()] !== weekday) {
    throw new Error(`요일 불일치 (${label}): "${raw}" 는 ${year}년 기준 ${WEEKDAYS[d.getDay()]}요일`);
  }
  return { year: year, month: month, day: day };
}

/**
 * '8:00-10:00PM' → 20:00~22:00.
 * 이 DB 는 오전/오후를 끝쪽에만 적는 관행이라 앞쪽이 뒤쪽 표기를 물려받는다.
 * '10:00-13:00AM' 처럼 13 이상이면 이미 24시간 표기로 본다.
 */
function parseTimeRange_(raw, label) {
  const text = String(raw).replace(/[~–—]/g, '-').trim();
  const m = text.match(/^(\d{1,2}):(\d{2})\s*(AM|PM)?\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)?$/i);
  if (!m) throw new Error(`시간 해석 실패 (${label}): "${raw}"`);

  const startMer = m[3] ? m[3].toUpperCase() : null;
  const endMer = m[6] ? m[6].toUpperCase() : null;
  const start = { h: to24h_(Number(m[1]), startMer || endMer), m: Number(m[2]) };
  const end = { h: to24h_(Number(m[4]), endMer || startMer), m: Number(m[5]) };

  if (end.h * 60 + end.m <= start.h * 60 + start.m) {
    throw new Error(`종료가 시작보다 빠름 (${label}): "${raw}"`);
  }
  return { start: start, end: end };
}

function to24h_(hour, meridiem) {
  if (hour >= 13) return hour;            // 이미 24시간 표기
  if (meridiem === 'PM' && hour !== 12) return hour + 12;
  if (meridiem === 'AM' && hour === 12) return 0;
  return hour;
}

/** 타임존 변환 없이 'YYYY-MM-DDTHH:MM:SS' 문자열을 만든다 (KST 는 별도 필드로 전달). */
function isoLocal_(date, time) {
  const p = n => String(n).padStart(2, '0');
  return `${date.year}-${p(date.month)}-${p(date.day)}T${p(time.h)}:${p(time.m)}:00`;
}

// ─── 구독 링크 ───────────────────────────────────────────────
/** 멤버에게 뿌릴 구독 링크를 출력한다. */
function listLinks() {
  const lines = ['', '=== 구독 링크 ==='];
  SOURCES.filter(s => s.key !== COMMON_KEY).forEach(src => {
    const id = PROPS.getProperty(calendarPropKey_(src.key));
    if (!id) { lines.push(`${src.label}: (아직 없음 — setup() 실행)`); return; }
    lines.push(
      `\n■ ${src.label}`,
      `  추가: https://calendar.google.com/calendar/u/0/r?cid=${encodeURIComponent(id)}`,
      `  ICS : https://calendar.google.com/calendar/ical/${encodeURIComponent(id)}/public/basic.ics`
    );
  });
  Logger.log(lines.join('\n'));
}

/** 잘못 만들었을 때 되돌리기 — 캘린더 자체를 지운다. 되돌릴 수 없으니 주의. */
function deleteAllCalendars_() {
  SOURCES.filter(s => s.key !== COMMON_KEY).forEach(src => {
    const propKey = calendarPropKey_(src.key);
    const id = PROPS.getProperty(propKey);
    if (!id) return;
    Calendar.Calendars.remove(id);
    PROPS.deleteProperty(propKey);
    Logger.log(`삭제 · ${src.label}`);
  });
}

// ─── 잡동사니 ────────────────────────────────────────────────
function calendarPropKey_(key) { return `CALENDAR_ID_${key.toUpperCase()}`; }

function notionToken_() {
  const token = PROPS.getProperty('NOTION_TOKEN');
  if (!token) throw new Error('NOTION_TOKEN 미설정 — setSecrets_() 먼저 실행할 것');
  return token;
}

function dashed_(id) {
  const i = id.replace(/-/g, '');
  return `${i.slice(0, 8)}-${i.slice(8, 12)}-${i.slice(12, 16)}-${i.slice(16, 20)}-${i.slice(20)}`;
}

function plain_(prop) {
  if (!prop) return '';
  const chunks = prop.title || prop.rich_text;
  if (!chunks) return '';
  return chunks.map(c => c.plain_text || '').join('');
}

function firstLine_(content) {
  const lines = String(content).split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim().replace(/^[•\-\s]+/, '').trim();
    if (line) return line;
  }
  return '(제목 없음)';
}
