"""수집 → 정제 → 추천 → Dashboard 전체 실행기.

각 단계는 기존 모듈의 main() 을 그대로 호출한다 (로직 중복 구현 없음).
단계 상태:
  SUCCESS  산출물 생성, 이상 없음
  WARNING  산출물은 생성했지만 품질 저하 (Source 실패, fallback 사용, LLM 미사용 등)
  FAILED   단계 실패. 가능한 경우 대체 경로로 다음 단계를 계속한다.

실패 대응:
  수집  Source 별 Timeout/403/URL 오류/RSS 파싱 실패는 crawler 가 격리·분류.
        live 200건 미만이면 crawler 가 대체 Source → fallback 병합.
        crawler 자체가 죽거나 raw CSV 가 없으면 fallback CSV 로 raw 를 만든다.
  정제  실패하면 raw 를 그대로 cleaned 로 복사해 다음 단계를 진행한다.
  추천  실패하면 --no-llm 규칙 기반으로 한 번 더 시도한다.

출력: logs/pipeline.log, logs/pipeline_status.json
실행: python src/run_pipeline.py [--no-llm] [--target 200] [--skip-crawl]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import shutil
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

import build_site  # noqa: E402
import cleaner  # noqa: E402
import crawler  # noqa: E402
import recommender  # noqa: E402

LOG_PATH = ROOT / "logs" / "pipeline.log"
STATUS_PATH = ROOT / "logs" / "pipeline_status.json"
KST = timezone(timedelta(hours=9))

SUCCESS, WARNING, FAILED, SKIPPED = "SUCCESS", "WARNING", "FAILED", "SKIPPED"

log = logging.getLogger("pipeline")


@dataclass
class StageResult:
    stage: str
    status: str = SKIPPED
    seconds: float = 0.0
    exit_code: int | None = None
    output: str = ""
    rows: int | None = None
    notes: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------
class _Tee(io.TextIOBase):
    """모듈 print/logging 출력을 콘솔에 그대로 보여주면서 pipeline.log 에도 남긴다."""

    def __init__(self, stage: str):
        self.stage, self.buf = stage, ""

    def write(self, s: str) -> int:
        sys.__stdout__.write(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                _file_only(f"    [{self.stage}] {line}")
        return len(s)

    def flush(self) -> None:
        sys.__stdout__.flush()


def _file_only(msg: str) -> None:
    for h in log.handlers:
        if isinstance(h, logging.FileHandler):
            h.emit(logging.LogRecord("pipeline", logging.INFO, "", 0, msg, None, None))


def run_module(stage: str, fn: Callable[[], int]) -> tuple[int | None, str]:
    """모듈 main 실행. (exit_code, error) — 예외는 잡아서 문자열로 돌려준다."""
    tee = _Tee(stage)
    try:
        with contextlib.redirect_stdout(tee):
            code = fn()
        return code, ""
    except SystemExit as e:  # argparse 오류 등
        return int(e.code or 0), ""
    except Exception as e:
        _file_only(traceback.format_exc())
        return None, f"{type(e).__name__}: {e}"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def count_rows(path: Path) -> int | None:
    try:
        import pandas as pd
        return len(pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False))
    except Exception:
        return None


def fresh(path: Path, since: float) -> bool:
    return path.exists() and path.stat().st_mtime >= since - 1


# ---------------------------------------------------------------------------
# 단계
# ---------------------------------------------------------------------------
def stage_collect(args) -> StageResult:
    res = StageResult("collect", output=str(crawler.OUTPUT_PATH.relative_to(ROOT)))
    started = time.time()
    argv = ["--target", str(args.target)] + (["--skip-primary", "--skip-alternate"] if args.skip_crawl else [])
    code, err = run_module("collect", lambda: crawler.main(argv))
    res.exit_code, res.error = code, err
    summary = read_json(crawler.SUMMARY_PATH) if fresh(crawler.SUMMARY_PATH, started) else {}

    if err or not fresh(crawler.OUTPUT_PATH, started):
        # crawler 자체 실패 → fallback CSV 로 raw 생성 (crawler 의 fallback 로더 재사용)
        res.status = FAILED
        res.notes.append("crawler 실행 실패 → fallback CSV 로 raw 생성")
        try:
            rows = crawler.finalize(crawler.load_fallback(10**9))
            crawler.save_csv(rows)
            res.notes.append(f"fallback {len(rows)}건으로 대체")
        except Exception as e:
            res.error += f" / fallback 대체도 실패: {e}"
        res.rows = count_rows(crawler.OUTPUT_PATH)
        return res

    res.rows = summary.get("total", count_rows(crawler.OUTPUT_PATH))
    live, fb = summary.get("live", 0), summary.get("fallback", 0)
    failed = summary.get("failed_sources", [])
    errors = summary.get("error_types", {})
    res.notes.append(f"live {live}건 / fallback {fb}건")
    if failed:
        res.notes.append(f"실패 Source: {', '.join(failed)}")
    if errors:
        res.notes.append("오류 유형: " + ", ".join(f"{k} {v}" for k, v in sorted(errors.items())))
    if summary.get("below_target_before_fallback"):
        res.notes.append(f"live 수집 {live}건 < 목표 {args.target}건 → fallback 보충")
    if code != 0:
        res.status = FAILED
        res.notes.append(f"최종 {res.rows}건 < 목표 {args.target}건")
    elif fb or failed or live < args.target:
        res.status = WARNING
    else:
        res.status = SUCCESS
    return res


def stage_clean(args) -> StageResult:
    res = StageResult("clean", output=str(cleaner.OUTPUT_PATH.relative_to(ROOT)))
    started = time.time()
    if not crawler.OUTPUT_PATH.exists():
        res.status, res.error = FAILED, "raw CSV 없음"
        return res
    code, err = run_module("clean", lambda: cleaner.main([]))
    res.exit_code, res.error = code, err
    report = read_json(cleaner.REPORT_PATH) if fresh(cleaner.REPORT_PATH, started) else {}

    if err or not fresh(cleaner.OUTPUT_PATH, started):
        res.status = FAILED
        shutil.copyfile(crawler.OUTPUT_PATH, cleaner.OUTPUT_PATH)
        res.notes.append("정제 실패 → raw 를 그대로 cleaned 로 사용 (중복·노이즈 미처리)")
    else:
        dup = sum(report.get(k, 0) for k in ("removed_full_duplicate", "removed_url_duplicate",
                                             "removed_title_duplicate"))
        miss = sum(report.get(k, 0) for k in ("removed_empty_title", "removed_short_title",
                                              "removed_empty_url"))
        res.notes.append(f"{report.get('raw_rows')} → {report.get('final_rows')}건 "
                         f"(중복 -{dup}, 결측 -{miss}, 날짜 무효 {report.get('date_invalid', 0)})")
        problems = report.get("verification_problems", [])
        if code != 0 or problems:
            res.status = WARNING
            res.notes.append(f"검증 문제: {problems}")
        else:
            res.status = SUCCESS
    res.rows = count_rows(cleaner.OUTPUT_PATH)
    if res.rows is not None and res.rows < args.target:
        res.status = WARNING if res.status == SUCCESS else res.status
        res.notes.append(f"정제 후 {res.rows}건 < 목표 {args.target}건")
    return res


def stage_recommend(args) -> StageResult:
    res = StageResult("recommend", output=str(recommender.OUTPUT_PATH.relative_to(ROOT)))
    started = time.time()
    if not cleaner.OUTPUT_PATH.exists():
        res.status, res.error = FAILED, "cleaned CSV 없음"
        return res
    argv = ["--no-llm"] if args.no_llm else []
    code, err = run_module("recommend", lambda: recommender.main(argv))
    if (err or not fresh(recommender.OUTPUT_PATH, started)) and not args.no_llm:
        res.notes.append(f"1차 실행 실패({err or 'exit ' + str(code)}) → 규칙 기반(--no-llm) 재시도")
        started = time.time()
        code, err = run_module("recommend", lambda: recommender.main(["--no-llm"]))
    res.exit_code, res.error = code, err
    if err or not fresh(recommender.OUTPUT_PATH, started):
        res.status = FAILED
        return res

    meta = read_json(recommender.META_PATH)
    llm = meta.get("llm", {})
    res.rows = count_rows(recommender.OUTPUT_PATH)
    if llm.get("used"):
        res.notes.append(f"LLM 사용: {llm.get('evaluated')}건 평가, 모델 {', '.join(llm.get('models_used', []))}")
    else:
        res.notes.append(f"LLM 미사용: {llm.get('reason') or '규칙 기반'}")
    if llm.get("failed_batches"):
        res.notes.append(f"LLM 배치 실패 {llm['failed_batches']}건 → 해당 기사는 규칙 점수")
    degraded = (not llm.get("used") and not args.no_llm) or llm.get("failed_batches") or any(n.startswith("1차") for n in res.notes)
    if code != 0:
        res.status = WARNING
        res.notes.append("추천 결과 검증 미달 (건수 부족 또는 이유/URL 누락)")
    else:
        res.status = WARNING if degraded else SUCCESS
    return res


def stage_dashboard(args) -> StageResult:
    res = StageResult("dashboard", output="docs/index.html")
    started = time.time()
    if not recommender.OUTPUT_PATH.exists():
        res.status, res.error = FAILED, "recommended CSV 없음"
        return res
    code, err = run_module("dashboard", build_site.main)
    res.exit_code, res.error = code, err
    index = build_site.DOCS / "index.html"
    if err or not fresh(index, started):
        res.status = FAILED
    else:
        report = read_json(build_site.DOCS / "report.json")
        res.rows = len(report.get("items", []))
        fb = report.get("totals", {}).get("fallback", 0)
        res.notes.append(f"index.html {index.stat().st_size // 1024}KB, 추천 {res.rows}건")
        if fb:
            res.notes.append(f"합성 fallback {fb}건 포함 (Dashboard 에 표시)")
        res.status = SUCCESS if code == 0 and not fb else WARNING
    return res


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    sh = logging.StreamHandler(sys.__stdout__)
    sh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="market agent pipeline")
    ap.add_argument("--target", type=int, default=200, help="최소 수집 건수")
    ap.add_argument("--no-llm", action="store_true", help="Gemini 평가 없이 규칙 기반 추천")
    ap.add_argument("--skip-crawl", action="store_true", help="웹 수집 생략, fallback 만 사용 (오프라인)")
    args = ap.parse_args(argv)

    setup_logging()
    t0 = time.time()
    log.info("=" * 64)
    log.info("PIPELINE START %s  target=%d no_llm=%s skip_crawl=%s",
             datetime.now(KST).isoformat(timespec="seconds"), args.target, args.no_llm, args.skip_crawl)

    results: list[StageResult] = []
    for name, fn in [("1/4 수집", stage_collect), ("2/4 정제", stage_clean),
                     ("3/4 추천", stage_recommend), ("4/4 Dashboard", stage_dashboard)]:
        log.info("---- %s ----", name)
        s = time.time()
        try:
            res = fn(args)
        except Exception as e:  # 단계 판정 코드 자체의 예외도 파이프라인을 멈추지 않는다
            res = StageResult(name, status=FAILED, error=f"{type(e).__name__}: {e}")
            _file_only(traceback.format_exc())
        res.seconds = round(time.time() - s, 1)
        results.append(res)
        level = {SUCCESS: logging.INFO, WARNING: logging.WARNING}.get(res.status, logging.ERROR)
        log.log(level, "[%s] %s (%.1fs) rows=%s %s", res.status, res.stage, res.seconds, res.rows,
                ("error=" + res.error) if res.error else "")
        for n in res.notes:
            log.log(level, "    - %s", n)

    outputs = {
        "raw": crawler.OUTPUT_PATH, "cleaned": cleaner.OUTPUT_PATH,
        "recommended": recommender.OUTPUT_PATH, "dashboard": build_site.DOCS / "index.html",
    }
    missing = [k for k, p in outputs.items() if not p.exists()]
    statuses = [r.status for r in results]
    overall = FAILED if missing or FAILED in statuses[-1:] else (WARNING if set(statuses) - {SUCCESS} else SUCCESS)

    log.info("---- 요약 ----")
    for r in results:
        log.info("  %-10s %-8s %s", r.stage, r.status, r.output)
    log.info("  산출물 누락: %s", missing or "없음")
    log.info("PIPELINE END [%s] %.1fs", overall, time.time() - t0)

    STATUS_PATH.write_text(json.dumps({
        "finished_at": datetime.now(KST).isoformat(timespec="seconds"),
        "overall": overall, "seconds": round(time.time() - t0, 1),
        "stages": [asdict(r) for r in results],
        "outputs": {k: {"path": str(p.relative_to(ROOT)), "exists": p.exists()} for k, p in outputs.items()},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if overall == FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
