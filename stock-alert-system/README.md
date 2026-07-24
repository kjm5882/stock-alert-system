# 📈 stock-alert-system 사용 가이드 (완전 초보자용)

이 문서만 따라 하면 코딩을 몰라도 끝까지 설정할 수 있습니다.

---

## 1단계. 파일을 GitHub 저장소에 올리기

1. 웹 브라우저에서 본인의 GitHub 저장소(`stock-alert-system`) 페이지로 들어갑니다.
2. `Add file` → `Upload files` 버튼을 클릭합니다.
3. 이번에 받은 폴더 안의 파일/폴더들을 통째로 끌어다 놓습니다. (구조 그대로 유지되어야 합니다)
   - `config/watchlist.py`
   - `src/` 폴더 전체
   - `.github/workflows/quarterly-report.yml`
   - `requirements.txt`
   - `README.md`
4. 아래쪽 `Commit changes` 버튼을 눌러 저장합니다.

> 💡 폴더째로 드래그가 안 되면, GitHub Desktop 앱을 설치해서 폴더를 복사해 넣고 커밋/푸시하는 방법도 있습니다. 이 부분은 원하시면 제가 더 자세히 안내해드릴 수 있어요.

---

## 2단계. 비밀키(Secrets) 등록하기

프로그램이 API 키나 봇 토큰을 코드에 직접 노출하지 않고 안전하게 쓰도록, GitHub의 "Secrets" 기능에 등록합니다.

1. 저장소 페이지에서 `Settings` 탭 클릭
2. 왼쪽 메뉴에서 `Secrets and variables` → `Actions` 클릭
3. `New repository secret` 버튼으로 아래 3개를 하나씩 등록:

| 이름 (정확히 이대로) | 값 |
|---|---|
| `DART_API_KEY` | 기존에 발급받으신 DART API 키 |
| `TELEGRAM_BOT_TOKEN` | @Lahee_papa5882_bot 만들 때 BotFather가 준 토큰 |
| `TELEGRAM_CHAT_ID` | 아래 3단계에서 확인하는 방법 참고 |

---

## 3단계. 텔레그램 CHAT_ID 확인하기

1. 텔레그램 앱에서 본인 봇(@Lahee_papa5882_bot)에게 아무 메시지나 하나 보냅니다 (예: "안녕").
2. 웹 브라우저 주소창에 아래 주소를 입력합니다 (BOT_TOKEN 자리에 실제 토큰을 넣어서):
   ```
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```
3. 화면에 나오는 텍스트 중 `"chat":{"id":123456789,...` 부분에서 숫자가 CHAT_ID입니다.
4. 이 숫자를 2단계의 `TELEGRAM_CHAT_ID` 시크릿 값으로 등록합니다.

---

## 4단계. 수동으로 한 번 실행해보기 (테스트)

1. 저장소 페이지에서 `Actions` 탭 클릭
2. 왼쪽에서 `실적 및 재무지표 알림` 워크플로우 선택
3. 오른쪽의 `Run workflow` 버튼 클릭 → 다시 `Run workflow` 확인 클릭
4. 30초~1분 정도 기다리면 텔레그램으로 메시지가 옵니다.
   - 지금은 `config/watchlist.py`에 등록된 종목이 없어서
     "구조 점검 실행" 메시지만 오는 게 정상입니다. (에러 아님)

---

## 5단계. 나중에 종목 추가하는 법

1. 저장소에서 `config/watchlist.py` 파일을 엽니다.
2. `WATCHLIST = [` 아래에 이렇게 추가:
   ```python
   WATCHLIST = [
       {"ticker": "042660", "name": "한화오션"},
       {"ticker": "005930", "name": "삼성전자"},
   ]
   ```
3. `Commit changes` 저장
4. 그 다음부터는 매일 한국시간 오전 8시에 자동으로,
   또는 4단계처럼 수동으로도 언제든 실행 가능합니다.

---

## 지금 이 구조가 하는 일

- **DART**: 등록된 종목의 최신 분기/사업보고서에서 매출액, 영업이익, 순이익, 자산/부채/자본을 가져옴
- **KRX(pykrx)**: 같은 종목의 최근 종가, 등락률, 거래량, 시가총액, PER/PBR/배당수익률을 가져옴
- **계산**: 영업이익률, 순이익률, ROE, 부채비율을 자동 계산
- **전송**: 위 내용을 정리해서 텔레그램으로 전송

## 다음에 추가하면 좋은 기능 (원하시면 말씀해주세요)
- 전분기/전년동기 대비 성장률 자동 비교 (데이터 누적 필요)
- 특정 조건(예: 영업이익률 급증, PER 급락) 발생 시에만 알림 보내는 스크리닝 로직
- 데이터를 구글 스프레드시트에도 함께 저장
- 실적 발표 D-day 캘린더 알림
