# project1-market-agent

기업 Profile(`config/company_profile.yaml`)을 기준으로 시장·경쟁사·지원사업 동향을 수집하고 리포트를 만드는 에이전트 프로젝트.

## 구조

```
config/company_profile.yaml        수집 기준 (기업/제품/시장/경쟁사/키워드)
data/fallback/fallback_market_news.csv  웹/RSS 수집 실패 시 사용하는 합성 데이터 (800행)
data/raw/  data/processed/         수집 원본 / 정제 결과
src/                               수집·정제·리포트 코드
docs/                              리포트 및 문서
logs/                              실행 로그 (*.log 는 git 제외)
.github/workflows/                 GitHub Actions
```

## 시작

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # GEMINI_API_KEY 입력
```

## 데이터 원칙

공개 웹/RSS 크롤링을 먼저 시도하고, 실패하거나 수집량이 부족할 때만 fallback CSV를 사용한다.
fallback CSV는 실제 뉴스가 아닌 수업용 합성 데이터이며 의도적 중복 50행과 일부 결측치를 포함한다.
