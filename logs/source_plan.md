# Source Plan — project1-market-agent

- 작성일: 2026-09-19
- 기준 Profile: `config/company_profile.yaml` (NovaFactory AI / 제조업 AI 비전 품질검사)
- 검증 방법: 2026-09-19 로컬에서 `requests` + `feedparser` 로 실제 요청해 응답 코드와 항목 수를 측정했다. 로그인이 필요한 Source는 쓰지 않는다.

## 1. 수집 대상 정보 유형

| 유형 | Profile 근거 | 주 Source |
|---|---|---|
| 시장 | interest_keywords, business_area, target_market | S1 Google News RSS |
| 경쟁사 | competitors (+ 실제 기업 proxy) | S1 Google News RSS, fallback CSV |
| 정책 | 스마트공장, 제조 AX | S3 중기부 보도자료, S1 |
| 정부지원 | funding_keywords | S2 기업마당, S1 |
| 기술동향 | products, 생성형 AI, 품질검사 | S4 AI타임스, S5 ZDNet, S7 arXiv |

## 2. 수집 방법 우선순위

`RSS → requests(HTML 파싱) → 공개 API → Playwright(필요할 때만)`

- 점검한 Source는 모두 RSS나 requests로 수집할 수 있었다. 지금은 **Playwright가 필요 없다**.
- Playwright는 JS 렌더링이 필요한 예비 Source(K-Startup 상세 페이지, NIPA)에만 남겨 둔다.

## 3. Source 후보 및 접근 검증 결과

| ID | Source | 유형 | 방법 | 엔드포인트 | 검증 결과 | 예상 수집량(1회) | 채택 |
|---|---|---|---|---|---|---|---|
| S1 | Google News 검색 RSS (ko-KR) | 시장·경쟁사·정책·지원 | RSS | `https://news.google.com/rss/search?q={query}+when:90d&hl=ko&gl=KR&ceid=KR:ko` | 200, 쿼리당 최대 100건 | 20개 쿼리 합계 1,295건, **URL 중복 제거 시 1,190건** | ✅ 주력 |
| S2 | 기업마당 지원사업 공고 | 정부지원 | requests | `https://www.bizinfo.go.kr/web/lay1/bbs/S1T122C128/AS/74/list.do?cpage={n}` | 200, 페이지당 15건, 1~3쪽 모두 서로 다른 항목 | 10쪽 150건 → 키워드 필터 후 약 20~40건 | ✅ |
| S3 | 중소벤처기업부 보도자료 | 정책 | requests | `https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=86` | 200, 페이지당 10건 (예: "경남 제조 AI 센터 개소") | 5쪽 50건 → 관련 약 15~25건 | ✅ |
| S4 | AI타임스 전체기사 RSS | 기술동향 | RSS | `https://www.aitimes.com/rss/allArticle.xml` | 200, 50건 / 키워드 매칭 14건 | 1회 약 14건, 매일 누적 | ✅ |
| S5 | ZDNet Korea RSS | 기술동향·시장 | RSS | `https://feeds.feedburner.com/zdkorea` | 200, 30건 / 키워드 매칭 13건 | 1회 약 13건, 매일 누적 | ✅ |
| S6 | 전자신문 RSS | 시장 | RSS | `https://rss.etnews.com/Section901.xml` | 200, 28건 / 키워드 매칭 1건 | 1회 약 1~5건 | ⚪ 보조 |
| S7 | arXiv API | 기술동향(연구) | 공개 API | `http://export.arxiv.org/api/query?search_query=abs:"defect detection" AND abs:industrial&max_results=100&sortBy=submittedDate` | 200, 100건 (최신 2026-09-07) | 100건 | ✅ |
| S8 | Bing News RSS | 시장 | RSS | `https://www.bing.com/news/search?q={query}&format=rss` | 200, 11건 | 쿼리당 약 10건 | ⚪ 예비 |

### 제외하거나 보류한 Source

| Source | 결과 | 조치 |
|---|---|---|
| 정책브리핑 korea.kr RSS (`/rss/policy.xml`, `/rss/dept_mss.xml` 등) | 404 | 제외. 정책은 S3와 S1로 대체 |
| 기업마당 RSS (`/uss/rss/bizinfoApi.do`) | 200이지만 item 0건 (인증키가 필요한 것으로 보임) | 목록 페이지 requests 수집(S2)으로 대체 |
| GDELT DOC API | 429 (요청 간격 5초 이상 필요) | 예비. 사용할 때는 5초 이상 간격을 두고 1회만 요청 |
| K-Startup 사업공고 | 200, 목록 HTML 확인 | 예비. 상세 페이지가 필요하면 Playwright 사용 |
| data.go.kr API | 서비스키 발급에 회원가입 필요 | 로그인 필요 Source 금지 원칙에 따라 제외 |

