# Real 통합 국내·미국 주식 분석기

Real은 키움 REST API의 국내주식(KOSPI·KOSDAQ)과 미국주식(NASDAQ·NYSE·AMEX)을 한 화면에서 분석합니다. 공통 분석 엔진은 일목균형표 9·26·52, 거래량·거래대금, ATR, ADX/+DI/-DI, 선행 스팬 B 수평 구간, 과거 거래량 집중 가격, 주봉, 구조 손익비와 과거 유사패턴을 함께 확인합니다.

## 가장 쉬운 실행 순서

1. 키움 REST API 홈페이지에서 API 사용 신청 후 앱키와 시크릿키를 발급받습니다.
2. 통합 화면은 `주식분석기_실행.bat`를 더블클릭합니다.
3. 첫 실행에만 앱키와 시크릿키를 붙여 넣습니다. 입력 문자는 화면에 표시되지 않으며 Windows DPAPI로 암호화해 `.runtime`에 저장합니다.
4. 화면의 국내/미국 입력창에서 아래처럼 입력합니다.

```text
삼성전자 분석해줘
005930 분석해줘
AAPL 분석해줘
```

Windows 화면으로 사용하려면 `주식분석기_실행.bat`를 더블클릭합니다. 화면 상단은
`국내 주식 분석`과 `미국 주식 분석`으로 나뉘며, 각 입력창에 `삼성전자 분석해줘`,
`005930 분석해줘`, `AAPL 분석해줘`처럼 입력한 뒤 Enter를 누르면 개별 분석을 시작합니다.
각 시장의 `전체 분석해줘` 버튼은 해당 시장의 전체 분석을 실행합니다. 국내와 미국 모두 실제 키움 REST 분석 엔진에 연결되어 있습니다.

한글 종목명도 지원하지만 같은 이름이 여러 개면 티커를 입력하는 것이 가장 정확합니다. 저장된 API 키를 바꿀 때만 `키움_API키_재설정.bat`를 실행합니다.

## 전체 분석은 무엇을 하나요?

`전체 분석해줘` 한 번으로 다음 작업을 끝까지 진행합니다.

1. 키움의 미국주식 시가총액 상위와 거래대금 상위 목록을 합칩니다.
2. $5 미만 저가주와 ETF·ETN·워런트·유닛·권리·우선주·스팩을 제외합니다.
3. 한 업종에만 몰리지 않도록 분산하여 기본 220개를 고릅니다.
4. 각 종목의 수정주가 일봉을 최대 650개까지 수집하고 일봉·주봉 일목 구조를 계산합니다.
5. 최근 20일 평균 거래량 20만 주, 평균 거래대금 2천만 달러 미만은 관심 후보에서 제외합니다.
6. SPY와 QQQ를 시장 방향 확인용으로만 분석합니다. 두 ETF 자체는 종목 후보에 넣지 않습니다.
7. 일목 핵심 상태가 모두 같은 과거만 찾고, 그 안에서 RSI·SMA·모멘텀·거래량·ATR·ADX·캔들 폭도 각 항목별로 최대한 같은 사례를 찾습니다. 서로의 10거래일 결과 구간이 겹치지 않도록 표본을 띄워 최대 30건의 결과를 표시합니다.
8. 브라우저용 `report.html`, Markdown 원본 `report.md`, 전체 종목값 `all_results.csv`를 `reports` 폴더에 저장합니다.

첫 전체 분석은 종목별 과거 일봉을 받기 때문에 수 분이 걸릴 수 있습니다. 이후에는 별도 미국주식 캐시를 재사용하며, 미국 정규장 중에는 오래된 캐시만 자동 갱신합니다. 과거처럼 같은 명령을 여러 번 입력해 분석 범위를 늘리는 구조가 아닙니다.

### 국내 전체 분석

