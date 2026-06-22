# 웹 로그 뷰어 가독성 리팩토링 계획 (2026-06-19)

> 상태: 완료(2026-06-19). 구현·테스트(node 하니스 + 전체 스위트)·브라우저 검증·문서 동기화(SPEC §10·README) 반영됨. 이 문서는 시점 기록으로 동결.

## 목적

`web_viewer.py` 인라인 페이지의 로그 렌더링을 **문자열 암기(whitelist)** 방식에서
**구조적 패턴 인식(score 기반)** 으로 바꾼다. 특정 펌웨어·장비·태그·JSON 키에
종속되지 않고, 색은 장식이 아니라 신호가 되도록 절제한다.

비범위: 백엔드 라우트/데이터 계약 변경 없음(SSE `{ts,text}`, `/api/buffer` 스냅샷,
`/api/status` 포트 배열 유지). 단일 인라인 페이지(오프라인) 불변식 유지.

## 책임 분리 (같은 파일 내부, 순수 함수 묶음 `SViewer`)

순수 블록은 `/* <<<VIEWER-PURE-START>>> */ … <<<VIEWER-PURE-END>>> */` sentinel로
감싸 DOM 비의존으로 두고 node 테스트로 검증한다. DOM glue는 sentinel 밖.

- `escapeHtml(s)` — 모든 원문 출력에 적용(`& < > " '`).
- `cleanCtrl(s)` — 표시용 제어문자 정리(탭 보존).
- `stripAnsi(s)` — CSI/2-char escape 제거.
- `ansiToHtmlSafe(text,{enable})` — run마다 self-contained span. 0/1/2/3/4/7/22-27,
  30-37/90-97 fg, 40-47/100-107 bg, 38;5;n·48;5;n(256), 38;2;r;g;b(truecolor).
  실패 시 `escapeHtml(stripAnsi)` fallback — HTML은 절대 깨지지 않는다.
- `parseLine(raw,{ts,source,count,firstTs,lastTs})` → LineModel(raw/visible/ts/source/…).
- `classifyLine(model)` → `{scores, primary, confidence, badges}`.
  - 구조 축(bar/틴트 대상): error/warning/boot/noise. (+success는 badge·accent만, 틴트 없음)
  - 보조 축(badge만): network/json/timing/duplicate.
  - confidence 낮으면 primary=''(neutral). 모호한 줄에 억지 색 금지.
- `extractTokens(model)` → 선행 tag 블록, payload(JSON) 위치, kv.
- `findPayload(visible)` → 관용 JSON 추출(파싱 성공분만, 잘린 건 null→평문).
- `correlationBadges(obj)` → 이름 패턴 기반 correlation key(id/uid/seq/asn/req…변형).
- `renderPayloadHTML(value,{mode})` / `renderBodyHTML(model,cls,view)` → HTML 문자열.
- `normalizeForRepeat(model,mode)` — exact=visible, norm=숫자·hex·시각·MAC·IP 치환 signature.

## 컬러 정책 (CSS)

- 기본 neutral. 한 줄 최대 1개 약한 background tint(err/warn/noise, int-normal 이상).
- 좌측 2~3px bar + 작은 badge 중심. 성공은 badge/accent만.
- tag: hash 기반 저채도 고정색. JSON key 약하게, value만 약간 선명. true/false/null/number 과장 금지.
- 색 강도 body 클래스: `int-off`(거의 원문) / `int-min`(bar·badge만) /
  `int-normal`(기본=약한 틴트) / `int-vivid`(강조 강화).
- 줄 간격: `rhythm-dense/normal/relaxed`(기존 정의 배선).
- 색 없이도 spacing·badge·divider·indent로 구조가 보이게.

## 설정 패널 / localStorage (기존 키 보존 + 신규)

기존: `sv_port sv_fs sv_ts sv_wrap sv_nav`(유지). 신규(기본값):
`sv_intensity`(normal) `sv_rhythm`(normal) `sv_json`(compact) `sv_fold`(1)
`sv_foldmode`(norm) `sv_ansi`(1) `sv_semantic`(1) `sv_gap`(2) `sv_focus`(0).
없으면 기본값 fallback(=migration). 기존 동작 유지.

## 보존 기능

줄바꿈/글자크기/타임스탬프/gap divider/반복 접기(버퍼 ×N)/ANSI/JSON·kv 데코/
설정 저장/검색·레벨칩(err/warn/boot)/소유권 보드/포트 셀렉터 — 전부 유지, 내부만 정리.

## 멀티 소스

스트림은 포트당 SSE(백엔드 계약). 라인 모델에 `source`를 싣고 다중 포트일 때
소스 badge를 노출, 좌측 포트 보드가 소스 선택 필터 역할. **동시 다중 소스 머지**는
per-port SSE 계약에 묶여 이번 범위 밖(정직히 보고).

## 테스트 (섹션 14 패턴, node 가드)

`tests/test_viewer_logic.py` — 순수 블록 추출→`tests/viewer_logic_harness.cjs`로 검증.
케이스: ANSI 없음/있음/reset 누락/잘못된 시퀀스, 1줄 JSON/prefix+JSON/잘린 JSON,
key=value/key:value, [TAG]/평문, error/warn/success/neutral, 긴 payload, 정확 반복,
숫자만 바뀌는 유사 반복, gap 계산, source 보존, 깨진 문자/제어문자, boot/reset/setup/init,
MAC/IP/URL/UUID/hex. node 없으면 skip(=Python-only `uv run pytest` 깨지지 않음).

## 문서 동기화

- SPEC §10 컬러 우선순위: "ANSI > 틴트"(suppress 모델) → **채널 분리**(ANSI=본문색,
  semantic=구조 bar/badge 공존)로 갱신. README 웹 뷰어 절도 함께.
- 둘 다 도구 목록처럼 중복 서술 사실이므로 함께 갱신.
