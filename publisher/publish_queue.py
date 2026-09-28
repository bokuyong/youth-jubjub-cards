"""청년줍줍 예약 발행기 — GitHub Actions 에서 매일 20:30(KST) 실행.

PC 가 꺼져 있어도 발행되게, PC 에서 검토·승인해 올려둔 대기열을 여기서 게시한다.
이 파일은 jubjub-auto/cloud/ 가 원본이고 youth-jubjub-cards 저장소로 배포된다.
(jubjub-auto 는 비공개라 Actions 가 가져올 수 없어서 표준 라이브러리만 쓰는 독립 스크립트로 둔다)

대기열 구조 (youth-jubjub-cards):
    queue/2026-09-29/
        manifest.json        날짜·파일 해시·마감일·공고 id (PC 에서 승인 시 작성)
        00.jpg ~ NN.jpg      캐러셀 카드 (검토 통과본)
        reel.mp4             (선택) 같은 카드로 만든 릴스
        caption.txt
        first_comment.txt
        published.json       ← 여기서 게시 성공 후 기록

원칙
- **오늘(KST) 폴더만** 게시한다. 놓친 날을 다음 날 올리지 않는다 — 카드에 D-day 가 박혀 있다.
- 파일 해시가 manifest 와 다르면 게시하지 않는다(검토한 파일 = 올라가는 파일).
- 마감일이 지난 공고는 게시하지 않는다.
- 같은 캡션이 최근 36시간 안에 이미 올라가 있으면 게시하지 않는다(재시도 실행·기록 실패 대비).

사용:
    python publish_queue.py                 # 오늘 대기열 게시
    python publish_queue.py --dry-run       # 컨테이너 생성까지만(이미지 URL 검증), 게시 안 함
    python publish_queue.py --date 2026-09-29
    python publish_queue.py --warn-empty    # 내일 대기열이 비었으면 텔레그램 경고
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent          # 저장소 루트
QUEUE = ROOT / "queue"
REPO = os.environ.get("GITHUB_REPOSITORY", "bokuyong/youth-jubjub-cards")
SHA = os.environ.get("GITHUB_SHA", "main")               # 이 실행이 본 커밋 — URL 을 여기에 고정
API_VER = "v21.0"


# ── 외부 호출 ───────────────────────────────────────────────────────────

def _base() -> str:
    if os.environ.get("IG_API_MODE", "instagram") == "facebook":
        return f"https://graph.facebook.com/{API_VER}"
    return f"https://graph.instagram.com/{API_VER}"


def _post(path: str, params: dict, tries: int = 4) -> dict:
    """Graph API POST. 일시 오류(5xx·is_transient)만 재시도한다."""
    data = urllib.parse.urlencode(params).encode("utf-8")
    last = ""
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(f"{_base()}/{path}", data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last = f"Graph API {e.code}: {body[:400]}"
            if not (e.code >= 500 or '"is_transient":true' in body.replace(" ", "")) or attempt == tries:
                raise RuntimeError(last) from None
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"네트워크 오류: {e}"
            if attempt == tries:
                raise RuntimeError(last) from None
        time.sleep(5 * 2 ** (attempt - 1))
    raise RuntimeError(last)


def _get(path: str, params: dict) -> dict:
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{_base()}/{path}?{q}", timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _wait_ready(creation_id: str, token: str, tries: int = 20) -> None:
    for _ in range(tries):
        try:
            st = _get(creation_id, {"fields": "status_code", "access_token": token}).get("status_code")
        except Exception:
            st = None
        if st == "FINISHED":
            return
        if st == "ERROR":
            raise RuntimeError(f"컨테이너 처리 ERROR ({creation_id})")
        time.sleep(4)
    raise RuntimeError(f"컨테이너가 준비되지 않음 ({creation_id})")


def telegram(text: str) -> None:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("(텔레그램 미설정 — 알림 생략)")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20)
    except Exception as e:                     # 알림 실패가 발행 결과를 바꾸면 안 된다
        print(f"(텔레그램 전송 실패: {e})")


# ── 검증 ────────────────────────────────────────────────────────────────

def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _text_sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def load_and_verify(folder: Path, day: str) -> tuple[dict, str, str]:
    """manifest 와 실제 파일이 일치하는지 확인. 하나라도 어긋나면 예외."""
    m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    problems = []
    if m.get("date") != day:
        problems.append(f"manifest 날짜 {m.get('date')} ≠ 오늘 {day}")
    for f in m["images"] + ([m["reel"]] if m.get("reel") else []):
        p = folder / f["name"]
        if not p.exists():
            problems.append(f"파일 없음: {f['name']}")
        elif _sha256(p) != f["sha256"]:
            problems.append(f"승인본과 다름: {f['name']}")
    caption = (folder / "caption.txt").read_text(encoding="utf-8")
    comment = (folder / "first_comment.txt").read_text(encoding="utf-8")
    if _text_sha(caption) != m["caption_sha256"]:
        problems.append("캡션이 승인본과 다름")
    if _text_sha(comment) != m["first_comment_sha256"]:
        problems.append("첫 댓글이 승인본과 다름")
    dl = m.get("deadline")
    if dl and dl < day:
        problems.append(f"마감 지남({dl})")
    if not 2 <= len(m["images"]) <= 10:
        problems.append(f"캐러셀 장수 {len(m['images'])} (2~10장만 가능)")
    if problems:
        raise RuntimeError("; ".join(problems))
    return m, caption, comment


def already_posted(caption: str, token: str, ig_id: str) -> str | None:
    """같은 캡션이 최근 36시간 안에 올라갔으면 그 permalink. 재시도 실행에서 중복 게시 방지."""
    d = _get(f"{ig_id}/media", {"fields": "caption,timestamp,permalink,media_type",
                                "limit": 10, "access_token": token})
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    for x in d.get("data", []):
        ts = datetime.strptime(x["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        if ts >= cutoff and (x.get("caption") or "").strip() == caption.strip() \
                and x.get("media_type") == "CAROUSEL_ALBUM":
            return x.get("permalink", "(permalink 없음)")
    return None


# ── 게시 ────────────────────────────────────────────────────────────────

def _raw(day: str, name: str) -> str:
    # 커밋 SHA 에 고정한 URL — 브랜치 캐시로 옛 파일이 뜨는 일이 없다
    return f"https://raw.githubusercontent.com/{REPO}/{SHA}/queue/{day}/{name}"


def _cdn(day: str, name: str) -> str:
    # 영상은 raw 가 octet-stream 이라 IG 가 거부할 수 있다 → jsDelivr(video/mp4), 역시 SHA 고정
    return f"https://cdn.jsdelivr.net/gh/{REPO}@{SHA}/queue/{day}/{name}"


def _record(folder: Path, rec: dict) -> None:
    (folder / "published.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def run(day: str, dry: bool) -> int:
    folder = QUEUE / day
    if not (folder / "manifest.json").exists():
        print(f"{day}: 예약 없음")
        return 0
    if (folder / "published.json").exists():
        print(f"{day}: 이미 게시됨 → 건너뜀")
        return 0

    token, ig_id = os.environ["IG_ACCESS_TOKEN"], os.environ["IG_USER_ID"]
    m, caption, comment = load_and_verify(folder, day)
    title = m.get("title", day)
    print(f"{day}: '{title}' · 카드 {len(m['images'])}장 · 릴스 {'있음' if m.get('reel') else '없음'}")

    dup = already_posted(caption, token, ig_id)
    if dup:
        _record(folder, {"status": "already_posted", "permalink": dup,
                         "noted_at": datetime.now(KST).isoformat(timespec="seconds")})
        print(f"이미 같은 게시물이 있음 → 기록만 남김: {dup}")
        return 0

    # ① 카드 컨테이너 — dry-run 에서도 만든다(인스타가 이미지 URL 을 실제로 가져가는지 검증)
    children = []
    for f in m["images"]:
        r = _post(f"{ig_id}/media", {"image_url": _raw(day, f["name"]),
                                     "is_carousel_item": "true", "access_token": token})
        children.append(r["id"])
    for c in children:
        _wait_ready(c, token)
    print(f"  카드 컨테이너 {len(children)}개 준비 완료")

    if dry:
        if m.get("reel"):
            r = _post(f"{ig_id}/media", {"media_type": "REELS", "video_url": _cdn(day, m["reel"]["name"]),
                                         "caption": caption, "share_to_feed": "true",
                                         "access_token": token})
            _wait_ready(r["id"], token, tries=45)
            print("  릴스 컨테이너 준비 완료")
        print("(dry-run) 게시하지 않고 종료 — 컨테이너는 24시간 뒤 자동 만료")
        return 0

    # ② 캐러셀 게시
    parent = _post(f"{ig_id}/media", {"media_type": "CAROUSEL", "children": ",".join(children),
                                      "caption": caption, "access_token": token})
    _wait_ready(parent["id"], token)
    media_id = _post(f"{ig_id}/media_publish", {"creation_id": parent["id"],
                                                "access_token": token})["id"]
    link = _get(media_id, {"fields": "permalink", "access_token": token}).get("permalink", "")
    rec = {"status": "published", "carousel_media_id": media_id, "permalink": link,
           "published_at": datetime.now(KST).isoformat(timespec="seconds"), "commit": SHA}
    _record(folder, rec)                       # 캐러셀이 나갔으면 바로 기록 — 이후 단계가 실패해도 재게시 금지
    print(f"  ✅ 캐러셀 게시: {link}")

    notes = []
    try:
        _post(f"{media_id}/comments", {"message": comment, "access_token": token})
    except Exception as e:
        notes.append(f"첫 댓글 실패: {e}")

    if m.get("reel"):
        try:
            r = _post(f"{ig_id}/media", {"media_type": "REELS", "video_url": _cdn(day, m["reel"]["name"]),
                                         "caption": caption, "share_to_feed": "true",
                                         "cover_url": _raw(day, m["images"][0]["name"]),
                                         "access_token": token})
            _wait_ready(r["id"], token, tries=45)
            rid = _post(f"{ig_id}/media_publish", {"creation_id": r["id"], "access_token": token})["id"]
            rec["reel_media_id"] = rid
            try:
                _post(f"{rid}/comments", {"message": comment, "access_token": token})
            except Exception:
                pass
            print(f"  🎬 릴스 게시: {rid}")
        except Exception as e:
            notes.append(f"릴스 실패(캐러셀은 게시됨): {e}")
    rec["notes"] = notes
    _record(folder, rec)

    msg = f"✅ 청년줍줍 발행 완료 {day}\n{title}\n{link}"
    if notes:
        msg += "\n⚠️ " + "\n⚠️ ".join(notes)
    telegram(msg)
    return 0


def warn_if_tomorrow_empty(day: str) -> None:
    tomorrow = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    if not (QUEUE / tomorrow / "manifest.json").exists():
        ahead = sorted(p.name for p in QUEUE.glob("20*") if p.name > day
                       and (p / "manifest.json").exists())
        telegram(f"📭 내일({tomorrow}) 예약이 비어 있어요.\n"
                 f"PC 를 켜면 Claude 가 검토 후 대기열을 채웁니다.\n"
                 f"남은 예약: {', '.join(ahead) if ahead else '없음'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--warn-empty", action="store_true")
    a = ap.parse_args()
    if a.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.date):
        print(f"⛔ 날짜 형식 오류: {a.date!r}")
        return 2
    day = a.date or datetime.now(KST).strftime("%Y-%m-%d")
    try:
        rc = run(day, a.dry_run)
    except Exception as e:
        print(f"⛔ {e}")
        if not a.dry_run:
            telegram(f"⛔ 청년줍줍 발행 실패 {day}\n{e}")
        return 1
    if a.warn_empty and not a.dry_run:
        warn_if_tomorrow_empty(day)
    return rc


if __name__ == "__main__":
    sys.exit(main())