국내 `전체 분석해줘` 버튼은 KOSPI·KOSDAQ 종목마스터에서 ETF·ETN·우선주·스팩·관리/경고 종목을 제외하고, KRX 거래대금 상위 일반주를 시장·업종별로 분산해 기본 160개를 분석합니다. `ka10081` 수정주가 일봉은 국내 장 마감 전 오늘 진행 중인 일봉을 제거하고 완료된 일봉만 사용합니다. KOSPI·KOSDAQ 지수 일봉은 시장 방향 확인용으로 사용하며, 개별 분석의 60분봉도 아직 끝나지 않은 현재 시간 버킷을 제외합니다. 상장 후 완료 일봉이 80개 미만인 신규 종목은 오류가 아니라 `데이터 기간 부족`으로 자동 제외합니다.

국내 캐시는 `.domestic_ichimoku_cache`, 미국 캐시는 `.us_ichimoku_cache`로 완전히 분리됩니다. 인증정보는 같은 키움 앱키·시크릿키를 Windows DPAPI로 암호화한 `.runtime/kiwoom_rest_credentials.dat`를 공유합니다.

## 주식 규칙과 외환 규칙의 차이

참고 글은 시장별 규칙을 구분합니다. 외환의 금리 차이·기준선 회귀 규칙은 Real2에 넣지 않았습니다. Real2는 글의 **주식** 항목에 맞춰 거래량과 선행 스팬 B의 수평 지지·저항, 장기 지지선 확인을 우선합니다. 또한 미국주식에 필요한 USD, 미국 동부시간과 서머타임을 별도로 처리합니다.

## 결과를 읽는 법

- `확인할 가격`: 현재 흐름을 유지하거나 다시 넘어야 하는 가격입니다.
- `시나리오 무효 가격`: 일봉 종가가 이 아래로 내려가면 현재 상승 해석을 폐기하는 기준입니다.
- `첫 저항·목표 후보`: 도달 보장이 아니라 위에서 막힐 수 있는 첫 가격대입니다. 관측된 상단 저항이 없으면 `확인 불가`로 표시하고 관심 후보 판정을 보류합니다.
- `손익비`: 현재 종가와 ATR 보정 무효 가격의 위험 대비 실제 첫 저항까지의 보상 비율입니다. 가까운 저항 때문에 1.5:1 미만이면 관심 후보에서 보수적으로 제외합니다.
- `ADX(14)`: 추세의 강도를 보고 `+DI/-DI`로 방향을 함께 확인합니다. ADX 25 이상이면서 +DI가 -DI보다 높을 때 상승 추세 힘을 긍정적으로 보고, 반대면 오히려 위험으로 봅니다. 18 미만은 횡보 위험을 높게 봅니다.
- `A+`: 수익 보장이 아니라 일목 상승 구조가 많이 일치한 등급입니다.
- `과거 지표 일치 10일`: 현재 일목 상태가 모두 같고 보조지표도 각 항목별로 최대한 같은 과거 사례의 표본 내 결과입니다. 미래 확률이나 매수 허가가 아닙니다.

개별 분석은 키움 현재가를 별도로 다시 조회하고 60분봉을 짧은 흐름 참고로 추가합니다. 전체 분석은 속도와 호출 제한 때문에 종목별 분봉을 호출하지 않습니다.

## 시장·뉴스 보조 분석

차트만으로 판단하지 않도록 `core/market_intelligence.py`가 별도 보조 환경을 만듭니다.

