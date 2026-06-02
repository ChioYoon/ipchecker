"""
Trello JSON -> living_guideline.json 변환 파서
도원암귀: Crimson Inferno IP 검수 솔루션

실행: py -3 scripts/trello_parser.py [trello_json_path]
기본 입력: data/trello_export.json
기본 출력: data/living_guideline.json
"""

import json
import os
import sys
import re
import shutil
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archive")

DEFAULT_INPUT = os.path.join(DATA_DIR, "trello_export.json")
DEFAULT_OUTPUT = os.path.join(DATA_DIR, "living_guideline.json")

# Windows 콘솔 UTF-8 출력 (직접 실행 시에만)
if __name__ == "__main__" and sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── 리스트(컬럼) 상태 분류 ──────────────────────────────
REJECTION_LISTS = {"이슈 발생", "폐기"}
PENDING_LISTS   = {"컴투스 감수 요청", "지홀딩스 확인 중", "감수 진행 중", "컴투스 확인 중", "보류"}
APPROVED_LISTS  = {"감수 완료"}

# ── 카테고리 키워드 매핑 ────────────────────────────────
CATEGORY_KEYWORDS = {
    "영상":   ["영상", "동영상", "쇼츠", "shorts", "video", "유튜브", "틱톡"],
    "배너":   ["배너", "banner", "광고"],
    "SNS":    ["sns", "포스팅", "x(트위터)", "x 포스팅", "인스타", "트위터"],
    "이미지": ["이미지", "image", "일러스트", "컷"],
    "기획안": ["기획안", "기획", "기획서", "컨셉"],
    "텍스트": ["텍스트", "문구", "카피", "팝업", "공지"],
    "굿즈":   ["굿즈", "상품", "아이템"],
}

# ── 판권사(리모우) Trello 계정 ID ─────────────────────────
# G-Holdings(@gholdings2) 계정의 모든 댓글을 판권사 피드백으로 간주
LICENSOR_MEMBER_IDS = {
    "6944e1af178da57898d6e140",  # G-Holdings @gholdings2
}


def detect_category(card_name: str, desc: str) -> str:
    combined = (card_name + " " + desc).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw.lower() in combined for kw in keywords):
            return category
    return "기타"


