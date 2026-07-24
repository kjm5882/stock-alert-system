# ============================================================
# Telegram 알림 전송
# ============================================================
import os
import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("[경고] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않아 전송을 건너뜁니다.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    res = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }, timeout=15)

    if res.status_code != 200:
        print(f"[오류] 텔레그램 전송 실패: {res.status_code} {res.text}")
