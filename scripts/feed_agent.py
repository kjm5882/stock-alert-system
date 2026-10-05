"""
피드 담당자 (Feed Agent)
====================================
목적: 트래킹 중인 블로그/유튜브에서 새 글/영상을 찾아,
     Claude API로 "언급된 종목 + 논조 + 이유"를 추출해 저장/알림.

동작 방식:
  1. 각 블로그 RSS에서 최근 글 목록을 가져온다.
  2. data/feed_seen.json 에 이미 처리한 글/영상 목록이 있어 중복 처리를 막는다.
  3. 새 글이 있으면 본문 전체 텍스트를 가져온다.
  4. 유튜브는 채널 RSS에서 제목과 설명란을 가져온다 (자막은 쓰지 않는다 — 아래 참고).
  5. 텍스트를 Claude API에 보내 "종목명/논조/이유/확신도" JSON을 뽑는다.
  6. 결과를 data/feed_signals.jsonl 에 이어붙이고, 텔레그램으로 요약을 보낸다.

■ 유튜브를 자막이 아니라 RSS 설명란으로 처리하는 이유
  유튜브는 데이터센터 IP에서 오는 자막 요청을 차단한다. GitHub Actions가
  바로 그 데이터센터 IP라서 자막 라이브러리가 전부 실패했다.
  (KRX가 Actions IP를 막아 pykrx가 전부 실패했던 것과 같은 종류의 문제)
  RSS 피드는 차단되지 않으므로 제목과 설명란은 받아올 수 있다.

  실측(IT의 신 이형수 채널 최근 15개):
    · 해시태그가 있는 영상 13/15 — #삼성전자 #SK하이닉스 #대덕전자 식으로
      종목명이 그대로 들어 있어 종목 추출에는 충분하다.
    · 다만 제목은 "이 품목?", "이 부품 대박난다"처럼 종목명을 일부러 감춘다.
      15개 중 제목에 종목명이 나온 건 1개뿐이었다. 해시태그가 거의 전부다.

■ 유튜브 신호의 논조·확신도를 판정하지 않는 이유 (중요)
  설명란은 클릭을 유도하려고 쓴 글이라 그대로 판정하면 거의 전부
  '긍정·확신 높음'으로 들어온다. 블로그 본문에는 "지켜봐야 한다" 같은
  표현이 섞이는데 설명란은 그렇지 않다. 한쪽 출처만 체계적으로 후하게
  매겨지면 내러티브 점수 자체가 왜곡된다.

  그렇다고 '중립'으로 채우면 더 나쁘다. 매칭 엔진이 논조·확신도를
  '비율'로 계산하기 때문에 분모만 커져서 점수가 오히려 떨어진다.
  (블로그 1건 긍정만 있을 때 50점 → 유튜브 중립 1건 추가하면 48점)

  그래서 유튜브 기록에는 '논조판정: false'를 달아 보낸다.
  매칭 엔진은 이 기록을 출처 수에만 세고 비율 계산에서는 제외한다.
  출처가 하나 늘어나는 것 자체가 골든존 관문(출처 2곳 이상) 통과에
  결정적이므로, 그것만으로도 값어치가 충분하다.
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
PILOT_BLOGS = [
    "richyun0108", "doctordk", "pokara61", "shryankim", "ranto28",
    "kafca21", "plainvanilla_invest", "kmsmir04", "cybermw", "somewhaterror",
    "onejejuwave", "azplazma", "gangnam_0208", "audistar", "gunbaram",
    "bookgiver", "kimcharger", "leech6976",
]

# 유튜브 채널: 핸들 → 채널ID.
# 채널ID를 미리 적어두면 매번 유튜브 페이지를 긁지 않아도 되고,
# 유튜브가 페이지 구조를 바꿔도 영향을 받지 않는다.
# 모르는 채널은 None으로 두면 한 번 찾아서 로그에 찍어준다. 그걸 여기 적으면 된다.
YOUTUBE_CHANNELS = {
    "godofit_official": "UCQW05vzztAlwV54WL3pjGBQ",   # IT의 신 이형수
    "info_kangyongwoon": None,                        # ← 첫 실행 로그에서 확인 후 기입
}

MAX_POSTS_PER_BLOG = 3      # 블로그당 최근 글 몇 개까지 확인할지
MAX_VIDEOS_PER_CHANNEL = 5  # 유튜브 채널당 최근 영상 몇 개까지 확인할지
                            # (자막을 안 받으므로 비용이 거의 없다. 하루 쉬어도 안 놓치게 넉넉히)

SEEN_KEEP = 2000            # feed_seen.json 에 남겨둘 최근 항목 수 (무한 증가 방지)

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
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("blog_posts", [])
            data.setdefault("youtube_videos", [])
            return data
        except Exception:
            pass
    return {"blog_posts": [], "youtube_videos": []}


def save_seen(seen):
    # 목록이 무한히 길어지지 않도록 최근 것만 남긴다.
    # 오래된 글은 어차피 RSS에 더 이상 나오지 않으므로 지워도 재처리되지 않는다.
    for key in ("blog_posts", "youtube_videos"):
        if len(seen.get(key, [])) > SEEN_KEEP:
            seen[key] = seen[key][-SEEN_KEEP:]
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
    """유튜브 핸들(@xxx)에서 채널ID(UC...)를 찾아낸다.

    YOUTUBE_CHANNELS 에 ID를 적어두지 않은 채널에만 쓴다.
    찾아내면 로그에 찍으니 한 번 보고 위 설정에 적어두면 된다.
    """
    url = f"https://www.youtube.com/@{handle}/about?hl=en&gl=US"
    try:
        res = requests.get(
            url,
            headers=HEADERS,
            cookies={"CONSENT": "YES+1"},  # 유럽/일부 IP의 동의 페이지 리다이렉트 우회
            timeout=15,
        )
        patterns = [
            r'"channelId":"(UC[a-zA-Z0-9_-]{22})"',
            r'"externalId":"(UC[a-zA-Z0-9_-]{22})"',
            r'channel/(UC[a-zA-Z0-9_-]{22})',
        ]
        for pattern in patterns:
            match = re.search(pattern, res.text)
            if match:
                return match.group(1)
        print(f"  [디버그] 응답 길이: {len(res.text)}자, 상태코드: {res.status_code}")
        return None
    except Exception as e:
        print(f"[유튜브 채널ID 오류] {handle}: {e}")
        return None


def _entry_description(entry):
    """feedparser 엔트리에서 media:description 을 꺼낸다.

    feedparser 버전에 따라 담기는 자리가 달라서 후보를 순서대로 살핀다.
    """
    for attr in ("media_description", "summary", "description"):
        value = getattr(entry, attr, None) or (
            entry.get(attr) if hasattr(entry, "get") else None)
        if value and isinstance(value, str) and value.strip():
            return value.strip()
    # media_content / content 안에 들어오는 경우
    for attr in ("content",):
        blocks = getattr(entry, attr, None)
        if isinstance(blocks, list) and blocks:
            value = blocks[0].get("value")
            if value:
                return str(value).strip()
    return ""


def get_channel_videos(channel_id, limit=MAX_VIDEOS_PER_CHANNEL):
    """채널 RSS에서 최근 영상의 제목·설명란·링크를 가져온다.

    자막은 받지 않는다 (파일 상단 설명 참고).
    """
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        feed = feedparser.parse(rss_url)
        videos = []
        for entry in feed.entries[:limit]:
            videos.append({
                "title": entry.title,
                "video_id": getattr(entry, "yt_videoid", None) or entry.id,
                "link": entry.link,
                "description": _entry_description(entry),
            })
        return videos
    except Exception as e:
        print(f"[유튜브 RSS 오류] {channel_id}: {e}")
        return []


# ── Claude API로 종목 추출 ──────────────────────────────
def call_claude(prompt, label):
    """Claude API 호출 공통부. 실패하면 None(재시도 필요)을 돌려준다."""
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
        content = (content.strip().removeprefix("```json")
                   .removeprefix("```").removesuffix("```").strip())
        return json.loads(content)
    except Exception as e:
        print(f"[Claude 추출 오류] {label}: {e}")
        return None  # None = 실패(재시도 필요). 빈 리스트[]와 구분해서 사용.


def extract_stocks_with_claude(text, source_name):
    """블로그 본문에서 종목/논조/이유/확신도를 뽑는다."""
    if not text or len(text.strip()) < 30:
        return []

    text = text[:12000]   # 너무 길면 앞부분만 (비용/토큰 절약)

    prompt = f"""다음은 한국 주식 관련 블로그 글입니다. 출처: {source_name}

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
    return call_claude(prompt, source_name)


