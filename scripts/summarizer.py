"""
카드별 판권사 감수 내용 AI 요약기
도원암귀: Crimson Inferno IP 감수 인텔리전스 허브

동작 방식:
  - living_guideline.json에서 licensor_feedbacks가 있는 카드를 선별
  - 카드별로 Gemini 1회 호출하여 감수 핵심 이슈 요약
  - 결과를 living_guideline.json의 ai_summary 필드에 저장 (증분 업데이트)
  - NotebookLM 소스 문서 자동 재생성

실행: py -3 scripts/summarizer.py [GEMINI_API_KEY]
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

if __name__ == "__main__" and sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDELINE_PATH  = os.path.join(BASE_DIR, "data", "living_guideline.json")
CLAUDE_MD_PATH  = os.path.join(BASE_DIR, "CLAUDE.md")
NLM_PATH        = os.path.join(BASE_DIR, "data", "notebooklm_source.md")
DEBUG_LOG_PATH  = os.path.join(BASE_DIR, "data", "summarizer_debug.log")

# 사용 가능한 모델 순서 (429/404 시 다음 모델로 자동 전환)
# gemini-1.5-flash 제외 — responseMimeType:application/json 미지원으로 평문 반환
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
CALL_DELAY  = 1.0   # 카드 간 호출 간격 (초)

SYSTEM_PROMPT = """당신은 일본 애니메이션 IP '도원암귀: Crimson Inferno' 마케팅 소재 감수 전문가입니다.
판권사가 제시한 피드백을 분석하여 핵심 감수 이슈를 간결하게 정리해주세요.
반드시 JSON 형식만 출력하고 다른 텍스트는 포함하지 마세요."""

USER_PROMPT_TPL = """\
[감수 카드 정보]
카드명: {subject}
소재 유형: {asset_type}
상태: {status}

[판권사 피드백]
{feedbacks}