- 시장 지표: FinanceDataReader로 KOSPI/KOSDAQ, S&P 500, NASDAQ, VIX, 원/달러, 미국 10년물 등 시장별 핵심 지표를 확인합니다.
- 국내 뉴스: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`이 설정되어 있으면 네이버 뉴스 검색 API를 우선 사용합니다. 미설정 시 Google News RSS를 보조 소스로 사용합니다.
- 미국/종목 뉴스: Google News RSS에서 최근 관련 기사를 가져옵니다.
- 글로벌 사건: GDELT DOC 2.0에서 전쟁·관세·제재·사이버공격·재난 등 시장 충격 가능 이벤트를 확인하고, GDELT가 느리거나 실패하면 Google News RSS로 자동 대체합니다.
- 외부 정보는 `.runtime/market_intelligence_cache.json`에 기본 20분 캐시합니다. 외부 소스가 실패해도 키움 차트 분석은 중단되지 않습니다.

개별 종목 보고서는 `기술 점수 65% + 시장·뉴스 환경 35%`의 **보조 종합점수**를 함께 표시합니다. 기존 차트가 관심 후보더라도 환경 점수가 42점 미만이거나 글로벌 이벤트 위험이 75점 이상이면 최종 표시는 `기다림`으로 낮춥니다. 이 임계값과 비중은 아직 실험값이므로 향후 백테스트로 조정해야 합니다.

전체 분석은 종목 수가 많아 모든 종목의 뉴스를 각각 호출하지 않고, 시장 전체의 지수·뉴스·글로벌 이벤트 환경을 한 번만 수집해 후보 판정에 반영합니다. 개별 종목 분석에서는 해당 회사/종목 뉴스까지 추가합니다.

필요하면 환경 변수로 조정할 수 있습니다.

```text
REAL_INTELLIGENCE_ENABLED=1
REAL_INTELLIGENCE_TIMEOUT=4
REAL_INTELLIGENCE_CACHE_MINUTES=20

# 국내 뉴스 정확도를 높이고 싶을 때만 설정
NAVER_CLIENT_ID=발급받은_클라이언트_ID
NAVER_CLIENT_SECRET=발급받은_클라이언트_SECRET
```

외부 시장·뉴스 보조 기능을 완전히 끄고 기존 차트 분석만 쓰려면 `REAL_INTELLIGENCE_ENABLED=0`으로 실행합니다.

뉴스 검색과 점수 로직은 그대로 유지하면서, 앞으로 분석기가 실제 사용한 뉴스는 기본적으로
`data/news/` 아래에 원본 hash, 정규화 이벤트, 분석 시각과 함께 저장됩니다. 이 archive는
향후 point-in-time 검증용이며 현재 65/35 점수나 판단에 다시 입력되지 않습니다.

```text
REAL_NEWS_ARCHIVE_ENABLED=1
# 선택: 기본 data/news 대신 다른 보관 위치
REAL_NEWS_ARCHIVE_ROOT=C:\path\to\news_archive

# 무료 공식 공시 수집용
DART_API_KEY=OpenDART_인증키
SEC_USER_AGENT=이름 또는 조직 contact@example.com
```

무료 source 검증·수집 CLI는 다음처럼 실행합니다.

```powershell
# GDELT 15분 bulk 표본(Event/GKG/Mentions)
py -3 collect_free_news.py --source GDELT --sample

# OpenDART/SEC/현재 뉴스까지 포함. 키가 없거나 source가 실패해도 상태를 명시합니다.
py -3 collect_free_news.py --source ALL --sample

# 로컬 이벤트 index와 PIT/중복 상태 감사
py -3 audit_news_archive.py
```

원본은 `data/news/raw/`, 정규화 결과는 `data/news/processed/events/`, 로컬 조회
index는 `data/news/index/`, 실행별 사용 기록은 `data/news/forward/`에 분리됩니다.
개별 분석 완료 시 기술점수·등급·기술판단·최종판단과 당시 보인 공시/뉴스/event ID가
하나의 `analysis_id` snapshot으로 자동 저장됩니다. raw hash → normalized event →
cluster 연결을 보존하므로 나중에 뉴스 알고리즘만 다시 적용할 수 있으며, 1/5/10/20일
outcome은 별도 파일로 연결됩니다. OpenDART/SEC 자격증명이 없거나 API가 실패해도
기술 분석은 계속됩니다.
GDELT·OpenDART·SEC·Naver·Google RSS의 실제 범위와 신뢰 한계는
[`docs/free_news_event_sources.md`](docs/free_news_event_sources.md)에 정리되어 있습니다.

분석이 끝나면 안내되는 `report.html`을 더블클릭하면 별도 프로그램 없이 브라우저에서 볼 수 있습니다. 넓은 화면에서는 표의 내용이 셀 안에서 줄바꿈되고, 좁은 화면에서는 각 종목이 세로형 카드로 표시됩니다. 브라우저의 인쇄 기능으로 PDF 저장도 가능하며 기존 `report.md`도 함께 유지됩니다.

## 조정 가능한 값

필요할 때만 Windows 환경 변수로 바꿀 수 있습니다.

```text
REAL2_SCAN_SIZE=220
REAL2_SCAN_WORKERS=8
REAL2_MIN_AVG_VOLUME=200000
REAL2_MIN_AVG_DOLLAR_VOLUME=20000000

