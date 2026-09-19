"""GitHub Pages 용 정적 Dashboard 생성기.

입력: data/processed/recommended_market_news.csv, cleaned_market_news.csv,
      recommendation_meta.json, config/company_profile.yaml
출력: docs/index.html (JS 없이 완성된 정적 HTML), docs/report.json, docs/.nojekyll

실행: python src/build_site.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config" / "company_profile.yaml"
PROCESSED = ROOT / "data" / "processed"
RECOMMENDED_PATH = PROCESSED / "recommended_market_news.csv"
CLEANED_PATH = PROCESSED / "cleaned_market_news.csv"
META_PATH = PROCESSED / "recommendation_meta.json"
DOCS = ROOT / "docs"

KST = timezone(timedelta(hours=9))
CATEGORY_LABEL = {"market": "시장", "competitor": "경쟁사", "policy": "정책",
                  "funding": "지원사업", "technology": "기술동향"}
CATEGORY_ORDER = ["market", "competitor", "policy", "funding", "technology"]


def load() -> tuple[dict, pd.DataFrame, pd.DataFrame, dict]:
    profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    rec = pd.read_csv(RECOMMENDED_PATH, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    cleaned = pd.read_csv(CLEANED_PATH, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    meta = json.loads(META_PATH.read_text(encoding="utf-8")) if META_PATH.exists() else {}
    return profile, rec, cleaned, meta


def num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def display_title(title: str, source: str) -> str:
    """Google News 제목 끝에 붙는 ':매체명', ' - 매체명' 꼬리를 표시용으로 제거."""
    if source:
        t = re.sub(rf"\s*[:\-|]\s*[^:\-|]*{re.escape(source)}\s*$", "", title)
        if len(t) >= 10:
            return t
    return title


def build_report(profile: dict, rec: pd.DataFrame, cleaned: pd.DataFrame, meta: dict) -> dict:
    collected = cleaned["category"].value_counts().to_dict()
    recommended = rec["category"].value_counts().to_dict()
    cats = [c for c in CATEGORY_ORDER if c in collected] + sorted(set(collected) - set(CATEGORY_ORDER))
    items = []
    for _, r in rec.iterrows():
        items.append({
            "rank": int(r["rank"]), "article_id": r["article_id"], "category": r["category"],
            "category_label": CATEGORY_LABEL.get(r["category"], r["category"]),
            "title": display_title(r["title"], r["source_name"]), "date": r["date"], "source_name": r["source_name"],
            "source_url": r["source_url"], "company_tag": r["company_tag"],
            "final_score": num(r["final_score"]), "rule_score": num(r["rule_score"]),
            "llm_score": num(r["llm_score"]),
            "llm": {"relevance": num(r["llm_relevance"]), "importance": num(r["llm_importance"]),
                    "urgency": num(r["llm_urgency"])},
            "reason": r["reason"], "reason_source": r["reason_source"], "data_origin": r["data_origin"],
        })
    llm = meta.get("llm", {})
    return {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "company": {k: profile.get(k) for k in ("company_name", "business_area", "products",
                                                 "target_market", "competitors")},
        "totals": {"collected": len(cleaned), "recommended": len(rec),
                   "live": int((cleaned["data_origin"] == "live").sum()),
                   "fallback": int((cleaned["data_origin"] != "live").sum())},
        "llm": {"used": bool(llm.get("used")), "evaluated": llm.get("evaluated", 0),
                "models": llm.get("models_used", []), "note": llm.get("reason", "")},
        "scoring": {"weights": meta.get("weights", {}), "rule_weight": meta.get("rule_weight"),
                    "llm_weight": meta.get("llm_weight")},
        "category_stats": [{"category": c, "label": CATEGORY_LABEL.get(c, c),
                            "collected": int(collected.get(c, 0)),
                            "recommended": int(recommended.get(c, 0))} for c in cats],
        "top10": [it["article_id"] for it in items[:10]],
        "items": items,
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
CSS = """
:root{
  --steel:#E9EDF0; --panel:#F7F9FA; --ink:#17232B; --ink-2:#4A5A65; --rule:#C6CFD6;
  --signal:#F2B705; --signal-ink:#3A2C00; --track:#D5DCE1; --link:#0B5E7E;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --steel:#10181D; --panel:#172229; --ink:#E4EAEE; --ink-2:#9AAAB5; --rule:#2B3942;
    --signal:#F2B705; --signal-ink:#2A2000; --track:#26343C; --link:#6CC3E6; color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --steel:#10181D; --panel:#172229; --ink:#E4EAEE; --ink-2:#9AAAB5; --rule:#2B3942;
  --signal:#F2B705; --signal-ink:#2A2000; --track:#26343C; --link:#6CC3E6; color-scheme:dark;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--steel);color:var(--ink);
  font-family:"Pretendard Variable",Pretendard,-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;
  font-size:16px;line-height:1.6;font-feature-settings:"tnum";word-break:keep-all;overflow-wrap:anywhere}
