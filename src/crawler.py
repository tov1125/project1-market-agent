"""시장·경쟁사·정책·지원사업·기술동향 수집기.

logs/source_plan.md 기준:
  1차 Source  : Google News RSS, 기업마당(requests), 중기부 보도자료(requests),
                AI타임스/ZDNet/전자신문 RSS, arXiv API
  대체 Source : Bing News RSS, GDELT API  (1차 결과가 목표 미만일 때)
  Fallback    : data/fallback/fallback_market_news.csv (그래도 부족할 때)

실행: python src/crawler.py [--target 200] [--skip-primary] [--skip-alternate]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import urllib.parse as up
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config" / "company_profile.yaml"
FALLBACK_PATH = ROOT / "data" / "fallback" / "fallback_market_news.csv"
OUTPUT_PATH = ROOT / "data" / "raw" / "crawled_market_news.csv"
LOG_PATH = ROOT / "logs" / "crawler.log"
SUMMARY_PATH = ROOT / "logs" / "crawl_summary.json"

KST = timezone(timedelta(hours=9))
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) project1-market-agent/0.1"}
TIMEOUT = 15
RSS_DELAY = 1.0

# fallback CSV 와 동일한 스키마 + 수집 방법 컬럼
COLUMNS = [
    "article_id", "category", "title", "date", "content", "summary",
    "source_url", "source_name", "company_tag", "keywords", "collected_at",
    "has_null", "is_duplicate_seed", "data_origin", "collect_method",
]

# Profile 경쟁사는 가상 기업이라 실제 뉴스가 없음 → 실제 비전검사 기업을 proxy 로 사용.
# Profile 에 competitor_proxies 키가 있으면 그 값을 우선한다.
DEFAULT_COMPETITOR_PROXIES = ["마키나락스", "뉴로클", "세이지 AI 검사", "코그넥스", "머신비전 AI 검사"]
# 단독으로는 너무 넓은 키워드 → Google News 쿼리에 "제조" 를 붙인다.
GENERIC_KEYWORDS = {"AI", "자동화", "클라우드", "데이터", "생성형 AI", "디지털 전환"}
# 일반 RSS/게시판 관련성 필터에 추가로 쓰는 도메인 어휘
DOMAIN_TERMS = ["제조", "공장", "품질", "비전", "불량", "검사", "중소기업", "스타트업"]

log = logging.getLogger("crawler")


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------
@dataclass
class SourceResult:
    name: str
    kind: str          # primary / alternate
    method: str        # rss / requests / api
    ok: bool = False
    fetched: int = 0
    kept: int = 0
    error: str = ""
    error_type: str = ""                       # classify_error() 값
    partial_errors: dict = field(default_factory=dict)  # 일부 쿼리/페이지 실패 유형별 건수


class RssParseError(Exception):
    """응답은 받았지만 RSS/Atom 으로 해석할 수 없음 (HTML 오류 페이지, 깨진 XML 등)."""


class EmptyResultError(Exception):
    """정상 응답이지만 수집 항목이 0건 (셀렉터 변경·검색 결과 없음)."""


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 이 유형이 연속으로 나면 같은 호스트의 남은 쿼리도 실패할 가능성이 높다 → 조기 중단
BREAKER_TYPES = {"timeout", "connection_error", "http_403", "http_429", "http_5xx"}
BREAKER_LIMIT = 3
# 한 Source 안에서 쿼리/페이지 단위로 실패한 오류를 모은다 (run_sources 가 비움)
PARTIAL_ERRORS: dict[str, int] = {}


def classify_error(e: BaseException) -> str:
    """파이프라인 리포트용 오류 유형."""
    if isinstance(e, requests.Timeout):
        return "timeout"
    if isinstance(e, requests.HTTPError) and e.response is not None:
        code = e.response.status_code
        if code == 403:
            return "http_403"
        if code == 429:
            return "http_429"
        return f"http_{code // 100}xx"
    if isinstance(e, (requests.exceptions.MissingSchema, requests.exceptions.InvalidURL,
                      requests.exceptions.InvalidSchema)):
        return "url_error"
    if isinstance(e, requests.ConnectionError):
        return "connection_error"
    if isinstance(e, RssParseError):
        return "rss_parse_error"
    if isinstance(e, EmptyResultError):
        return "empty_result"
    if isinstance(e, ValueError):  # JSON 디코딩 등
        return "parse_error"
    return "other"


def note_partial(e: BaseException) -> None:
    t = classify_error(e)
    PARTIAL_ERRORS[t] = PARTIAL_ERRORS.get(t, 0) + 1


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def http_get(url: str, retries: int = 1, **kw) -> requests.Response:
    """Timeout·연결 오류·429/5xx 는 retries 회 재시도. 403/404·URL 오류는 즉시 실패."""
    timeout = kw.pop("timeout", TIMEOUT)
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, **kw)
            if r.status_code in RETRYABLE_STATUS and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt >= retries or isinstance(e, (requests.exceptions.MissingSchema,
                                                    requests.exceptions.InvalidURL)):
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def parse_feed(content: bytes):
    """RSS/Atom 파싱. 항목이 없고 파서 오류가 있으면 RssParseError."""
    feed = feedparser.parse(content)
    titled = [e for e in feed.entries if e.get("title")]
    # 깨진 XML 은 feedparser 가 빈 항목을 만들기도 하므로 '제목 있는 항목' 기준으로 판정
    if not titled and (feed.bozo or not feed.get("version")):
        reason = getattr(feed, "bozo_exception", None) or "RSS/Atom 형식이 아님"
        raise RssParseError(f"RSS 파싱 실패: {str(reason)[:120]}")
    return feed


def clean_text(html_or_text: str) -> str:
    if not html_or_text:
        return ""
    text = BeautifulSoup(html_or_text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def entry_date(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return time.strftime("%Y-%m-%d", t)
    return ""


def load_profile() -> dict:
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class Matcher:
    """Profile 키워드 기반 태깅·관련성 판정."""

    def __init__(self, profile: dict):
        self.interest = profile.get("interest_keywords", [])
        self.funding = profile.get("funding_keywords", [])
        self.competitors = profile.get("competitors", [])
        self.proxies = profile.get("competitor_proxies") or DEFAULT_COMPETITOR_PROXIES
        self.proxy_names = [p.split()[0] for p in self.proxies if p.split()[0] not in ("머신비전",)]

    @staticmethod
    def _has(text: str, kw: str) -> bool:
        if kw == "AI":  # 영문 단어 경계로 매칭 (예: "SAID" 오탐 방지)
            return re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", text) is not None
        return kw.lower() in text.lower()

    def keywords(self, text: str) -> list[str]:
        return [k for k in self.interest + self.funding if self._has(text, k)]

    def company_tag(self, text: str) -> str:
        for c in self.competitors + self.proxy_names:
            if c.lower() in text.lower():
                return c
        return ""

    def is_relevant(self, text: str) -> bool:
        """일반 피드용: Profile 키워드(AI 제외) 또는 도메인 어휘가 1개 이상."""
        strong = [k for k in self.keywords(text) if k != "AI"]
        return bool(strong or self.company_tag(text) or any(t in text for t in DOMAIN_TERMS))


def make_row(*, category, title, date, content, url, source_name, method, matcher: Matcher) -> dict:
    text = f"{title} {content}"
    summary = content[:200]
    return {
        "article_id": "",
        "category": category,
        "title": title.strip(),
        "date": date,
        "content": content,
        "summary": summary,
        "source_url": url,
        "source_name": source_name,
        "company_tag": matcher.company_tag(text),
        "keywords": "|".join(matcher.keywords(text)),
        "collected_at": now_iso(),
        "has_null": "",
        "is_duplicate_seed": "false",
        "data_origin": "live",
        "collect_method": method,
    }


# ---------------------------------------------------------------------------
# 1차 Source
# ---------------------------------------------------------------------------
def google_news_queries(profile: dict, matcher: Matcher) -> list[tuple[str, str]]:
    """(category, query) 목록."""
    qs: list[tuple[str, str]] = []
    for k in profile.get("interest_keywords", []):
        cat = "technology" if k in ("생성형 AI", "클라우드", "데이터") else "market"
        qs.append((cat, f"제조 {k}" if k in GENERIC_KEYWORDS else k))
    for k in profile.get("funding_keywords", []):
        qs.append(("funding", f"{k} 지원" if k == "스마트공장" else f"{k} 지원사업"))
    qs.append(("policy", "스마트공장 정책"))
    qs.append(("policy", "제조 AI 정부"))
    qs.append(("market", profile.get("business_area", "")))
    for p in profile.get("products", []):
        qs.append(("technology", p))
    for c in profile.get("competitors", []) + matcher.proxies:
        qs.append(("competitor", c))
    return [(c, q) for c, q in qs if q]


def fetch_google_news(profile: dict, matcher: Matcher) -> list[dict]:
    rows: list[dict] = []
    failures = streak = 0
    last_err: Exception | None = None
    queries = google_news_queries(profile, matcher)
    for i, (cat, q) in enumerate(queries):
        if streak >= BREAKER_LIMIT:
            log.warning("google_news: %s 연속 %d회 → 남은 %d개 쿼리 생략",
                        classify_error(last_err), streak, len(queries) - i)
            break
        url = ("https://news.google.com/rss/search?q=" + up.quote(f"{q} when:90d")
               + "&hl=ko&gl=KR&ceid=KR:ko")
        try:
            feed = parse_feed(http_get(url).content)
        except Exception as e:  # 쿼리 하나 실패는 Source 전체 실패가 아님
            failures += 1
            last_err = e
            streak = streak + 1 if classify_error(e) in BREAKER_TYPES else 0
            note_partial(e)
            log.warning("google_news query '%s' 실패(%s): %s", q, classify_error(e), e)
            continue
        streak = 0
        for e in feed.entries:
            src = (e.get("source") or {}).get("title", "") or "Google News"
            title = re.sub(rf"\s+-\s+{re.escape(src)}$", "", e.get("title", "")) if src else e.get("title", "")
            content = clean_text(e.get("summary", ""))
            # Google News summary 는 "제목 + 매체명" 반복이 대부분 → 쿼리 맥락을 덧붙인다
            if not content or content.startswith(title[:20]):
                content = f"{title} (검색어: {q})"
            rows.append(make_row(category=cat, title=title, date=entry_date(e), content=content,
                                 url=e.get("link", ""), source_name=src, method="rss",
                                 matcher=matcher))
        time.sleep(RSS_DELAY)
    log.info("google_news: %d 쿼리 중 %d 실패", len(queries), failures)
    if not rows and last_err is not None:
        raise last_err  # 대표 오류 유형(timeout/403 등)이 그대로 기록되도록
    return rows


def _rss_feed(name: str, url: str, category: str) -> Callable[[dict, Matcher], list[dict]]:
    def _fetch(profile: dict, matcher: Matcher) -> list[dict]:
        feed = parse_feed(http_get(url).content)
        if not feed.entries:
            raise EmptyResultError("RSS 항목 0건")
        rows = []
        for e in feed.entries:
            title = e.get("title", "")
            content = clean_text(e.get("summary", "")) or title
            if not matcher.is_relevant(f"{title} {content}"):
                continue
            rows.append(make_row(category=category, title=title, date=entry_date(e), content=content,
                                 url=e.get("link", ""), source_name=name, method="rss",
                                 matcher=matcher))
        return rows
    return _fetch


def fetch_bizinfo(profile: dict, matcher: Matcher, pages: int = 10) -> list[dict]:
    base = "https://www.bizinfo.go.kr"
    rows = []
    for page in range(1, pages + 1):
        try:
            html = http_get(f"{base}/web/lay1/bbs/S1T122C128/AS/74/list.do?cpage={page}").text
        except Exception as e:
            if page == 1:
                raise
            note_partial(e)
            log.warning("bizinfo page %d 실패(%s) → 이후 페이지 중단", page, classify_error(e))
            break
        trs = [a.find_parent("tr") for a in BeautifulSoup(html, "html.parser").select("td.txt_l a")]
        if not trs:
            if page == 1:
                raise EmptyResultError("목록 셀렉터 td.txt_l a 결과 0건 (HTML 구조 변경 의심)")
            break
        for tr in trs:
            a = tr.select_one("td.txt_l a")
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            title = a.get_text(" ", strip=True)
            field = tds[1] if len(tds) > 1 else ""
            org = tds[5] if len(tds) > 5 else (tds[4] if len(tds) > 4 else "")
            period = tds[3] if len(tds) > 3 else ""
            date = next((t for t in tds if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t)), "")
            content = f"{title}. 분야: {field}. 신청기간: {period}. 수행기관: {org}."
            if not matcher.is_relevant(content) and field not in ("기술", "창업"):
                continue
            rows.append(make_row(category="funding", title=title, date=date, content=content,
                                 url=up.urljoin(base, a.get("href", "")),
                                 source_name="기업마당", method="requests", matcher=matcher))
        time.sleep(RSS_DELAY)
    return rows


def fetch_mss(profile: dict, matcher: Matcher, pages: int = 5) -> list[dict]:
    base = "https://www.mss.go.kr/site/smba/ex/bbs"
    rows = []
    for page in range(1, pages + 1):
        try:
            html = http_get(f"{base}/List.do?cbIdx=86&pageIndex={page}").text
        except Exception as e:
            if page == 1:
                raise
            note_partial(e)
            log.warning("mss page %d 실패(%s) → 이후 페이지 중단", page, classify_error(e))
            break
        trs = BeautifulSoup(html, "html.parser").select("tr[onclick*=doBbsFView]")
        if not trs:
            if page == 1:
                raise EmptyResultError("목록 셀렉터 tr[onclick*=doBbsFView] 결과 0건 (HTML 구조 변경 의심)")
            break
        for tr in trs:
            a = tr.select_one("td.subject a")
            if a is None:
                continue
            title = a.get_text(" ", strip=True)
            m = re.search(r"doBbsFView\('(\d+)','(\d+)'", tr.get("onclick", ""))
            url = f"{base}/View.do?cbIdx={m.group(1)}&bcIdx={m.group(2)}" if m else f"{base}/List.do?cbIdx=86"
            dept = tr.select_one("td.cd_subject")
            dm = re.search(r"(\d{4})[.-](\d{2})[.-](\d{2})", tr.get_text(" ", strip=True))
            date = "-".join(dm.groups()) if dm else ""
            content = f"{title}. 담당: {dept.get_text(strip=True) if dept else ''}."
            if not matcher.is_relevant(content):
                continue
            rows.append(make_row(category="policy", title=title, date=date, content=content, url=url,
                                 source_name="중소벤처기업부 보도자료", method="requests",
                                 matcher=matcher))
        time.sleep(RSS_DELAY)
    return rows


def fetch_arxiv(profile: dict, matcher: Matcher) -> list[dict]:
    q = 'abs:"defect detection" AND abs:industrial'
    url = ("http://export.arxiv.org/api/query?search_query=" + up.quote(q)
           + "&max_results=100&sortBy=submittedDate&sortOrder=descending")
    feed = parse_feed(http_get(url, timeout=30).content)
    if not feed.entries:
        raise EmptyResultError("arXiv 결과 0건")
    rows = []
    for e in feed.entries:
        content = clean_text(e.get("summary", ""))
        row = make_row(category="technology", title=clean_text(e.get("title", "")), date=entry_date(e),
                       content=content, url=e.get("link", ""), source_name="arXiv", method="api",
                       matcher=matcher)
        row["keywords"] = row["keywords"] or "품질검사"
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 대체 Source
# ---------------------------------------------------------------------------
def fetch_bing_news(profile: dict, matcher: Matcher) -> list[dict]:
    rows = []
    queries = ["스마트팩토리", "AI 품질검사", "제조 AI", "스마트공장 지원사업", "머신비전 검사"]
    for q in queries:
        url = "https://www.bing.com/news/search?q=" + up.quote(q) + "&format=rss"
        try:
            feed = parse_feed(http_get(url).content)
        except Exception as e:
            note_partial(e)
            log.warning("bing query '%s' 실패(%s): %s", q, classify_error(e), e)
            continue
        for e in feed.entries:
            title = e.get("title", "")
            content = clean_text(e.get("summary", "")) or title
            rows.append(make_row(category="market", title=title, date=entry_date(e), content=content,
                                 url=e.get("link", ""), source_name=e.get("source", "") or "Bing News",
                                 method="rss", matcher=matcher))
        time.sleep(RSS_DELAY)
    return rows


def fetch_gdelt(profile: dict, matcher: Matcher) -> list[dict]:
    time.sleep(6)  # GDELT: 요청 간 5초 이상
    q = '("smart factory" OR "visual inspection" OR "defect detection") manufacturing'
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=" + up.quote(q)
           + "&mode=artlist&maxrecords=100&format=json&timespan=3months")
    r = http_get(url, timeout=30)
    if not r.text.lstrip().startswith("{"):
        raise ValueError(f"JSON 아님: {r.text[:80]}")
    data = r.json()
    rows = []
    for a in data.get("articles", []):
        d = a.get("seendate", "")
        date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else ""
        rows.append(make_row(category="market", title=a.get("title", ""), date=date,
                             content=a.get("title", ""), url=a.get("url", ""),
                             source_name=a.get("domain", "GDELT"), method="api", matcher=matcher))
    return rows


PRIMARY_SOURCES: list[tuple[str, str, Callable]] = [
    ("google_news_rss", "rss", fetch_google_news),
    ("bizinfo", "requests", fetch_bizinfo),
    ("mss_press", "requests", fetch_mss),
    ("aitimes_rss", "rss", _rss_feed("AI타임스", "https://www.aitimes.com/rss/allArticle.xml", "technology")),
    ("zdnet_rss", "rss", _rss_feed("ZDNet Korea", "https://feeds.feedburner.com/zdkorea", "technology")),
    ("etnews_rss", "rss", _rss_feed("전자신문", "https://rss.etnews.com/Section901.xml", "market")),
    ("arxiv_api", "api", fetch_arxiv),
]
ALTERNATE_SOURCES: list[tuple[str, str, Callable]] = [
    ("bing_news_rss", "rss", fetch_bing_news),
    ("gdelt_api", "api", fetch_gdelt),
]


# ---------------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------------
def run_sources(sources, kind: str, profile: dict, matcher: Matcher,
                rows: list[dict], seen: set, results: list[SourceResult]) -> None:
    for name, method, fn in sources:
        res = SourceResult(name=name, kind=kind, method=method)
        PARTIAL_ERRORS.clear()
        try:
            fetched = fn(profile, matcher)
            res.fetched = len(fetched)
            res.kept = add_unique(rows, fetched, seen)
            res.ok = True
            log.info("[%s] %s 성공: 수집 %d / 신규 %d", kind, name, res.fetched, res.kept)
        except Exception as e:  # Source 하나 실패해도 전체 작업은 계속
            res.error = f"{type(e).__name__}: {e}"[:300]
            res.error_type = classify_error(e)
            log.error("[%s] %s 실패(%s): %s", kind, name, res.error_type, res.error)
        res.partial_errors = dict(PARTIAL_ERRORS)
        results.append(res)


def dedup_keys(row: dict) -> tuple[str, str]:
    title = re.sub(r"\W+", "", row["title"].lower())
    return row["source_url"], title


def add_unique(rows: list[dict], new_rows: list[dict], seen: set) -> int:
    added = 0
    for r in new_rows:
        if not r["title"] or not r["source_url"]:
            continue
        url_key, title_key = dedup_keys(r)
        if url_key in seen or title_key in seen:
            continue
        seen.update((url_key, title_key))
        rows.append(r)
        added += 1
    return added


def load_fallback(n: int) -> list[dict]:
    with open(FALLBACK_PATH, encoding="utf-8-sig", newline="") as f:
        fb = list(csv.DictReader(f))
    out = []
    for r in fb[:n]:
        r = {c: r.get(c, "") for c in COLUMNS}
        r["data_origin"] = "synthetic_fallback"
        r["collect_method"] = "fallback_csv"
        out.append(r)
    return out


def finalize(rows: list[dict]) -> list[dict]:
    live_i = fb_i = 0
    for r in rows:
        if r["data_origin"] == "live":
            live_i += 1
            r["article_id"] = f"LV-{live_i:04d}"
        else:
            fb_i += 1
            r["article_id"] = r["article_id"] or f"FB-{fb_i:04d}"
        core = ("title", "date", "content", "source_url", "source_name")
        r["has_null"] = "true" if any(not str(r.get(c, "")).strip() for c in core) else "false"
    return rows


def save_csv(rows: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def _error_type_counts(results: list[SourceResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        if r.error_type:
            counts[r.error_type] = counts.get(r.error_type, 0) + 1
        for t, n in r.partial_errors.items():
            counts[t] = counts.get(t, 0) + n
    return counts


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="market/competitor/policy crawler")
    ap.add_argument("--target", type=int, default=200, help="최소 확보 건수 (기본 200)")
    ap.add_argument("--skip-primary", action="store_true", help="1차 Source 생략 (fallback 경로 테스트용)")
    ap.add_argument("--skip-alternate", action="store_true", help="대체 Source 생략")
    args = ap.parse_args(argv)

    setup_logging()
    started = now_iso()
    profile = load_profile()
    matcher = Matcher(profile)
    log.info("=== crawl 시작: %s (target=%d) ===", profile.get("company_name"), args.target)

    rows: list[dict] = []
    seen: set = set()
    results: list[SourceResult] = []

    if not args.skip_primary:
        run_sources(PRIMARY_SOURCES, "primary", profile, matcher, rows, seen, results)
    log.info("1차 수집 후 live %d건", len(rows))

    if len(rows) < args.target and not args.skip_alternate:
        log.info("목표 미달 → 대체 Source 시도")
        run_sources(ALTERNATE_SOURCES, "alternate", profile, matcher, rows, seen, results)
        log.info("대체 수집 후 live %d건", len(rows))

    live_count = len(rows)
    fallback_count = 0
    if live_count < args.target:
        need = args.target - live_count
        fb = load_fallback(need)
        fallback_count = len(fb)
        rows.extend(fb)
        log.warning("live %d건 < %d → fallback %d건 병합", live_count, args.target, fallback_count)

    rows = finalize(rows)
    save_csv(rows)

    summary = {
        "started_at": started,
        "finished_at": now_iso(),
        "target": args.target,
        "total": len(rows),
        "live": live_count,
        "fallback": fallback_count,
        "output": os.path.relpath(OUTPUT_PATH, ROOT),
        "by_category": {c: sum(r["category"] == c for r in rows) for c in sorted({r["category"] for r in rows})},
        "sources": [vars(r) for r in results],
        "failed_sources": [r.name for r in results if not r.ok],
        "error_types": _error_type_counts(results),
        "below_target_before_fallback": live_count < args.target,
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("=== 완료: total %d (live %d / fallback %d), 실패 Source: %s ===",
             len(rows), live_count, fallback_count, summary["failed_sources"] or "없음")
    return 0 if len(rows) >= args.target else 1


if __name__ == "__main__":
    sys.exit(main())
