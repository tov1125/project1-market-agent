# Project 1 Starter Data

이 폴더는 Antigravity Project 1 실습용 시작 데이터입니다.

- `data/fallback/fallback_market_news.csv`: 800행의 합성 fallback 데이터
  - 정상 데이터 750행
  - 의도적 중복 50행
  - 일부 결측치 포함
- `config/company_profile.yaml`: 기업/시장/경쟁사 설정 예시
- `.env.example`: 환경변수 예시
- `.gitignore`: 비밀값 및 로컬 파일 제외 예시

주의: fallback CSV의 기사/출처는 실제 뉴스가 아니라 수업 안정성을 위한 합성 데이터입니다.
실습에서는 먼저 공개 웹/RSS 크롤링을 시도하고, 실패하거나 수집량이 부족할 때만 이 파일을 사용합니다.