a{color:var(--link)}
a:focus-visible{outline:3px solid var(--signal);outline-offset:2px;border-radius:2px}
.wrap{max-width:1120px;margin:0 auto;padding:0 16px}

header.top{padding:40px 0 28px;border-bottom:1px solid var(--rule)}
.company{font-size:15px;color:var(--ink-2);margin:0}
h1{font-size:clamp(28px,5vw,44px);line-height:1.15;font-weight:800;letter-spacing:-0.02em;margin:6px 0 18px}
.facts{display:flex;min-width:0;flex-wrap:wrap;gap:8px 28px;margin:0;padding:0;list-style:none;font-size:14px;color:var(--ink-2)}
.facts b{color:var(--ink);font-weight:700;font-size:18px;margin-right:4px}

.layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:40px;padding:32px 0 8px}
@media (max-width:880px){.layout{grid-template-columns:minmax(0,1fr);gap:24px}}
h2{font-size:21px;font-weight:800;letter-spacing:-0.01em;margin:0 0 4px}
.sub{font-size:14px;color:var(--ink-2);margin:0 0 18px}

/* TOP 10: 검사 라인 판독값 */
ol.line{list-style:none;margin:0;padding:0;border-top:2px solid var(--ink)}
ol.line>li{display:grid;grid-template-columns:52px minmax(0,1fr) 112px;gap:0 18px;
  padding:18px 0 20px;border-bottom:1px solid var(--rule)}
.rank{font-size:30px;font-weight:800;line-height:1;color:var(--ink);padding-top:2px}
.item-title{font-size:18px;font-weight:700;line-height:1.4;margin:0 0 6px}
.item-title a{color:var(--ink);text-decoration:none}
.item-title a:hover{text-decoration:underline;text-underline-offset:3px}
.reason{margin:0 0 8px;color:var(--ink);max-width:62ch}
.meta{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px;color:var(--ink-2);margin:0}
.chip{display:inline-block;font-size:12px;font-weight:700;padding:1px 8px;border-radius:3px;
  border:1px solid var(--ink-2);color:var(--ink)}
.chip.synthetic{border-style:dashed;color:var(--ink-2)}
.chip.competitor{background:var(--ink);color:var(--steel);border-color:var(--ink)}
.gauge{text-align:right;white-space:nowrap}
.gauge .score{font-size:26px;font-weight:800;line-height:1}
.gauge .of{font-size:12px;color:var(--ink-2)}
.meter{height:8px;background:var(--track);margin-top:8px;position:relative}
.meter i{position:absolute;inset:0 auto 0 0;background:var(--signal)}
.meter::after{content:"";position:absolute;left:70%;top:-3px;bottom:-3px;width:1px;background:var(--ink-2)}
.llm{font-size:12px;color:var(--ink-2);margin-top:6px;line-height:1.5}
@media (max-width:560px){
  ol.line>li{grid-template-columns:36px minmax(0,1fr);}
  .rank{font-size:22px}
  .gauge{grid-column:2;text-align:left;display:flex;align-items:center;gap:10px;margin-top:10px}
  .gauge .meter{flex:1;margin-top:0}
  .gauge .llm{display:none}
}

/* 카테고리 통계 */
aside section{background:var(--panel);border:1px solid var(--rule);padding:18px 18px 16px;margin-bottom:20px}
.bars{list-style:none;margin:0;padding:0}
.bars li{margin:0 0 14px}
.bars .row{display:flex;justify-content:space-between;font-size:14px;margin-bottom:4px}
.bars .row span:last-child{color:var(--ink-2)}
.bar{height:6px;background:var(--track);position:relative;margin-top:3px}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--ink-2)}
.bar.rec i{background:var(--signal)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--ink-2);margin:4px 0 0}
.legend span::before{content:"";display:inline-block;width:10px;height:10px;margin-right:6px;vertical-align:-1px}
.legend .l1::before{background:var(--ink-2)} .legend .l2::before{background:var(--signal)}
dl.method{margin:0;font-size:14px}
dl.method dt{font-weight:700;margin-top:10px}
dl.method dt:first-child{margin-top:0}
dl.method dd{margin:2px 0 0;color:var(--ink-2)}