REAL_KR_SCAN_SIZE=160
REAL_KR_SCAN_WORKERS=6
REAL_KR_MIN_AVG_VOLUME=100000
REAL_KR_MIN_AVG_TRADE_VALUE=5000000000
```

키움 공식 REST API는 미국주식을 지원하며, 조회 TR은 일반적으로 초당 호출 제한이 있습니다. 이 프로그램은 전 요청을 중앙 속도 제한기로 통과시키고 토큰 만료·일시 오류를 자동 재시도합니다.

- 키움 REST API 소개: https://openapi.kiwoom.com/intro
- 키움 REST API 가이드: https://openapi.kiwoom.com/guide/apiguide
- 일목균형표 참고 글: https://m4markets.com/media-kor/ichimoku-cloud-interpretation-trading-strategies/

## 개발 확인

```powershell
py -3 -m pytest -q
py -3 us_ichimoku_analyzer.py --self-test
py -3 domestic_stock_analyzer.py --self-test
```

위 테스트와 자체점검은 합성 데이터 및 모의 응답을 사용하며 실시간 API 연결을 검증하지는 않습니다. 전체 분석에서 재시도 후에도 실패한 종목이 있으면 보고서는 보존하고, `--once` 실행은 종료 코드 `2`로 부분 실패를 알립니다.

## Point-in-time 평가 데이터 기반

운영 분석 규칙과 분리된 `data_pipeline/`은 과거 데이터의 원본/가공본, manifest,
SHA-256, 품질 리포트, 실패 목록과 재개 체크포인트를 관리합니다. 현재 자격증명으로
소규모 연결 검증을 실행하려면 다음 명령을 사용합니다.

```powershell
py -3 collect_historical_data.py --market BOTH --sample-size 20 --dataset historical_sample
py -3 audit_historical_dataset.py --dataset historical_sample
py -3 evaluate_analyzer.py --market BOTH --dataset historical_sample `
  --baseline-mode TECHNICAL_BASELINE --decision-step 20
```

`historical_sample`은 현재 종목목록과 최근 Kiwoom 이력뿐인 탐색용 표본입니다. 날짜별
상장폐지 포함 universe와 검증된 corporate-action 정책이 없으므로 out-of-sample
성능 근거가 아닙니다. 신뢰 가능한 `historical` 데이터셋을 만들기 위한 KRX/CRSP/
Norgate/Massive/Tiingo 비교와 필수 필드는
[`docs/historical_data_sources.md`](docs/historical_data_sources.md)에 정리되어 있습니다.
`TRUSTED/PARTIALLY_TRUSTED/EXPLORATORY/NOT_EVALUABLE` 판정 기준은
[`docs/baseline_trust_gates.md`](docs/baseline_trust_gates.md)에 있습니다.

장중 미완료 캔들, 손상된 가격·날짜·거래량 데이터, 미국 서머타임 전환을 포함한 분봉 캐시를 점검합니다. 주봉을 확인하지 못한 종목은 관심 판정을 보류하며, 과거 유사패턴은 일목 계산에 필요한 기간이 확보되고 이후 10거래일 결과가 모두 있는 표본만 사용합니다.

이 프로그램은 주문 기능이 없으며 조건부 차트 분석만 제공합니다.
