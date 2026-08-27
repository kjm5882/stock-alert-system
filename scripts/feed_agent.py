"""
피드 담당자 (Feed Agent) - 파일럿 버전
====================================
목적: 트래킹 중인 블로그/유튜브에서 새 글/영상을 찾아,
     Claude API로 "언급된 종목 + 논조 + 이유"를 추출해 저장/알림.

파일럿 대상:
  - 블로그 3개: richyun0108, doctordk, pokara61
  - 유튜브 1개: @godofit_official

동작 방식:
  1. 각 블로그 RSS에서 최근 글 목록을 가져온다.
  2. data/feed_seen.json 에 이미 처리한 글/영상 목록이 있어 중복 처리를 막는다.
  3. 새 글이 있으면 본문 전체 텍스트를 가져온다.
  4. 유튜브는 채널ID를 찾아 RSS로 최근 영상 목록을 가져오고, 자막을 가져온다.
  5. 텍스트를 Claude API에 보내 "종목명/논조/이유/확신도" JSON을 뽑는다.
  6. 결과를 data/feed_signals.jsonl 에 이어붙이고, 텔레그램으로 요약을 보낸다.
"""

import os
import re
import json
import time
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# ── 설정 ──────────────────────────────────────────────
PILOT_BLOGS = ["richyun0108", "doctordk", "pokara61"]
PILOT_YOUTUBE_HANDLES = ["godofit_official"]

MAX_POSTS_PER_BLOG = 3      # 블로그당 최근 글 몇 개까지 확인할지 (파일럿이라 적게)
MAX_VIDEOS_PER_CHANNEL = 2  # 유튜브 채널당 최근 영상 몇 개까지 확인할지

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SEEN_FILE = os.path.join(DATA_DIR, "feed_seen.json")
SIGNALS_FILE = os.path.join(DATA_DIR, "feed_signals.jsonl")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ── 상태 관리 (중복 처리 방지) ──────────────────────────
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"blog_posts": [], "youtube_videos": []}


def save_seen(seen):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def append_signal(record):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SIGNALS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── 블로그 수집 ───────────────────────────────────────
def get_blog_posts(blog_id, limit=MAX_POSTS_PER_BLOG):
    """네이버 블로그 RSS에서 최근 글 목록(제목, 링크)을 가져온다."""
    rss_url = f"https://rss.blog.naver.com/{blog_id}.xml"
    try:
        feed = feedparser.parse(rss_url)
        posts = []
        for entry in feed.entries[:limit]:
            posts.append({"title": entry.title, "link": entry.link})
        return posts
    except Exception as e:
        print(f"[블로그 RSS 오류] {blog_id}: {e}")
        return []