/* 11~30 */
.rest{padding:32px 0 8px}
.table-wrap{overflow-x:auto;border-top:2px solid var(--ink)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:10px 10px 10px 0;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-size:13px;color:var(--ink-2);font-weight:600}
td.n{font-weight:800;width:36px}
td.s{font-weight:700;white-space:nowrap;width:56px}
td.c{white-space:nowrap;width:80px}
td a{color:var(--ink);font-weight:600;text-decoration:none}
td a:hover{text-decoration:underline}
td .r{display:block;color:var(--ink-2);font-size:13px;margin-top:2px}
@media (max-width:560px){th.h-src,td.src,th.h-cat,td.c{display:none}}

footer{padding:28px 0 48px;font-size:13px;color:var(--ink-2)}
footer p{margin:0 0 6px;max-width:80ch}
"""


def meter(score: float | None) -> str:
    pct = max(0.0, min(100.0, score or 0.0))
    return f'<div class="meter" role="img" aria-label="점수 {pct:.1f}/100"><i style="width:{pct:.1f}%"></i></div>'


def top_item(it: dict) -> str:
    chip_cls = "chip competitor" if it["category"] == "competitor" or it["company_tag"] else "chip"
    tag = f' <span class="{chip_cls}">{escape(it["company_tag"])}</span>' if it["company_tag"] else ""
    if it["data_origin"] != "live":
        tag += ' <span class="chip synthetic" title="웹 수집 실패 시 사용한 수업용 합성 데이터">합성 데이터</span>'
    llm_line = ""
    if it["reason_source"] == "llm" and it["llm"]["relevance"] is not None:
        l = it["llm"]
        llm_line = (f'<div class="llm" title="Gemini 평가: 관련성/중요도/대응 필요성 (1~5)">'
                    f'관련성 {l["relevance"]:.0f} · 중요도 {l["importance"]:.0f}<br>대응 필요 {l["urgency"]:.0f} <span aria-hidden="true">(5점 척도)</span></div>')
    return f"""
<li>
  <div class="rank">{it["rank"]}</div>
  <div>
    <h3 class="item-title"><a href="{escape(it["source_url"], quote=True)}" target="_blank" rel="noopener">{escape(it["title"])}</a></h3>
    <p class="reason">{escape(it["reason"])}</p>
    <p class="meta"><span class="chip">{escape(it["category_label"])}</span>{tag}
      <span>{escape(it["source_name"])}</span><span>{escape(it["date"] or "날짜 미상")}</span>
      <a href="{escape(it["source_url"], quote=True)}" target="_blank" rel="noopener">원문 보기</a></p>
  </div>
  <div class="gauge">
    <div><span class="score">{(it["final_score"] or 0):.1f}</span> <span class="of">/100</span></div>
    {meter(it["final_score"])}
    {llm_line}
  </div>
</li>"""


def rest_row(it: dict) -> str:
    return f"""<tr>
  <td class="n">{it["rank"]}</td>
  <td><a href="{escape(it["source_url"], quote=True)}" target="_blank" rel="noopener">{escape(it["title"])}</a>
      <span class="r">{escape(it["reason"])}</span></td>
  <td class="c">{escape(it["category_label"])}{"<br><small>합성</small>" if it["data_origin"] != "live" else ""}</td>
  <td class="src">{escape(it["source_name"])}<span class="r">{escape(it["date"])}</span></td>
  <td class="s">{(it["final_score"] or 0):.1f}</td>
</tr>"""


def category_bars(stats: list[dict]) -> str:
    """수집·추천 규모가 달라 건수 대신 각 집합 안의 비율(%)로 분포를 비교한다."""
    tot_c = sum(s["collected"] for s in stats) or 1
    tot_r = sum(s["recommended"] for s in stats) or 1
    rows = []
    for s in stats:
        pc, pr = s["collected"] / tot_c * 100, s["recommended"] / tot_r * 100
        rows.append(f"""<li>
  <div class="row"><span>{escape(s["label"])}</span><span>수집 {s["collected"]} ({pc:.0f}%) / 추천 {s["recommended"]} ({pr:.0f}%)</span></div>
  <div class="bar" role="img" aria-label="{escape(s["label"])} 수집 비율 {pc:.0f}%"><i style="width:{pc:.1f}%"></i></div>
  <div class="bar rec" role="img" aria-label="{escape(s["label"])} 추천 비율 {pr:.0f}%"><i style="width:{pr:.1f}%"></i></div>