def parse_desc(desc: str) -> dict:
    """desc 텍스트에서 구조화된 필드 추출"""
    fields = {"감수항목": "", "사용용도": "", "언어": "", "코멘트": ""}
    patterns = {
        "감수항목": r"(?:내용|감수\s*항목)[^\w]*[:：]\s*(.+?)(?=\n\d\.|$)",
        "사용용도": r"사용\s*용도[^\w]*[:：]\s*([^\n]+)",
        "언어":     r"언어[^\w]*[:：]\s*(.+?)(?=\n\d\.|$)",
        "코멘트":   r"코멘트[^\w]*[:：]\s*(.+?)(?=\n\d\.|$)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, desc, re.IGNORECASE | re.DOTALL)
        if m:
            fields[key] = m.group(1).strip()[:200]
    return fields


def is_licensor_member(action: dict) -> bool:
    """G-Holdings 계정이 작성한 댓글인지 확인"""
    member_id = action.get("memberCreator", {}).get("id", "")
    return member_id in LICENSOR_MEMBER_IDS


def parse_trello_json(file_path: str) -> list:
    print(f"[로드] {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # ── 리스트 ID → 이름 매핑 ────────────────────────────
    lists = {lst["id"]: lst["name"] for lst in data.get("lists", [])}

    # ── 카드별 댓글 인덱싱 ──────────────────────────────
    comments_by_card: dict[str, list[str]] = {}
    licensor_comments_by_card: dict[str, list[str]] = {}
    latest_comment_date_by_card: dict[str, str] = {}
    actions = data.get("actions", [])

    for action in actions:
        if action.get("type") == "commentCard":
            card_id = action.get("data", {}).get("card", {}).get("id", "")
            text    = action.get("data", {}).get("text", "").strip()
            date    = action.get("date", "")
            if card_id and text:
                comments_by_card.setdefault(card_id, []).append(text)
                # 카드별 최신 댓글 날짜 추적
                if date > latest_comment_date_by_card.get(card_id, ""):
                    latest_comment_date_by_card[card_id] = date
                if is_licensor_member(action):
                    licensor_comments_by_card.setdefault(card_id, []).append(text)

    # ── 카드 파싱 ────────────────────────────────────────
    parsed_rules = []
    for card in data.get("cards", []):
        if card.get("closed"):   # 아카이브된 카드 제외
            continue

        card_id    = card.get("id", "")
        card_name  = card.get("name", "").strip()
        card_desc  = card.get("desc", "").strip()
        list_name  = lists.get(card.get("idList", ""), "알 수 없음")
        labels     = [lbl.get("name", "") for lbl in card.get("labels", [])]
        # Trello 카드 URL (shortUrl 우선, shortLink로 조합)
        short_link = card.get("shortLink", "")
        card_url   = card.get("shortUrl", "") or (
            f"https://trello.com/c/{short_link}" if short_link else ""
        )

        desc_fields = parse_desc(card_desc)
        feedbacks   = comments_by_card.get(card_id, [])
        licensor_fb = licensor_comments_by_card.get(card_id, [])
        category    = detect_category(card_name, card_desc)

        # 상태 분류
        if list_name in REJECTION_LISTS:
            status = "반려"
        elif list_name in APPROVED_LISTS:
            status = "승인"
        elif list_name in PENDING_LISTS:
            status = "진행중"
        else:
            status = "알 수 없음"

        parsed_rules.append({
            "id":                 card_id,
            "subject":            card_name,
            "list":               list_name,
            "status":             status,
            "category":           category,
            "card_url":           card_url,
            "description":        card_desc[:300],
            "asset_type":         desc_fields["감수항목"],
            "purpose":            desc_fields["사용용도"],
            "language":           desc_fields["언어"],
            "memo":               desc_fields["코멘트"],
            "labels":             labels,
            "feedbacks":             feedbacks,
            "licensor_feedbacks":    licensor_fb,
            "latest_comment_date":   latest_comment_date_by_card.get(card_id, ""),
            "last_activity":         card.get("dateLastActivity", ""),
            "ai_summary":            None,   # summarizer.py가 채움
            "is_rejection":          status == "반려" or bool(licensor_fb),
            "feedback_count":     len(feedbacks),
        })

    # 반려/이슈 우선, 피드백 많은 순 정렬
    parsed_rules.sort(key=lambda x: (0 if x["is_rejection"] else 1, -x["feedback_count"]))

    return parsed_rules, len(actions)


def save_guideline(rules: list, output_path: str) -> None:
    # 재동기화 시 기존 ai_summary 보존 + 변경 감지 → needs_update 플래그
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                existing = {item["id"]: item for item in json.load(f)}
            for rule in rules:
                prev_summary = existing.get(rule["id"], {}).get("ai_summary")
                if not prev_summary:
                    continue

                rule["ai_summary"] = prev_summary

                curr_fb  = len(rule.get("licensor_feedbacks", []))
                # feedback_count 없으면 현재와 동일로 간주 (최초 플래그 폭발 방지)
                prev_fb  = prev_summary.get("feedback_count", curr_fb)
                curr_res = rule.get("status") == "승인"
                prev_res = prev_summary.get("resolved")

                fb_changed     = curr_fb > prev_fb
                status_changed = (prev_res is not None) and (curr_res != prev_res)

                if fb_changed or status_changed:
                    rule["ai_summary"]["needs_update"] = True
                    reason = []
                    if fb_changed:
                        reason.append(f"피드백 {prev_fb}→{curr_fb}건")
                    if status_changed:
                        reason.append("상태 변경")
                    rule["ai_summary"]["update_reason"] = ", ".join(reason)
                else:
                    rule["ai_summary"].pop("needs_update", None)
                    rule["ai_summary"].pop("update_reason", None)
        except Exception:
            pass

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2, ensure_ascii=False)


def archive_previous(output_path: str) -> None:
    """이전 living_guideline.json을 날짜 스탬프로 보관"""
    if not os.path.exists(output_path):
        return
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = os.path.join(ARCHIVE_DIR, f"living_guideline_{stamp}.json")
    shutil.copy2(output_path, archive_path)
    print(f"[보관] 이전 파일 -> {archive_path}")


def print_summary(rules: list, action_count: int) -> None:
    total = len(rules)
    rejection = sum(1 for r in rules if r["status"] == "반려")
    approved  = sum(1 for r in rules if r["status"] == "승인")
    pending   = sum(1 for r in rules if r["status"] == "진행중")

    cat_stats: dict[str, int] = {}
    for r in rules:
        cat_stats[r["category"]] = cat_stats.get(r["category"], 0) + 1

    print(f"\n[완료] 총 {total}건 파싱")
    print(f"  반려/이슈: {rejection}건 | 승인: {approved}건 | 진행중: {pending}건")
    print("  카테고리별:")
    for cat, cnt in sorted(cat_stats.items(), key=lambda x: -x[1]):
        print(f"    [{cat}] {cnt}건")

    # API 동기화(no_action_limit=True)가 아닌 수동 JSON 내보내기일 때만 경고
    with open(input_path, encoding="utf-8") as _f:
        _meta = json.load(_f).get("_meta", {})
    if action_count >= 1000 and not _meta.get("no_action_limit"):
        print()
        print("[!] 액션이 1,000건으로 표시됩니다.")
        print("    Trello JSON 내보내기는 최근 1,000건의 액션만 포함합니다.")
        print("    오래된 댓글 피드백은 누락되었을 수 있습니다.")
        print("    -> Trello API 동기화 사용을 권장합니다.")


def main():
    # 인자로 파일명 지정 가능: py -3 scripts/trello_parser.py "data/파일명.json"
    input_path  = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_INPUT
    output_path = DEFAULT_OUTPUT

    # 절대 경로가 아니면 BASE_DIR 기준으로 처리
    if not os.path.isabs(input_path):
        input_path = os.path.join(BASE_DIR, input_path)

    if not os.path.exists(input_path):
        print(f"[오류] 파일 없음: {input_path}")
        sys.exit(1)

    archive_previous(output_path)
    rules, action_count = parse_trello_json(input_path)
    save_guideline(rules, output_path)
    print_summary(rules, action_count)
    print(f"\n[저장] {output_path}")
    print("[다음] py -3 server.py 를 실행하여 포털을 시작하세요.")


if __name__ == "__main__":
    main()