## 4. S1 Google News 쿼리 설계 및 측정값 (최근 90일)

| 구분 | 쿼리 → 건수 |
|---|---|
| 관심 키워드 | 제조 AI 100 · 스마트팩토리 100 · 품질검사 100 · 제조 자동화 100 · 제조 클라우드 87 · 제조 AX 100 · 제조 디지털 전환 65 · 제조 생성형 AI 100 · 제조 데이터 100 |
| 지원사업 | 창업지원 지원사업 70 · AI 바우처 지원사업 41 · 스마트공장 지원 100 · R&D 지원사업 100 · 사업화 자금 지원사업 62 |
| 사업영역 | AI 비전 검사 제조 65 · 비전 불량 탐지 5 |
| 경쟁사(Profile) | VisionForge 0 · InspectAI 0 · FactoryMind 0 · QualiBot 0 |
| 경쟁사 proxy(실제 기업) | 마키나락스 100 · 머신비전 AI 검사 42 · 뉴로클 22 · 세이지 AI 검사 14 · 코그넥스 7 · 딥비전 검사 6 |

- 일반어 키워드(AI, 자동화, 클라우드, 데이터 등)는 앞에 "제조"를 붙여 사업 영역과 관련 없는 기사를 줄인다.
- Google News 링크는 `news.google.com/rss/articles/...` 리다이렉트 URL이다. 중복을 제거할 때는 `title + source` 조합을 함께 쓴다.

## 5. 200건 확보 가능성 평가

| Source | 원천 건수 | 관련성 필터 후(보수적) |
|---|---|---|
| S1 Google News | 1,190 (중복 제거 후) | 약 360 (통과율 30% 가정) |
| S2 기업마당 | 150 | 약 20 |
| S3 중기부 | 50 | 약 15 |
| S4 + S5 + S6 | 108 | 약 28 |
| S7 arXiv | 100 | 약 50 |
| **합계** | **약 1,600** | **약 470** |

**판정: 달성 가능.** S1 하나만으로도 보수적 추정치가 200건을 넘는다. 3종 이상의 정보 유형(시장, 정책, 정부지원, 기술동향)을 로그인 없이 확보할 수 있다.

### 리스크와 대응
1. **Profile의 경쟁사 4곳이 가상 기업이라 실제 뉴스가 0건이다.**
   - 경쟁사 유형은 fallback CSV(`company_tag` 173건)로 채운다.
   - 실제 경쟁 동향은 proxy 쿼리(마키나락스, 뉴로클, 세이지, 코그넥스, "머신비전 AI 검사")로 보완한다.
   - proxy 기업을 `competitors`에 넣을지는 별도로 정한다.
2. **Google News 차단 또는 결과 변동이 있을 수 있다.** 쿼리 사이에 1초 이상 간격을 두고 User-Agent를 명시한다. 실패하면 S8 Bing과 fallback CSV로 전환한다.
3. **HTML 구조가 바뀔 수 있다(S2, S3).** 셀렉터(`td.txt_l a`, `td.subject a`)를 설정으로 분리하고, 파싱 결과가 0건이면 경고를 로그에 남긴다.
4. **Fallback 규칙:** 실시간 수집이 필터 후 200건 미만이면 `data/fallback/fallback_market_news.csv`에서 부족분을 채우고, 이때 `data_origin=synthetic_fallback`을 표시한다.

## 6. 수집 실행 계획
1. `config/company_profile.yaml`을 로드해 쿼리 목록을 생성한다 (관심 키워드, 지원사업 키워드, 사업영역, 경쟁사).
2. RSS Source S1, S4~S8을 수집한다. 요청 사이에 1초 대기하고 timeout은 15초로 둔다.
3. requests Source S2와 S3의 1~N 페이지를 수집한다.
4. 공개 API S7(arXiv)을 호출한다. 요청 간격은 3초 이상으로 둔다.
5. 모든 수집 결과를 fallback CSV와 같은 스키마로 정규화하고 `data/raw/`에 저장한다.
6. 중복 제거(URL, 제목+출처)와 키워드 관련성 필터를 적용한 뒤 `data/processed/`에 저장한다. 결과가 200건 미만이면 fallback으로 보충한다.
7. Source별 요청 수, 성공 수, 건수를 `logs/`에 기록한다.
