"""
Trello API 자동 수집 스크립트
도원암귀: Crimson Inferno IP 검수 솔루션 — Phase 3

실행: py -3 scripts/trello_fetch.py
환경변수: TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID

기존 JSON 내보내기와의 차이:
  - 1,000건 액션 제한 없음 (카드별 전체 댓글 개별 조회)
  - 매번 최신 상태를 API로 직접 수집
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

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_RAW = os.path.join(BASE_DIR, "data", "trello_export.json")

TRELLO_BASE = "https://api.trello.com/1"
RATE_DELAY  = 0.12   # 100 req/10s 안전 여유


def trello_get(path: str, api_key: str, token: str, params: dict = None) -> dict | list:
    q = f"key={api_key}&token={token}"
    if params:
        q += "&" + "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{TRELLO_BASE}{path}?{q}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_board(api_key: str, token: str, board_id: str) -> dict:
    """보드 전체 데이터 수집 (카드별 댓글 무제한)."""
    print(f"[1/4] 보드 기본 정보 조회...")
    board = trello_get(f"/boards/{board_id}", api_key, token,
                       {"fields": "id,name,desc"})

    print(f"[2/4] 리스트 조회...")
    lists = trello_get(f"/boards/{board_id}/lists", api_key, token,
                       {"fields": "id,name,closed"})

    print(f"[3/4] 카드 조회...")
    cards = trello_get(f"/boards/{board_id}/cards", api_key, token, {
        "fields": "id,name,desc,idList,closed,labels,shortLink,shortUrl,dateLastActivity",
        "labels": "all",
    })
    active_cards = [c for c in cards if not c.get("closed")]
    print(f"      활성 카드 {len(active_cards)}건 / 전체 {len(cards)}건")

    print(f"[4/4] 카드별 댓글 수집 (제한 없음)...")
    all_actions = []
    for i, card in enumerate(active_cards, 1):
        if i % 50 == 0 or i == len(active_cards):
            print(f"      {i}/{len(active_cards)} 완료...", end="\r")
        try:
            actions = trello_get(f"/cards/{card['id']}/actions", api_key, token, {
                "filter": "commentCard",
                "limit":  "1000",
            })
            all_actions.extend(actions)
        except Exception:
            pass
        time.sleep(RATE_DELAY)
    print()

    total_comments = sum(1 for a in all_actions if a.get("type") == "commentCard")
    print(f"      댓글 {total_comments}건 수집 완료")

    # 기존 JSON 내보내기 형식으로 조립
    return {
        "id":      board.get("id"),
        "name":    board.get("name"),
        "lists":   lists,
        "cards":   cards,
        "actions": all_actions,
        "_meta": {
            "fetched_at":     datetime.now().isoformat(),
            "card_count":     len(active_cards),
            "comment_count":  total_comments,
            "no_action_limit": True,
        },
    }


def save(data: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main(api_key: str = "", token: str = "", board_id: str = "") -> dict:
    api_key  = api_key  or os.environ.get("TRELLO_API_KEY",  "")
    token    = token    or os.environ.get("TRELLO_TOKEN",    "")
    board_id = board_id or os.environ.get("TRELLO_BOARD_ID", "")

    if not all([api_key, token, board_id]):
        raise RuntimeError("TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID 가 모두 필요합니다.")

    print(f"[Trello 동기화 시작] 보드: {board_id}")
    try:
        data = fetch_board(api_key, token, board_id)
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}: {e.reason}"
        if e.code == 401:
            msg += " — API 키 또는 토큰이 잘못됐습니다."
        elif e.code == 404:
            msg += " — 보드 ID를 확인하세요."
        raise RuntimeError(msg)

    save(data, OUTPUT_RAW)
    print(f"[저장] {OUTPUT_RAW}")

    meta = data.get("_meta", {})
    return {
        "card_count":    meta.get("card_count", 0),
        "comment_count": meta.get("comment_count", 0),
        "fetched_at":    meta.get("fetched_at", ""),
        "output":        OUTPUT_RAW,
    }


if __name__ == "__main__":
    result = main()
    print(f"\n[완료] 카드 {result['card_count']}건 · 댓글 {result['comment_count']}건")
    print("[다음] py -3 scripts/trello_parser.py 를 실행하세요.")