def get_blog_full_text(link):
    """블로그 글 링크에서 본문 전체 텍스트를 추출한다 (모바일 버전 사용)."""
    m_link = link.replace("blog.naver.com", "m.blog.naver.com")
    try:
        res = requests.get(m_link, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        # 신형 에디터(스마트에디터 3.0/ONE)
        container = soup.select_one("div.se-main-container")
        if container:
            return container.get_text(separator="\n", strip=True)

        # 구형 에디터
        container = soup.select_one("div#postViewArea")
        if container:
            return container.get_text(separator="\n", strip=True)

        return ""
    except Exception as e:
        print(f"[블로그 본문 오류] {link}: {e}")
        return ""


# ── 유튜브 수집 ───────────────────────────────────────
def get_channel_id_from_handle(handle):
    """유튜브 핸들(@xxx)에서 채널ID(UC...)를 찾아낸다."""
    url = f"https://www.youtube.com/@{handle}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        match = re.search(r'"channelId":"(UC[a-zA-Z0-9_-]{22})"', res.text)
        if match:
            return match.group(1)
        return None
    except Exception as e:
        print(f"[유튜브 채널ID 오류] {handle}: {e}")
        return None


def get_channel_videos(channel_id, limit=MAX_VIDEOS_PER_CHANNEL):
    """채널 RSS에서 최근 영상 목록(제목, video_id, 링크)을 가져온다."""
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        feed = feedparser.parse(rss_url)
        videos = []
        for entry in feed.entries[:limit]:
            video_id = entry.yt_videoid
            videos.append({
                "title": entry.title,
                "video_id": video_id,
                "link": entry.link,
            })
        return videos
    except Exception as e:
        print(f"[유튜브 RSS 오류] {channel_id}: {e}")
        return []


def get_video_transcript(video_id):
    """영상 자막(한국어 우선, 없으면 영어)을 가져온다."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=["ko"])
        except Exception:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
        return " ".join([t["text"] for t in transcript])
    except Exception as e:
        print(f"[자막 오류] {video_id}: {e}")
        return ""


# ── Claude API로 종목 추출 ──────────────────────────────
def extract_stocks_with_claude(text, source_name):
    """텍스트에서 언급된 종목/논조/이유/확신도를 JSON으로 추출한다."""
    if not text or len(text.strip()) < 30:
        return []

    # 텍스트가 너무 길면 앞부분만 사용 (비용/토큰 절약, 파일럿 단계라 보수적으로)
    text = text[:12000]

    prompt = f"""다음은 한국 주식 관련 블로그/유튜브 콘텐츠입니다. 출처: {source_name}

이 텍스트에서 구체적으로 언급된 한국 상장 기업(종목)을 모두 찾아서 아래 JSON 형식으로만 답하세요.
설명이나 다른 텍스트 없이 JSON 배열만 출력하세요. 언급된 종목이 없으면 빈 배열 []을 출력하세요.

형식:
[
  {{
    "종목명": "정식 회사명 또는 언급된 이름",
    "논조": "긍정" | "부정" | "중립",
    "언급이유": "왜 언급되었는지 한 문장 요약",
    "확신도": "높음" | "중간" | "낮음"
  }}
]

텍스트:
{text}
"""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        response.raise_for_status()
        content = response.json()["content"][0]["text"]

        # 혹시 모를 코드블록 표시 제거
        content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(content)
    except Exception as e:
        print(f"[Claude 추출 오류] {source_name}: {e}")
        return []


# ── 텔레그램 알림 ────────────────────────────────────
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[텔레그램 미설정] 메시지 전송 생략")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=15)
    except Exception as e:
        print(f"[텔레그램 오류] {e}")


# ── 메인 실행 ────────────────────────────────────────
def main():
    seen = load_seen()
    new_signals_summary = []

    # 1) 블로그 처리
    for blog_id in PILOT_BLOGS:
        print(f"\n[블로그] {blog_id} 확인 중...")
        posts = get_blog_posts(blog_id)
        for post in posts:
            if post["link"] in seen["blog_posts"]:
                continue

            print(f"  새 글 발견: {post['title']}")
            full_text = get_blog_full_text(post["link"])
            if not full_text:
                seen["blog_posts"].append(post["link"])
                continue

            stocks = extract_stocks_with_claude(full_text, f"블로그 {blog_id}")
            timestamp = datetime.now(timezone.utc).isoformat()

            for s in stocks:
                record = {
                    "timestamp": timestamp,
                    "source_type": "blog",
                    "source_name": blog_id,
                    "post_title": post["title"],
                    "post_link": post["link"],
                    **s,
                }
                append_signal(record)
                new_signals_summary.append(record)

            seen["blog_posts"].append(post["link"])
            time.sleep(1)  # API 호출 간격

    # 2) 유튜브 처리
    for handle in PILOT_YOUTUBE_HANDLES:
        print(f"\n[유튜브] @{handle} 확인 중...")
        channel_id = get_channel_id_from_handle(handle)
        if not channel_id:
            print(f"  채널ID를 찾지 못했습니다: {handle}")
            continue

        videos = get_channel_videos(channel_id)
        for video in videos:
            if video["video_id"] in seen["youtube_videos"]:
                continue

            print(f"  새 영상 발견: {video['title']}")
            transcript = get_video_transcript(video["video_id"])
            if not transcript:
                seen["youtube_videos"].append(video["video_id"])
                continue

            stocks = extract_stocks_with_claude(transcript, f"유튜브 @{handle}")
            timestamp = datetime.now(timezone.utc).isoformat()

            for s in stocks:
                record = {
                    "timestamp": timestamp,
                    "source_type": "youtube",
                    "source_name": handle,
                    "post_title": video["title"],
                    "post_link": video["link"],
                    **s,
                }
                append_signal(record)
                new_signals_summary.append(record)

            seen["youtube_videos"].append(video["video_id"])
            time.sleep(1)

    save_seen(seen)

    # 3) 텔레그램 요약 전송
    if new_signals_summary:
        lines = [f"📰 <b>피드 담당자 파일럿 결과</b> ({len(new_signals_summary)}건 종목 언급 발견)\n"]
        for r in new_signals_summary[:20]:  # 너무 길면 20개까지만
            emoji = {"긍정": "🟢", "부정": "🔴", "중립": "⚪"}.get(r.get("논조"), "⚪")
            lines.append(
                f"{emoji} <b>{r.get('종목명')}</b> ({r['source_type']}: {r['source_name']})\n"
                f"   └ {r.get('언급이유', '')} [확신도: {r.get('확신도', '-')}]"
            )
        send_telegram("\n".join(lines))
        print(f"\n총 {len(new_signals_summary)}건의 종목 언급을 찾았고, 텔레그램으로 전송했습니다.")
    else:
        print("\n새로운 종목 언급이 없습니다. (새 글/영상이 없거나, 언급된 종목이 없음)")


if __name__ == "__main__":
    main()