def extract_stocks_from_video_meta(title, description, source_name):
    """유튜브 제목+설명란에서 '종목명만' 뽑는다.

    논조와 확신도는 묻지 않는다. 설명란은 클릭 유도용으로 쓰인 글이라
    여기서 논조를 판정하면 거의 전부 긍정으로 기울기 때문이다.
    """
    text = f"제목: {title}\n\n설명란:\n{(description or '')[:3000]}"
    if len(text.strip()) < 20:
        return []

    prompt = f"""다음은 한국 주식 관련 유튜브 영상의 제목과 설명란입니다. 출처: {source_name}

여기서 언급된 **한국 상장 기업**만 찾아 아래 JSON 배열로만 답하세요.
설명 없이 JSON만 출력하고, 없으면 []을 출력하세요.

지켜야 할 것:
- 해시태그(#삼성전자 등)에 들어있는 회사명이 가장 중요한 단서입니다.
- 해외 기업(엔비디아, TSMC, 마이크론, 인텔, 애플 등)은 제외하세요.
- 기술·제품·산업 용어(HBM, D램, 유리기판, 소부장 등)는 회사가 아니므로 제외하세요.
- 논조나 전망은 판단하지 마세요. 설명란만으로는 알 수 없습니다.

형식:
[
  {{"종목명": "회사명", "언급이유": "제목/설명란에서 어떤 맥락으로 나왔는지 한 문장"}}
]

내용:
{text}
"""
    return call_claude(prompt, source_name)


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
    blog_count = youtube_count = 0

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
            if stocks is None:
                print("  → 추출 실패, 다음 실행에 재시도합니다 (이번엔 '확인 완료' 처리 안 함)")
                continue  # seen에 추가하지 않음 → 다음 실행에서 다시 시도

            timestamp = datetime.now(timezone.utc).isoformat()

            for s in stocks:
                record = {
                    "timestamp": timestamp,
                    "source_type": "blog",
                    "source_name": blog_id,
                    "post_title": post["title"],
                    "post_link": post["link"],
                    "논조판정": True,       # 본문 전체를 읽었으므로 논조·확신도를 믿을 수 있다
                    **s,
                }
                append_signal(record)
                new_signals_summary.append(record)
                blog_count += 1

            seen["blog_posts"].append(post["link"])
            time.sleep(1)  # API 호출 간격

    # 2) 유튜브 처리 (제목 + 설명란만, 자막 없음)
    for handle, channel_id in YOUTUBE_CHANNELS.items():
        print(f"\n[유튜브] @{handle} 확인 중...")

        if not channel_id:
            channel_id = get_channel_id_from_handle(handle)
            if not channel_id:
                print(f"  채널ID를 찾지 못했습니다: {handle}")
                continue
            print(f"  ★ 채널ID를 찾았습니다: {channel_id}")
            print(f"    → YOUTUBE_CHANNELS 설정에 \"{handle}\": \"{channel_id}\" 로 "
                  f"적어두면 다음부터 조회를 건너뜁니다.")

        videos = get_channel_videos(channel_id)
        if not videos:
            print("  RSS에서 영상을 받지 못했습니다.")
            continue

        for video in videos:
            if video["video_id"] in seen["youtube_videos"]:
                continue

            desc_len = len(video.get("description") or "")
            print(f"  새 영상 발견: {video['title']} (설명란 {desc_len}자)")

            stocks = extract_stocks_from_video_meta(
                video["title"], video.get("description"), f"유튜브 @{handle}")
            if stocks is None:
                print("  → 추출 실패, 다음 실행에 재시도합니다")
                continue

            if not stocks:
                # 설명란에 해시태그가 없는 영상이 간혹 있다. 그건 그냥 넘긴다.
                print("  → 언급된 국내 종목 없음")
                seen["youtube_videos"].append(video["video_id"])
                time.sleep(1)
                continue

            timestamp = datetime.now(timezone.utc).isoformat()

            for s in stocks:
                record = {
                    "timestamp": timestamp,
                    "source_type": "youtube",
                    "source_name": handle,
                    "post_title": video["title"],
                    "post_link": video["link"],
                    "종목명": s.get("종목명"),
                    "언급이유": s.get("언급이유", ""),
                    # 아래 세 줄이 핵심이다.
                    # 설명란으로는 논조·확신도를 판정할 수 없으므로 판정하지 않는다.
                    # 매칭 엔진은 '논조판정: false'를 보고 출처 수에만 세고
                    # 긍정비율·확신도비율 계산에서는 이 건을 제외한다.
                    "논조": "중립",
                    "확신도": "중간",
                    "논조판정": False,
                }
                append_signal(record)
                new_signals_summary.append(record)
                youtube_count += 1

            seen["youtube_videos"].append(video["video_id"])
            time.sleep(1)

    save_seen(seen)

    # 3) 텔레그램 요약 전송
    if new_signals_summary:
        lines = [f"📰 <b>피드 담당자</b> (종목 언급 {len(new_signals_summary)}건 "
                 f"— 블로그 {blog_count} · 유튜브 {youtube_count})\n"]
        for r in new_signals_summary[:20]:  # 너무 길면 20개까지만
            if r.get("논조판정") is False:
                head = f"📺 <b>{r.get('종목명')}</b> (유튜브: {r['source_name']})"
                tail = f"   └ {r.get('언급이유', '')} [논조 판정 안 함 — 제목·설명란만]"
            else:
                emoji = {"긍정": "🟢", "부정": "🔴", "중립": "⚪"}.get(r.get("논조"), "⚪")
                head = f"{emoji} <b>{r.get('종목명')}</b> ({r['source_type']}: {r['source_name']})"
                tail = f"   └ {r.get('언급이유', '')} [확신도: {r.get('확신도', '-')}]"
            lines.append(head + "\n" + tail)
        send_telegram("\n".join(lines))
        print(f"\n총 {len(new_signals_summary)}건의 종목 언급을 찾았고, "
              f"텔레그램으로 전송했습니다. (블로그 {blog_count} · 유튜브 {youtube_count})")
    else:
        print("\n새로운 종목 언급이 없습니다. (새 글/영상이 없거나, 언급된 종목이 없음)")


if __name__ == "__main__":
    main()