위 피드백을 분석하여 아래 JSON 형식으로 요약해주세요:
{{
  "issue": "핵심 감수 이슈 (1문장, 40자 이내)",
  "detail": "구체적 수정 요구사항 또는 주요 지적 내용 (2~3문장)",
  "resolved": {resolved}
}}"""


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "issue":    {"type": "string"},
        "detail":   {"type": "string"},
        "resolved": {"type": "boolean"},
    },
    "required": ["issue", "detail", "resolved"],
}


def _call_gemini(api_key: str, model: str, system: str, user: str) -> str:
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": 800,
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    body = json.dumps(payload).encode("utf-8")
    url  = f"{GEMINI_BASE}/{model}:generateContent?key={api_key}"
    req  = urllib.request.Request(url, data=body,
                                   headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # 프롬프트 자체가 차단된 경우
    block_reason = data.get("promptFeedback", {}).get("blockReason", "")
    if block_reason:
        raise RuntimeError(f"BLOCKED:{block_reason}")

    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("EMPTY_RESPONSE: candidates 없음")

    candidate = candidates[0]
    finish = candidate.get("finishReason", "")

    # 안전 필터 또는 기타 비정상 종료
    if finish in ("SAFETY", "RECITATION", "OTHER") or finish.startswith("PROHIBITED"):
        raise RuntimeError(f"FILTERED:{finish}")

    parts = candidate.get("content", {}).get("parts", [])
    if not parts or not parts[0].get("text", "").strip():
        raise RuntimeError(f"EMPTY_PARTS: finishReason={finish}")

    return parts[0]["text"]


def _call_with_fallback(api_key: str, system: str, user: str) -> tuple[str, str]:
    """여러 모델을 순차 시도. 오류 유형별 처리."""
    last_err = None
    filtered_count = 0  # 안전 필터 차단 횟수

    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                text = _call_gemini(api_key, model, system, user)
                return text, model
            except RuntimeError as e:
                err = str(e)
                last_err = f"{model}: {err}"
                if err.startswith("BLOCKED:") or err.startswith("FILTERED:") or err.startswith("EMPTY_PARTS:"):
                    # 안전 필터 차단 — 모든 모델에서 동일하게 차단되므로 즉시 중단
                    filtered_count += 1
                    break
                # 기타 RuntimeError(EMPTY_RESPONSE 등) → 다음 모델 시도
                break
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                try:
                    msg = json.loads(raw).get("error", {}).get("message", raw[:100])
                except Exception:
                    msg = raw[:100]
                last_err = f"HTTP {e.code}: {msg}"

                if e.code == 503:
                    break  # 과부하 → 다음 모델
                if e.code == 429:
                    is_quota = "quota" in last_err.lower() or "exceeded" in last_err.lower()
                    if is_quota:
                        break  # 일일 한도 → 다음 모델
                    if attempt == 0:
                        time.sleep(5)
                        continue  # 분당 RPM → 1회 재시도
                    break
                if e.code not in (404, 503):
                    raise RuntimeError(last_err)
                break  # 404 → 다음 모델

    # 안전 필터로 모든 모델 차단된 경우 → 건너뜀 처리 (에러 아님)
    if filtered_count == len(GEMINI_MODELS):
        raise RuntimeError(f"SKIP:안전 필터 차단 ({last_err})")

    if last_err and ("quota" in last_err.lower() or "exceeded" in last_err.lower()):
        raise RuntimeError(
            f"Gemini 일일 무료 할당량 초과 — 자정(태평양 표준시) 이후 초기화됩니다. "
            f"또는 다른 API 키를 사용해 주세요. (상세: {last_err})"
        )
    raise RuntimeError(f"모든 모델 실패: {last_err}")


def _parse_json_response(raw: str) -> dict:
    """Gemini 응답에서 JSON 추출.
    마크다운 코드블록, 텍스트 서문("Here is..."), trailing comma 처리.
    """
    import re, ast
    text = raw.strip()

    # 마크다운 코드블록 우선 추출
    m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', text)
    if m:
        text = m.group(1).strip()
    else:
        # 텍스트 서문이 있을 때 첫 { ... 마지막 } 구간만 추출
        start = text.find('{')
        end   = text.rfind('}')
        if start != -1 and end > start:
            text = text[start:end + 1]

    def _try(t: str) -> dict | None:
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass
        cleaned = re.sub(r',\s*([}\]])', r'\1', t)  # trailing comma 제거
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        try:
            r = ast.literal_eval(cleaned)
            if isinstance(r, dict):
                return r
        except (ValueError, SyntaxError):
            pass
        return None

    result = _try(text)
    if result is not None:
        return result

    # 모두 실패 — 원본 오류 메시지로 예외 발생
    return json.loads(text)


def summarize_card(api_key: str, item: dict) -> dict | None:
    """카드 1건 요약. {issue, detail, resolved} 반환."""
    feedbacks = item.get("licensor_feedbacks") or []
    if not feedbacks:
        return None

    # 피드백 3건 이하로 압축 (토큰 절약)
    fb_text = "\n".join(f"- {f.strip()[:200]}" for f in feedbacks[:3])
    resolved = item.get("status") == "승인"

    user_msg = USER_PROMPT_TPL.format(
        subject    = item.get("subject", "")[:50],
        asset_type = item.get("asset_type", "불명"),
        status     = item.get("status", ""),
        feedbacks  = fb_text,
        resolved   = str(resolved).lower(),
    )

    try:
        raw, model = _call_with_fallback(api_key, SYSTEM_PROMPT, user_msg)
        parsed = _parse_json_response(raw)
        return {
            "issue":          parsed.get("issue", ""),
            "detail":         parsed.get("detail", ""),
            "resolved":       parsed.get("resolved", resolved),
            "model":          model,
            "feedback_count": len(feedbacks),
            "updated_at":     datetime.now().isoformat(),
        }
    except json.JSONDecodeError as e:
        # 실패 시 원본 응답을 디버그 로그에 기록
        try:
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as dbg:
                dbg.write(f"\n[{datetime.now().isoformat()}] 카드: {item.get('subject','')[:40]}\n")
                dbg.write(f"RAW: {raw}\n")
                dbg.write(f"ERR: {e}\n")
        except Exception:
            pass
        raise RuntimeError(f"JSON 파싱 실패: {str(e)[:60]} | 응답: {raw[:80]}")
    except RuntimeError as e:
        if str(e).startswith("SKIP:"):
            return "SKIPPED"
        raise


def update_guidelines(api_key: str, force: bool = False) -> dict:
    """living_guideline.json에 ai_summary를 증분 추가."""
    with open(GUIDELINE_PATH, "r", encoding="utf-8") as f:
        guidelines = json.load(f)

    targets = [
        g for g in guidelines
        if g.get("licensor_feedbacks") and (
            force or
            not g.get("ai_summary") or
            g.get("ai_summary", {}).get("needs_update")
        )
    ]

    total = len(targets)
    done, failed = 0, 0
    first_error = ""
    deadline = time.time() + 120  # 2분 타임아웃

    for i, item in enumerate(targets, 1):
        if time.time() > deadline:
            print(f"[타임아웃] 2분 경과 — {done}건 완료, 나머지는 다음 실행에서 처리됩니다.")
            break
        subj = item.get("subject", "")[:35]
        print(f"  [{i}/{total}] {subj}...", end=" ", flush=True)
        try:
            summary = summarize_card(api_key, item)
            if summary == "SKIPPED":
                print("건너뜀 (안전 필터 차단)")
            elif summary:
                item["ai_summary"] = summary
                done += 1
                print(f"완료 ({summary['model']})")
            else:
                print("건너뜀 (피드백 없음)")
        except Exception as e:
            failed += 1
            err_msg = str(e)[:120]
            if not first_error:
                first_error = err_msg
            print(f"실패: {err_msg}")
            if "일일 무료 할당량 초과" in err_msg or ("429" in err_msg and "quota" in err_msg.lower()):
                print("[중단] 일일 API 할당량 초과 — 내일 재시도하거나 다른 API 키를 사용하세요.")
                break
        if i < total:
            time.sleep(CALL_DELAY)

    # 저장 (실패가 있어도 성공한 것은 보존)
    with open(GUIDELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(guidelines, f, ensure_ascii=False, indent=2)

    return {"total": total, "done": done, "failed": failed, "first_error": first_error}


def build_notebooklm_source() -> None:
    """NotebookLM 소스 문서 재생성."""
    with open(GUIDELINE_PATH, "r", encoding="utf-8") as f:
        guidelines = json.load(f)

    lines = [
        "# 도원암귀: Crimson Inferno — IP 감수 지식베이스\n",
        f"> 생성일: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n",
    ]

    # 1. IP 규칙
    if os.path.exists(CLAUDE_MD_PATH):
        with open(CLAUDE_MD_PATH, "r", encoding="utf-8") as f:
            lines.append(f.read())
        lines.append("\n\n---\n\n")

    # 2. 카드별 AI 요약 (판권사 피드백 있는 카드)
    summarized = [g for g in guidelines if g.get("ai_summary")]
    if summarized:
        lines.append("## 판권사 감수 이슈 카드별 요약\n\n")
        # 카테고리별 그룹
        by_cat: dict[str, list] = {}
        for g in summarized:
            cat = g.get("category", "기타")
            by_cat.setdefault(cat, []).append(g)

        for cat, items in by_cat.items():
            lines.append(f"### {cat} 소재\n\n")
            for item in items:
                s = item["ai_summary"]
                status_label = "✅ 해결" if s.get("resolved") else "🔴 미해결/반려"
                lines.append(f"**{item['subject'][:50]}** [{status_label}]\n")
                lines.append(f"- 핵심 이슈: {s.get('issue','')}\n")
                lines.append(f"- 상세: {s.get('detail','')}\n\n")

    # 3. 반려 카드 상위 30건 원문
    rejected = [g for g in guidelines if g.get("is_rejection")][:30]
    if rejected:
        lines.append("## 실제 반려/이슈 사례 원문 (상위 30건)\n\n")
        for item in rejected:
            lines.append(f"### {item.get('subject','')}\n")
            lines.append(f"- 카테고리: {item.get('category','')}\n")
            lines.append(f"- 소재 유형: {item.get('asset_type','')}\n")
            for fb in (item.get("licensor_feedbacks") or [])[:3]:
                lines.append(f"  - {fb[:200]}\n")
            lines.append("\n")

    os.makedirs(os.path.dirname(NLM_PATH), exist_ok=True)
    with open(NLM_PATH, "w", encoding="utf-8") as f:
        f.write("".join(lines))

    print(f"[NotebookLM 소스 재생성] {NLM_PATH}")


def main(api_key: str = "", force: bool = False) -> dict:
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[오류] GEMINI_API_KEY 또는 API 키 인자 필요.")
        sys.exit(1)

    print("[카드별 AI 요약 시작]")
    result = update_guidelines(api_key, force)
    print(f"\n[완료] 처리 {result['total']}건 / 성공 {result['done']}건 / 실패 {result['failed']}건")

    build_notebooklm_source()
    return result


if __name__ == "__main__":
    _key   = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("AI") else ""
    _force = "--force" in sys.argv
    main(_key, _force)