</li>""")
    return "\n".join(rows)


def render(report: dict) -> str:
    c, t, llm = report["company"], report["totals"], report["llm"]
    items = report["items"]
    gen = datetime.fromisoformat(report["generated_at"])
    w = report["scoring"]["weights"]
    if llm["used"]:
        llm_fact = f'<li><b>{llm["evaluated"]}</b>건 Gemini 재평가</li>'
        llm_method = (f'규칙 점수 {int((report["scoring"]["rule_weight"] or 0) * 100)}% + Gemini 평가 '
                      f'{int((report["scoring"]["llm_weight"] or 0) * 100)}%. Gemini는 기업 관련성, 사업 중요도, '
                      f'대응 필요성을 1~5로 평가하고 추천 이유를 작성했습니다.'
                      + (f' 모델: {escape(", ".join(llm["models"]))}.' if llm["models"] else ""))
    else:
        llm_fact = "<li>규칙 기반 추천</li>"
        llm_method = f'Gemini를 사용하지 않았습니다 ({escape(llm["note"] or "API Key 없음")}). 추천 이유는 매칭된 키워드로 만들었습니다.'
    weights = ", ".join(f"{k} {v}" for k, v in w.items()) if w else ""
    competitors = ", ".join(c.get("competitors") or [])

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>시장 동향 브리핑</title>
<meta name="description" content="{escape(c["company_name"])} 맞춤 시장·경쟁사·정책·지원사업 뉴스 추천">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pretendard@1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<style>{CSS}</style>
</head>
<body>
<header class="top"><div class="wrap">
  <p class="company">{escape(c["company_name"])} / {escape(c["business_area"])}</p>
  <h1>이번 주 먼저 볼 시장 뉴스 {len(items)}건</h1>
  <ul class="facts">
    <li><b>{t["collected"]:,}</b>건 수집·정제</li>
    <li><b>{t["recommended"]}</b>건 추천</li>
    {llm_fact}
    <li>{gen:%Y년 %m월 %d일 %H:%M} 생성</li>
  </ul>
</div></header>

<main class="wrap">
  <div class="layout">
    <section aria-labelledby="top10">
      <h2 id="top10">TOP 10</h2>
      <p class="sub">점수는 100점 만점입니다. 게이지의 세로선은 70점 기준선입니다.</p>
      <ol class="line">{"".join(top_item(it) for it in items[:10])}
      </ol>
    </section>

    <aside>
      <section aria-labelledby="cats">
        <h2 id="cats">카테고리별 건수</h2>
        <p class="sub">전체 수집 중 비율과 추천 {len(items)}건 중 비율</p>
        <ul class="bars">{category_bars(report["category_stats"])}</ul>
        <p class="legend"><span class="l1">수집</span><span class="l2">추천</span></p>
      </section>
      <section aria-labelledby="how">
        <h2 id="how">점수 계산 방식</h2>
        <dl class="method">
          <dt>규칙 점수</dt><dd>{escape(weights)} (합계 100)</dd>
          <dt>LLM 평가</dt><dd>{llm_method}</dd>
          <dt>선정 규칙</dt><dd>같은 사건을 다룬 기사는 1건만, 카테고리당 최대 10건</dd>
          <dt>경쟁사 기준</dt><dd>{escape(competitors)} (실제 기사 없음 → 비전검사 기업 뉴스로 보완)</dd>
        </dl>
      </section>
    </aside>
  </div>

  <section class="rest" aria-labelledby="rest">
    <h2 id="rest">11~{len(items)}위</h2>
    <p class="sub">제목을 누르면 원문이 새 탭에서 열립니다.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>순위</th><th>제목과 추천 이유</th><th class="h-cat">분류</th><th class="h-src">출처</th><th>점수</th></tr></thead>
      <tbody>{"".join(rest_row(it) for it in items[10:])}</tbody>
    </table></div>
  </section>
</main>

<footer class="wrap">
  <p>원문 {t["live"]:,}건은 공개 RSS, 웹 페이지, API에서 수집했습니다. 합성 fallback 데이터 {t["fallback"]}건이 포함되어 있습니다.</p>
  <p>전체 데이터: <a href="report.json">report.json</a></p>
</footer>
</body>
</html>
"""


def main() -> int:
    profile, rec, cleaned, meta = load()
    report = build_report(profile, rec, cleaned, meta)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (DOCS / "index.html").write_text(render(report), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    missing_reason = sum(1 for it in report["items"] if not it["reason"])
    missing_url = sum(1 for it in report["items"] if not it["source_url"])
    print(f"docs/index.html, docs/report.json 생성 (추천 {len(report['items'])}건, "
          f"이유 누락 {missing_reason}, URL 누락 {missing_url}, LLM {report['llm']['used']})")
    return 0 if report["items"] and not (missing_reason or missing_url) else 1


if __name__ == "__main__":
    sys.exit(main())
