"""수집 데이터 정제기.

data/raw/crawled_market_news.csv → data/processed/cleaned_market_news.csv

단계:
  1. 컬럼/타입 점검
  2. HTML 태그·엔티티·불필요한 공백 제거
  3. title 결측 / 지나치게 짧은 데이터 처리
  4. 날짜 정규화 (YYYY-MM-DD, 파싱 불가·미래·너무 오래된 날짜는 비우고 date_valid=false)
  5. 완전 중복 → URL 중복 → 제목(+날짜) 중복 제거 (live 우선, 내용이 긴 행 우선)
  6. 저장 + 정제 리포트 출력

실행: python src/cleaner.py [--input PATH] [--output PATH]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse as up
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "data" / "raw" / "crawled_market_news.csv"
OUTPUT_PATH = ROOT / "data" / "processed" / "cleaned_market_news.csv"
REPORT_PATH = ROOT / "data" / "processed" / "cleaning_report.json"

KST = timezone(timedelta(hours=9))
REQUIRED_COLUMNS = ["article_id", "title", "date", "content", "source_url", "source_name", "data_origin"]
TEXT_COLUMNS = ["title", "content", "summary", "source_name", "company_tag", "keywords", "category"]
MIN_TITLE_LEN = 5        # 이보다 짧은 제목은 제거
MIN_CONTENT_LEN = 20     # 이보다 짧은 본문은 제목으로 보강
MIN_DATE = datetime(2000, 1, 1)

TAG_RE = re.compile(r"<[^>]+>")
INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff\xa0]")
SPACE_RE = re.compile(r"\s+")
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|oc$|ref$)")
DATE_PATTERNS = [
    re.compile(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
    re.compile(r"^(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일"),
]


# ---------------------------------------------------------------------------
# 개별 정제 함수
# ---------------------------------------------------------------------------
def clean_text(value: str) -> str:
    """HTML 엔티티 복원 → 태그 제거 → 보이지 않는 문자·연속 공백 정리."""
    if not value:
        return ""
    text = html.unescape(html.unescape(value))  # &amp;lt; 같은 이중 인코딩까지 복원
    text = TAG_RE.sub(" ", text)
    text = INVISIBLE_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def normalize_date(value: str, today: datetime) -> str:
    """여러 날짜 표기를 YYYY-MM-DD 로. 실패하거나 범위를 벗어나면 ''."""
    v = (value or "").strip()
    if not v:
        return ""
    dt = None
    for pattern in DATE_PATTERNS:
        m = pattern.match(v)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return ""  # 2026-02-30 같은 존재하지 않는 날짜
            break
    if dt is None:
        try:  # RFC 822 (RSS pubDate) 형식
            dt = parsedate_to_datetime(v).astimezone(KST).replace(tzinfo=None)
        except (TypeError, ValueError, IndexError):
            return ""
    if dt < MIN_DATE or dt.date() > today.date():
        return ""
    return dt.strftime("%Y-%m-%d")


def normalize_url(url: str) -> str:
    """중복 비교용 URL: 소문자 host, 추적 파라미터·fragment·끝 슬래시 제거, http→https."""
    url = (url or "").strip()
    if not url:
        return ""
    p = up.urlsplit(url)
    query = [(k, v) for k, v in up.parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    path = p.path.rstrip("/") or "/"
    return up.urlunsplit(("https", p.netloc.lower(), path, up.urlencode(query), ""))


def normalize_title(title: str, source_name: str = "") -> str:
    """중복 비교용 제목: 끝의 ' - 매체명' 과 말줄임표·기호·공백 제거, 소문자화."""
    t = title or ""
    if source_name:
        t = re.sub(rf"\s*[-|–]\s*{re.escape(source_name)}\s*$", "", t)
    t = re.sub(r"(…|\.\.\.)$", "", t)
    return re.sub(r"[\W_]+", "", t.lower())


# ---------------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------------
def inspect(df: pd.DataFrame) -> dict:
    info = {
        "rows": len(df),
        "columns": list(df.columns),
        "missing_required_columns": [c for c in REQUIRED_COLUMNS if c not in df.columns],
        "empty_per_column": {c: int((df[c].str.strip() == "").sum()) for c in df.columns},
        "html_noise_rows": int(df[[c for c in TEXT_COLUMNS if c in df]].apply(
            lambda s: s.str.contains(r"<[^>]+>|&[a-zA-Z#0-9]+;", regex=True)).any(axis=1).sum()),
    }
    print(f"[1] 원본 {info['rows']}행 × {len(df.columns)}열 (모든 컬럼 문자열로 로드)")
    print(f"    필수 컬럼 누락: {info['missing_required_columns'] or '없음'}")
    print(f"    빈 값이 있는 컬럼: { {k: v for k, v in info['empty_per_column'].items() if v} or '없음'}")
    print(f"    HTML 노이즈 포함 행: {info['html_noise_rows']}")
    return info


def clean(df: pd.DataFrame, today: datetime) -> tuple[pd.DataFrame, dict]:
    stats: dict[str, int] = {"raw_rows": len(df)}

    # 2. HTML·공백
    before = df[[c for c in TEXT_COLUMNS if c in df]].copy()
    for c in TEXT_COLUMNS:
        if c in df:
            df[c] = df[c].map(clean_text)
    df["source_url"] = df["source_url"].str.strip()
    stats["text_cells_cleaned"] = int((before != df[before.columns]).sum().sum())
    print(f"[2] HTML/공백 정리된 셀: {stats['text_cells_cleaned']}")

    # 3. 결측·짧은 데이터
    empty_title = df["title"] == ""
    short_title = ~empty_title & (df["title"].str.len() < MIN_TITLE_LEN)
    empty_url = df["source_url"] == ""
    stats["removed_empty_title"] = int(empty_title.sum())
    stats["removed_short_title"] = int(short_title.sum())
    stats["removed_empty_url"] = int((empty_url & ~empty_title & ~short_title).sum())
    df = df[~(empty_title | short_title | empty_url)].copy()

    short_content = df["content"].str.len() < MIN_CONTENT_LEN
    df.loc[short_content, "content"] = df.loc[short_content].apply(
        lambda r: r["title"] if not r["content"] else f"{r['title']}. {r['content']}", axis=1)
    empty_summary = df["summary"] == ""
    df.loc[empty_summary, "summary"] = df.loc[empty_summary, "content"].str[:200]
    df.loc[df["source_name"] == "", "source_name"] = "unknown"
    stats["filled_short_content"] = int(short_content.sum())
    stats["filled_empty_summary"] = int(empty_summary.sum())
    print(f"[3] 제거: 빈 title {stats['removed_empty_title']}, 짧은 title(<{MIN_TITLE_LEN}자) "
          f"{stats['removed_short_title']}, 빈 URL {stats['removed_empty_url']} / "
          f"보강: 짧은 content {stats['filled_short_content']}, 빈 summary {stats['filled_empty_summary']}")

    # 4. 날짜
    raw_dates = df["date"].copy()
    df["date"] = df["date"].map(lambda v: normalize_date(v, today))
    df["date_valid"] = (df["date"] != "").map({True: "true", False: "false"})
    stats["date_reformatted"] = int(((raw_dates != df["date"]) & (df["date"] != "")).sum())
    stats["date_invalid"] = int((df["date"] == "").sum())
    print(f"[4] 날짜: 형식 변환 {stats['date_reformatted']}, 무효/누락(비움) {stats['date_invalid']}")

    # 5. 중복 — live 우선, 내용이 긴 행 우선으로 정렬 후 첫 행 유지
    df["_live"] = (df["data_origin"] == "live").astype(int)
    df["_len"] = df["content"].str.len()
    df = df.sort_values(["_live", "_len"], ascending=False, kind="stable")

    content_cols = [c for c in df.columns if c not in ("article_id", "collected_at", "_live", "_len")]
    full_dup = df.duplicated(subset=content_cols)
    stats["removed_full_duplicate"] = int(full_dup.sum())
    df = df[~full_dup]

    df["_url"] = df["source_url"].map(normalize_url)
    url_dup = df.duplicated(subset="_url")
    stats["removed_url_duplicate"] = int(url_dup.sum())
    df = df[~url_dup]

    # 같은 제목이라도 날짜가 다르면 별개 기사로 본다 (정기 발표·템플릿형 제목 보호).
    # 매체만 다른 동일 기사는 보통 같은 날 게재되므로 제목+날짜로 걸러진다.
    df["_title"] = [normalize_title(t, s) for t, s in zip(df["title"], df["source_name"])]
    title_dup = df.duplicated(subset=["_title", "date"])
    stats["removed_title_duplicate"] = int(title_dup.sum())
    df = df[~title_dup]
    print(f"[5] 중복 제거: 완전 {stats['removed_full_duplicate']}, URL {stats['removed_url_duplicate']}, "
          f"제목 {stats['removed_title_duplicate']}")

    # 마무리: 원래 순서 복원, article_id 유일성 보장, has_null 재계산
    df = df.sort_index().drop(columns=["_live", "_len", "_url", "_title"])
    id_dup = df["article_id"].duplicated(keep="first") | (df["article_id"] == "")
    stats["reassigned_article_id"] = int(id_dup.sum())
    if id_dup.any():
        df.loc[id_dup, "article_id"] = [f"CL-{i:04d}" for i in range(1, int(id_dup.sum()) + 1)]
    core = ["title", "date", "content", "source_url", "source_name"]
    df["has_null"] = (df[core] == "").any(axis=1).map({True: "true", False: "false"})
    stats["final_rows"] = len(df)
    return df.reset_index(drop=True), stats


def verify(df: pd.DataFrame) -> list[str]:
    problems = []
    if df["article_id"].duplicated().any():
        problems.append("중복 article_id 존재")
    if df["source_url"].duplicated().any() or df["source_url"].map(normalize_url).duplicated().any():
        problems.append("중복 URL 존재")
    if (df["title"].str.strip() == "").any():
        problems.append("빈 title 존재")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="market news cleaner")
    ap.add_argument("--input", type=Path, default=INPUT_PATH)
    ap.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = ap.parse_args(argv)

    df = pd.read_csv(args.input, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    info = inspect(df)
    if info["missing_required_columns"]:
        print(f"필수 컬럼 누락으로 중단: {info['missing_required_columns']}", file=sys.stderr)
        return 1

    df, stats = clean(df, datetime.now(KST).replace(tzinfo=None))
    problems = verify(df)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False, encoding="utf-8-sig")
    report_path = args.output.parent / REPORT_PATH.name
    report_path.write_text(json.dumps({"input": str(args.input), "output": str(args.output), **stats,
                                       "verification_problems": problems},
                                      ensure_ascii=False, indent=2), encoding="utf-8")

    dup_total = stats["removed_full_duplicate"] + stats["removed_url_duplicate"] + stats["removed_title_duplicate"]
    miss_total = stats["removed_empty_title"] + stats["removed_short_title"] + stats["removed_empty_url"]
    print("\n===== 정제 리포트 =====")
    print(f"원본 건수      : {stats['raw_rows']}")
    print(f"중복 제거 수   : {dup_total} (완전 {stats['removed_full_duplicate']} / URL "
          f"{stats['removed_url_duplicate']} / 제목 {stats['removed_title_duplicate']})")
    print(f"결측 제거 수   : {miss_total} (빈 title {stats['removed_empty_title']} / 짧은 title "
          f"{stats['removed_short_title']} / 빈 URL {stats['removed_empty_url']})")
    print(f"최종 건수      : {stats['final_rows']}")
    print(f"날짜 무효 표시 : {stats['date_invalid']}")
    print(f"검증           : {'통과' if not problems else problems}")
    print(f"저장           : {args.output}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
