"""기업 맞춤형 시장 뉴스 추천기.

data/processed/cleaned_market_news.csv + config/company_profile.yaml
  → data/processed/recommended_market_news.csv (상위 30)

1. 규칙 기반 점수 (0~100)
     keyword 30 · company 30 · competitor 15 · funding 15 · recency 10
2. GEMINI_API_KEY 가 있으면 상위 후보를 Gemini 로 재평가
     기업 관련성 · 사업 중요도 · 대응 필요성 (각 1~5) + 추천 이유
     final = rule 60% + llm 40%
3. Key 가 없거나 호출이 실패하면 규칙 기반 점수·이유만으로 계속 진행

실행: python src/recommender.py [--top 30] [--no-llm]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config" / "company_profile.yaml"
INPUT_PATH = ROOT / "data" / "processed" / "cleaned_market_news.csv"
OUTPUT_PATH = ROOT / "data" / "processed" / "recommended_market_news.csv"
META_PATH = ROOT / "data" / "processed" / "recommendation_meta.json"

KST = timezone(timedelta(hours=9))
WEIGHTS = {"keyword": 30, "company": 30, "competitor": 15, "funding": 15, "recency": 10}
RULE_WEIGHT, LLM_WEIGHT = 0.6, 0.4
LLM_CANDIDATES = 60          # 규칙 점수 상위 N개만 LLM 평가 (비용·시간 제한)
LLM_BATCH = 15
MAX_PER_CATEGORY = 10        # 상위 30 안에서 한 카테고리 최대 개수 (다양성)
# 앞 모델이 과부하(503)·한도(429)·폐기(404)이면 다음 모델로 넘어간다
GEMINI_MODELS = [os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
                 "gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-lite-latest"]
USED_MODELS: set[str] = set()
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Profile 문장을 그대로 매칭하기 어려워 사업 영역 어휘를 펼쳐 둔다 (영문은 arXiv 대응).
COMPANY_TERMS = {
    "비전": 3, "머신비전": 3, "불량": 3, "품질검사": 3, "검사": 2, "품질": 2, "결함": 2,
    "제조": 2, "공장": 2, "스마트팩토리": 2, "스마트공장": 2, "SaaS": 1, "리포트": 1,
    "중소기업": 1, "중견기업": 1, "중소·중견": 1,
    "defect": 3, "inspection": 3, "anomaly": 2, "industrial": 2, "manufacturing": 2,
}
CATEGORY_LABEL = {"market": "시장", "competitor": "경쟁사", "policy": "정책",
                  "funding": "지원사업", "technology": "기술동향"}


# ---------------------------------------------------------------------------
# 규칙 기반 점수
# ---------------------------------------------------------------------------
def has(text: str, kw: str) -> bool:
    if kw == "AI":
        return re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", text) is not None
    return kw.lower() in text.lower()


def rule_score(row: pd.Series, profile: dict, today: datetime) -> dict:
    title, body = row["title"], row["content"]
    text = f"{title} {body}"

    # keyword: 제목 매칭 2점, 본문만 1점, 최대 6점 → 30
    kw_hits = [k for k in profile["interest_keywords"] if has(text, k)]
    kw_raw = sum(2 if has(title, k) else 1 for k in kw_hits)
    keyword = min(kw_raw / 6, 1) * WEIGHTS["keyword"]

    # company: 사업 영역 어휘 가중합, 최대 8점 → 30
    co_hits = [t for t in COMPANY_TERMS if has(text, t)]
    company = min(sum(COMPANY_TERMS[t] for t in co_hits) / 8, 1) * WEIGHTS["company"]

    # competitor: Profile 경쟁사 또는 proxy 태그
    comp_names = [c for c in profile["competitors"] if has(text, c)]
    tag = row.get("company_tag", "")
    competitor = WEIGHTS["competitor"] if (comp_names or tag) else 0

    # funding: 지원사업 키워드 + 지원사업 카테고리
    fund_hits = [k for k in profile["funding_keywords"] if has(text, k)]
    funding = min(len(fund_hits) / 2 + (0.5 if row["category"] == "funding" else 0), 1) * WEIGHTS["funding"]

    # recency: 30일 이내 만점, 180일에서 0
    recency = 0.0
    if row.get("date"):
        try:
            age = (today - datetime.strptime(row["date"], "%Y-%m-%d")).days
            recency = max(0.0, min(1.0, (180 - age) / 150)) * WEIGHTS["recency"]
        except ValueError:
            pass

    total = keyword + company + competitor + funding + recency
    return {
        "keyword_score": round(keyword, 1), "company_score": round(company, 1),
        "competitor_score": round(competitor, 1), "funding_score": round(funding, 1),
        "recency_score": round(recency, 1), "rule_score": round(total, 1),
        "_kw": kw_hits, "_co": co_hits, "_comp": comp_names or ([tag] if tag else []), "_fund": fund_hits,
    }


def rule_reason(s: dict, row: pd.Series) -> str:
    parts = []
    if s["_comp"]:
        parts.append(f"경쟁사 {', '.join(s['_comp'])} 동향")
    if s["_co"]:
        parts.append(f"사업영역 연관({', '.join(s['_co'][:3])})")
    if s["_kw"]:
        parts.append(f"관심 키워드 {', '.join(s['_kw'][:3])}")
    if s["_fund"] or row["category"] == "funding":
        parts.append("지원사업 " + (", ".join(s["_fund"][:2]) if s["_fund"] else "공고"))
    if s["recency_score"] >= WEIGHTS["recency"] * 0.9:
        parts.append("최근 30일 내 발행")
    return " · ".join(parts) or "일반 시장 동향"


# ---------------------------------------------------------------------------
# Gemini 평가 (선택)
# ---------------------------------------------------------------------------
LLM_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "relevance": {"type": "INTEGER"},
            "importance": {"type": "INTEGER"},
            "urgency": {"type": "INTEGER"},
            "reason": {"type": "STRING"},
        },
        "required": ["id", "relevance", "importance", "urgency", "reason"],
    },
}


def build_prompt(profile: dict, items: list[dict]) -> str:
    company = (f"회사: {profile['company_name']} / 사업: {profile['business_area']} / "
               f"제품: {', '.join(profile['products'])} / 타깃: {', '.join(profile['target_market'])} / "
               f"경쟁사: {', '.join(profile['competitors'])}")
    lines = [f"- id={it['article_id']} [{it['category']}] {it['title']} :: {it['content'][:220]}" for it in items]
    return (
        "당신은 제조 AI 스타트업의 시장 분석가다. 아래 회사 관점에서 각 기사를 평가하라.\n"
        f"{company}\n\n"
        "평가 기준 (각 1~5 정수):\n"
        "- relevance: 회사 사업·제품과의 관련성\n"
        "- importance: 회사 사업에 미치는 영향의 크기(시장 기회·위협·자금)\n"
        "- urgency: 회사가 지금 대응(지원 신청·영업·제품 대응)해야 할 필요성\n"
        "- reason: 이 회사에 왜 중요한지 한국어 1~2문장 (60자 내외, 구체적 행동 포함)\n"
        "기사 내용에 없는 사실은 지어내지 말 것.\n\n기사 목록:\n" + "\n".join(lines)
    )


def call_gemini(api_key: str, prompt: str) -> list[dict]:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                             "responseSchema": LLM_SCHEMA},
    }
    last_err: Exception | None = None
    for model in dict.fromkeys(GEMINI_MODELS):
        for attempt in range(2):
            try:
                r = requests.post(GEMINI_URL.format(model=model), headers={"x-goog-api-key": api_key},
                                  json=body, timeout=120)
                if r.status_code == 404:  # 폐기·미제공 모델 → 재시도 없이 다음 모델
                    last_err = RuntimeError(f"{model} HTTP 404")
                    break
                if r.status_code in (429, 500, 503):
                    raise RuntimeError(f"{model} HTTP {r.status_code}")
                r.raise_for_status()
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = next(p["text"] for p in reversed(parts) if p.get("text") and not p.get("thought"))
                USED_MODELS.add(model)
                return json.loads(text)
            except Exception as e:
                last_err = e
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Gemini 호출 실패: {last_err}")


def llm_evaluate(df: pd.DataFrame, profile: dict, api_key: str) -> tuple[pd.DataFrame, dict]:
    # 같은 사건 중복을 뺀 규칙 상위 후보만 평가해 호출을 아낀다
    cand, _ = select_top(df, LLM_CANDIDATES)
    results: dict[str, dict] = {}
    info = {"requested": len(cand), "evaluated": 0, "failed_batches": 0}
    for i in range(0, len(cand), LLM_BATCH):
        batch = cand.iloc[i:i + LLM_BATCH].to_dict("records")
        try:
            for it in call_gemini(api_key, build_prompt(profile, batch)):
                results[str(it.get("id"))] = it
        except Exception as e:  # 배치 하나 실패해도 규칙 점수로 계속
            info["failed_batches"] += 1
            print(f"  ! LLM 배치 {i // LLM_BATCH + 1} 실패 → 규칙 점수 유지: {str(e)[:120]}")
    clamp = lambda v: max(1, min(5, int(v)))
    for idx, row in cand.iterrows():
        it = results.get(row["article_id"])
        if not it:
            continue
        rel, imp, urg = clamp(it["relevance"]), clamp(it["importance"]), clamp(it["urgency"])
        llm = (rel * 0.4 + imp * 0.35 + urg * 0.25 - 1) / 4 * 100   # 1~5 → 0~100
        df.loc[idx, ["llm_relevance", "llm_importance", "llm_urgency"]] = [rel, imp, urg]
        df.loc[idx, "llm_score"] = round(llm, 1)
        df.loc[idx, "final_score"] = round(row["rule_score"] * RULE_WEIGHT + llm * LLM_WEIGHT, 1)
        df.loc[idx, "reason"] = str(it["reason"]).strip() or row["reason"]
        df.loc[idx, "reason_source"] = "llm"
        info["evaluated"] += 1
    return df, info


# ---------------------------------------------------------------------------
# 선정
# ---------------------------------------------------------------------------
def title_bigrams(title: str) -> set[str]:
    t = re.sub(r"[^\w가-힣]", "", title.lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def same_story(a: pd.Series, b: pd.Series, grams: dict) -> bool:
    """매체만 다른 같은 사건: 제목 bigram overlap 이 높거나, 같은 기업 태그 + 중간 overlap."""
    A, B = grams[a.name], grams[b.name]
    if not A or not B:
        return False
    overlap = len(A & B) / min(len(A), len(B))
    same_tag = a["company_tag"] and a["company_tag"] == b["company_tag"]
    return overlap >= 0.45 or (same_tag and overlap >= 0.25)


def select_top(df: pd.DataFrame, n: int) -> tuple[pd.DataFrame, int]:
    """점수순으로 뽑되 같은 사건 중복과 카테고리 편중을 막는다.
    모자라면 ① 카테고리 제한 해제 ② 같은 사건 제한 해제(제목 완전 일치만 제외) 순으로 채운다."""
    grams = {idx: title_bigrams(t) for idx, t in df["title"].items()}
    picked: list = []
    picked_titles: set[str] = set()
    per_cat: dict[str, int] = {}
    skipped_story = 0
    for cap, story_rule in ((MAX_PER_CATEGORY, True), (n, True), (n, False)):
        for idx, row in df.iterrows():
            if len(picked) == n:
                break
            if idx in picked or per_cat.get(row["category"], 0) >= cap:
                continue
            if "".join(sorted(grams[idx])) in picked_titles:
                continue
            if story_rule and any(same_story(row, df.loc[p], grams) for p in picked):
                skipped_story += cap == MAX_PER_CATEGORY
                continue
            picked_titles.add("".join(sorted(grams[idx])))
            picked.append(idx)
            per_cat[row["category"]] = per_cat.get(row["category"], 0) + 1
    return df.loc[picked], skipped_story


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="market news recommender")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--no-llm", action="store_true", help="Key 가 있어도 규칙 기반만 사용")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")
    profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    df = pd.read_csv(INPUT_PATH, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    today = datetime.now(KST).replace(tzinfo=None)
    print(f"[1] 입력 {len(df)}건, 기업: {profile['company_name']}")

    scores = [rule_score(r, profile, today) for _, r in df.iterrows()]
    df["reason"] = [rule_reason(s, r) for s, (_, r) in zip(scores, df.iterrows())]
    sdf = pd.DataFrame(scores, index=df.index).drop(columns=["_kw", "_co", "_comp", "_fund"])
    df = pd.concat([df, sdf], axis=1)
    df["final_score"] = df["rule_score"]
    df["reason_source"] = "rule"
    for c in ("llm_relevance", "llm_importance", "llm_urgency", "llm_score"):
        df[c] = pd.NA
    df = df.sort_values(["rule_score", "date"], ascending=False)
    print(f"[2] 규칙 점수 계산 완료 (최고 {df['rule_score'].max()}, 중앙값 {df['rule_score'].median()})")

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip().strip("'\"")
    llm_info = {"used": False, "reason": ""}
    if args.no_llm:
        llm_info["reason"] = "--no-llm 옵션"
    elif not api_key:
        llm_info["reason"] = "GEMINI_API_KEY 없음"
    else:
        print(f"[3] Gemini 평가: 상위 {LLM_CANDIDATES}건, 모델 후보 {list(dict.fromkeys(GEMINI_MODELS))}")
        df, info = llm_evaluate(df, profile, api_key)
        llm_info.update(info, used=info["evaluated"] > 0,
                        reason="" if info["evaluated"] else "모든 LLM 호출 실패 → 규칙 기반")
        # LLM 평가를 받은 후보가 규칙 점수만 있는 나머지보다 먼저 오도록 (점수 척도 혼합 방지)
        df["_llm"] = (df["reason_source"] == "llm").astype(int)
        df = df.sort_values(["_llm", "final_score", "date"], ascending=False).drop(columns="_llm")
    if not llm_info["used"]:
        print(f"[3] LLM 미사용 ({llm_info['reason']}) → 규칙 기반으로 진행")

    top, skipped_story = select_top(df, args.top)
    top = top.reset_index(drop=True)
    print(f"[4] 같은 사건 중복으로 건너뛴 기사: {skipped_story}건")
    top.insert(0, "rank", range(1, len(top) + 1))
    cols = ["rank", "article_id", "category", "title", "date", "source_name", "source_url",
            "company_tag", "keywords", "final_score", "rule_score", "llm_score",
            "keyword_score", "company_score", "competitor_score", "funding_score", "recency_score",
            "llm_relevance", "llm_importance", "llm_urgency", "reason", "reason_source",
            "summary", "data_origin"]
    top[cols].to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")

    meta = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "input_rows": len(df), "top_n": len(top),
        "weights": WEIGHTS, "rule_weight": RULE_WEIGHT, "llm_weight": LLM_WEIGHT,
        "skipped_same_story": skipped_story, "llm": {**llm_info, "models_used": sorted(USED_MODELS)},
        "model_candidates": list(dict.fromkeys(GEMINI_MODELS)),
    }
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[5] 상위 {len(top)}건 저장: {OUTPUT_PATH.relative_to(ROOT)}")
    print(f"    카테고리: {top['category'].value_counts().to_dict()}")
    print(f"    LLM 사용: {llm_info['used']} {'(평가 ' + str(llm_info.get('evaluated')) + '건)' if llm_info['used'] else ''}")
    print("\n TOP 10")
    for _, r in top.head(10).iterrows():
        print(f" {r['rank']:>2}. [{r['final_score']:>5}] ({CATEGORY_LABEL.get(r['category'], r['category'])}) {r['title'][:60]}")
    missing = int((top["reason"].str.strip() == "").sum() + (top["source_url"].str.strip() == "").sum())
    return 0 if len(top) == args.top and missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
