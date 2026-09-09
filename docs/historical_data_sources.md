# Point-in-time 평가 데이터 소스 조사

조사 기준일: 2026-09-09. 이 문서는 공급자의 공식 문서에서 확인된 내용과
확인되지 않은 내용을 구분한다. `가능`은 해당 필드가 존재한다는 뜻이지, 모든
종목·모든 기간에서 완전하다는 보장은 아니다. 가격과 이용조건은 변경될 수 있으므로
구매 직전에 다시 확인해야 한다.

## 1. 평가 입력 계약과 우선순위

### A. Baseline에 반드시 필요

| 데이터 | 최소 필드 | 이유 |
|---|---|---|
| 일반주식 완료 일봉 | permanent_id, session_date, raw/adjusted OHLCV, 거래대금 또는 계산 가능한 값 | 일목·ATR·ADX/DI·거래량·거래대금 및 Forward 1/5/10/20일 |
| 날짜별 적격 universe | as_of, permanent_id, 당시 ticker/거래소/security_type, eligible, 사유 | 현재 생존 종목을 과거에 소급하는 bias 방지 |
| 시장 프록시 | KOSPI/KOSDAQ 또는 SPY/QQQ의 완료 일봉 | 기존 시장 방향 로직 재생 |
| 기업행사 정책 | split/dividend event date, factor, raw/adjusted 정의 | 과거 신호와 미래 수익률의 일관성 확보 |
| 식별자 이력 | permanent_id, ticker 유효 시작/종료, 상장/상장폐지일 | ticker 변경·재사용·상장폐지 처리 |

### B. Bias를 줄이기 위해 중요

- 각 날짜의 시가총액·거래대금·업종과 거래정지/관리 상태
- 상장폐지 수익률 또는 마지막 실현 가능 가격
- 거래소 공식 휴장일과 예상 세션
- 원천 데이터의 수정·정정 시각 또는 빈티지
- 당시 일반주식/ADR/SPAC/우선주/ETF/ETN/warrant 구분

### C. 후속 고도화에 필요

- 정규장 60분봉의 timezone, DST, bar 시작/종료, 완료 여부
- `published_at`과 `first_seen_at`이 모두 있는 뉴스/이벤트 archive
- 기초자산별 시장·거시 데이터와 source revision vintage

`evaluate_analyzer.py`가 직접 읽는 가격 CSV 필드는 `timestamp|date|datetime`,
`open,high,low,close,volume`, 선택 `trade_value`이다. 종목 메타데이터는
`symbol,exchange,name|korean_name|english_name,sector,is_etf`를 읽는다. 신뢰도
판정에는 별도로 `date,market,symbol,eligible` universe CSV가 필요하다.

## 2. 국내 데이터 소스

| 제공처 | 범위·OHLCV·거래대금·시총 | universe/상폐/업종/기업행사 | 지수·intraday | API·비용·제한·약관 | PIT·survivorship·연동 |
|---|---|---|---|---|---|
| [KRX OPEN API 서비스 목록](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd) | KOSPI/KOSDAQ 주권 일별매매정보와 종목기본정보, 2010-01-04 이후. 일별 표에서 OHLCV·거래대금·시총을 날짜별로 확보할 수 있는 공식 1순위 후보 | 날짜별 시장 전체 행으로 당시 상장/거래 종목을 재구성할 수 있다. ETF/ETN/ELW는 별도 서비스라 주권과 분리 가능. 정확한 상폐·ticker 이력·기업행사 완전성은 추가 데이터 상품과 대조 필요 | KRX/KOSPI/KOSDAQ 일별 지수 제공. 60분봉은 공개 서비스 목록에서 확인되지 않음 | 회원가입→인증키→관리자 승인→서비스별 활용 승인. [약관](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp)은 비상업 목적, 키당 일 10,000회, 제3자 제공 금지, 계약 종료 후 이용 제한, 화면 출처 표시를 요구 | 날짜별 전 시장 원장을 저장하면 높은 PIT 후보. 단, 정정 빈티지와 상폐 후 수익 처리까지 검증하기 전에는 `완전 survivorship-safe`라 하지 않음. 날짜 단위 bulk adapter 난이도 중간 |
| Kiwoom REST (현재 프로젝트) | 현재 후보와 종목별 최근 최대 650 일봉. 거래량·거래대금 사용 | 현재 master/ranking만 있어 과거 universe와 상폐 전체를 재구성할 수 없음. 기업행사 조정 명세도 연구용으로 불충분 | KOSPI/KOSDAQ 최대 600 일봉, 종목 60분봉 최대 500행 | 승인된 계정 API, 기존 중앙 rate limiter/retry 사용. 계정 약관 범위 확인 필요 | 수집 파이프라인 E2E 표본에는 쉬움. 역사 성능에는 survivorship/selection bias 높음 |
| FinanceDataReader | 종목/지수 가격 접근이 편리하나 현재 KRX listing endpoint가 HTTP 404로 실패함 | 보통 현재 목록 중심이며 완전한 과거 universe·상폐·ticker 이력 보장이 없음 | 일부 지수. 장기 60분봉 부적합 | 오픈소스 라이브러리이나 하위 웹 소스의 약관·변경에 의존 | 탐색/교차검증 보조만. survivor-safe로 표현 불가 |
| pykrx | KRX 가격·시총·거래대금 조회가 편리 | 날짜별 ticker 조회 기능은 유용하지만 비공식 웹 호출 의존성과 상폐/식별자 이력 완전성 검증 필요 | 지수 일봉 가능, 장기 intraday 아님 | 무료 오픈소스 래퍼. KRX 원천 이용조건과 endpoint 안정성에 의존 | KRX 공식 API 결과 대조용. 주 원장으로 쓰기에는 운영·약관 위험 중간 |

국내 선택: KRX OPEN API 승인 키를 받아 날짜별 KOSPI/KOSDAQ 일별매매정보,
종목기본정보, 지수를 2015년부터 수집하는 조합이 현실적이다. 2026-09-09 실행
환경에는 KRX 인증키가 없었고 무인증 과거일 호출은 HTTP 401이었다. 따라서 실제
역사 원장은 수집하지 않았다. 대신 `data_pipeline/krx_open_api.py`에 날짜별 원장을
현재 목록 소급 없이 저장하고 `evaluate_analyzer.py` dataset 형식으로 발행하는
credential-gated adapter를 추가했다. 상폐 실제 사례와 corporate action을 검증하기
전까지 해당 adapter가 만든 manifest는 자동으로 `EXPLORATORY`에 머문다.

## 3. 미국 데이터 소스

| 제공처 | 범위·OHLCV·거래대금·시총 | universe/상폐/업종/기업행사 | 지수·intraday | API·비용·제한·약관 | PIT·survivorship·연동 |
|---|---|---|---|---|---|
| [CRSP via WRDS](https://wrds-www.wharton.upenn.edu/demo/crsp/form/) | NYSE/AMEX/Nasdaq 가격·거래량·shares·holding-period return | PERMNO/PERMCO, name/ticker/exchange history, share code, distributions, split factors, delisting code/price/return을 제공 | CRSP 시장지수. intraday는 이 제품 범위 아님 | 기관 CRSP 구독 및 WRDS 접근 필요, 가격은 기관 계약 | 연구 기준 최상. 상폐수익과 영구식별자로 survivorship bias를 가장 잘 통제. 현재 개인 환경 연동 난이도/비용 높음 |
| [Norgate Data Platinum/Diamond](https://norgatedata.com/stockmarketpackages.php) | 미국 일봉, Platinum은 1990년 이후, Diamond는 1950년 이후 | delisted securities와 historical index constituents 포함. 공급자도 delisted 완전성의 시기별 한계를 별도 명시 | 지수와 progressive hourly snapshots(패키지별) | 개인 구독: Platinum 12개월 USD 630, Diamond USD 787.50(조사일 표시 가격). 전용 updater/plugin 방식 | 개인 연구에서 명시적 survivor-bias 지원이 가장 쉬운 후보. Python/export 및 라이선스 검토 필요, 연동 중간 |
| [Massive/Polygon Stocks](https://polygon.io/stocks) | 전 미국 종목 aggregates. Basic 2년, Starter 5년, Developer 10년, Advanced 20+년. OHLCV; 거래대금은 price×volume 근사 | [`/v3/reference/tickers`](https://polygon.io/docs/rest/crypto/tickers/all-tickers?auth=signup)는 `date`, `active`, `delisted_utc`, exchange/type/FIGI를 제공해 날짜별 목록 후보. [split API](https://polygon.io/docs/rest/stocks/corporate-actions/splits?auth=login) 제공. 시총/업종의 역사 빈티지는 별도 검증 필요 | SPY/QQQ 직접 가능, minute flat files 제공 | 월 USD 0/29/79/199의 개인 플랜, Basic 5 calls/min, 유료 unlimited 표기. 시장데이터 이용약관 준수 필요 | 가격+date reference를 함께 보관하면 현실적 10년 후보. 다만 reference의 과거 완전성, ticker 변경 연결, delisting return을 표본 검증하기 전에는 완전 survivor-safe 아님. REST/flat-file 연동 쉬움~중간 |
| [Tiingo EOD](https://www.tiingo.com/documentation/end-of-day) | 30+년 raw/adjusted OHLCV, `divCash`, `splitFactor`. adjusted는 CRSP 방식으로 split+dividend 반영 | 현재 supported ticker/metadata start/end/exchange. 별도 split API는 존재하지만 과거 전체 universe/상폐 영구식별자 보장은 공식 문서에서 확인되지 않음 | EOD 중심; IEX/intraday 상품 별도 | [가격](https://www.tiingo.com/about/pricing): 무료 500 symbols/month·50/hour·1,000/day, Power USD 30·10,000/hour·100,000/day. 내부사용 라이선스 | 가격/기업행사 보완에는 강함. current supported list만 과거에 소급하면 survivor-safe 아님. 연동 쉬움 |
| [Nasdaq Data Link Sharadar SF1](https://data.nasdaq.com/databases/SF1/documentation) 및 SEP/SFP | SF1은 fundamentals, DAILY, TICKERS, ACTIONS, EVENTS, SP500 보조 테이블과 active/delisted 16,000+ 기업을 설명. EOD price 제품(SEP/SFP)은 별도 entitlement 필요 | listing status·exchange·industry·actions/events 및 delisted coverage 후보. SF1 fundamental은 filing-date PIT 차원이 명시됨 | 지수/분봉은 별도 | Premium, 현재 문서는 기관 문의/로그인 후 가격. Tables API | universe/기업행사 보강 후보이나 EOD 상품의 정확한 survivor-safe 범위와 라이선스를 구매 전 확인해야 함. API 연동 중간 |
| Yahoo/yfinance | 장기 가격 접근이 쉽지만 비공식 라이브러리이며 조정 정책·정정 재현성이 변할 수 있음 | 현재 ticker 중심, 상폐·역사 universe·ticker 이력 불완전 | 일부 지수와 제한적 intraday | 무료처럼 보이나 Yahoo 이용약관과 비공식 endpoint에 의존 | 빠른 탐색/교차확인만. 신뢰 baseline 원장으로 사용하지 않음 |

미국 선택: 기관 접근이 있으면 CRSP가 1순위다. 개인 환경의 현실적 1순위는
Norgate Platinum/Diamond, API 중심 대안은 Massive Developer(2016년 이후 10년)다.
2015년부터 고정하지 않고, 먼저 3~5종목에 대해 상폐·split·ticker 변경을 CRSP 또는
거래소/SEC 기록과 대조해 통과한 기간만 채택한다.

## 4. 뉴스와 이벤트

| 소스 | 역사 범위/필드 | PIT 위험 | 결론 |
|---|---|---|---|
| [Massive News API](https://polygon.io/docs/rest/stocks/news?auth=login) | ticker, title, publisher, URL, `published_utc`, id 등 | 공급자 최초수집시각과 기사 수정 이력이 없으면 `first_seen_at`을 재구성할 수 없음 | 가격 데이터와 함께 후보이나 역사 PIT 완전성 검증 필요 |
| [Tiingo News](https://www.tiingo.com/about/pricing) | 일반 플랜은 queryable history 3개월, enterprise는 최대 15년 문의 | 2015년부터의 개인 역사검증에 부족 | 현재부터 forward paper archive 구축 권장 |
| [GDELT 2.0](https://blog.gdeltproject.org/the-datasets-of-gdelt-as-of-february-2016/) | 2015-02 이후 event/mentions 스트림, 15분 업데이트 | DOC API는 제한된 rolling window이며 기업 관련성·중복·원문 게시시각/최초관측 정확도 검증이 필요 | 글로벌 사건 보조 archive 후보, 종목 뉴스 정답 원장으로 단독 사용 금지 |
| Google News/Naver 현재 검색 | 현재 검색 결과 | 과거 검색결과·순위·수정 상태를 당시 스냅샷처럼 재현할 수 없음 | historical test에 사용 금지. 지금부터 `first_seen_at` 저장 |

뉴스 레코드 계약은 `event_id,title,source,url,published_at,first_seen_at,market,
symbols,sector,category,event_type`로 한다. 충분한 archive가 확보되기 전까지는
`TECHNICAL_BASELINE` 역사검증과 뉴스의 forward paper test를 분리한다.

## 5. Corporate action 정책

저장 계층은 다음 열을 지원하도록 한다.

- `raw_open,raw_high,raw_low,raw_close,raw_volume`: 해당 세션 당시 거래 단위
- `adjusted_open,adjusted_high,adjusted_low,adjusted_close,adjusted_volume`: 명시된 기준일/방식의 조정값
- `split_factor,dividend_cash,adjustment_factor`: 이벤트와 계산 추적용

이 프로젝트에서 `adjustment_factor`는 `adjusted_close / raw_close`인 **가격 배수**로
고정한다. 같은 세션의 adjusted OHLC는 raw OHLC에 동일 배수를 적용해야 하며 품질검사가
이를 확인한다. 거래량 조정 배수는 공급자 정의를 별도 manifest에 기록한다.

신호 계산은 당시 이용 가능했던 raw/as-known 가격을 원칙으로 한다. Forward 수익률은
분할·합병 때문에 생기는 가짜 수익을 제거한 split-adjusted 경로를 사용하고, 배당 포함
total return을 별도 열로 둔다. 오늘 알려진 미래 split factor를 과거 신호 입력에 직접
사용하지 않는다. 공급자가 raw/adjusted를 분리하지 않거나 adjustment vintage를 설명하지
못하면 manifest를 `provider_unspecified`로 두고 신뢰 baseline 승인을 막는다.

## 6. 60분봉 정책

일봉 baseline을 먼저 완료한다. 이후 3~5년 60분봉을 수집할 때는 UTC 원본 timestamp와
`America/New_York`/`Asia/Seoul` 변환값, DST offset, regular-session 여부, bar label이
시작인지 종료인지, 완료 여부를 함께 저장한다. 지수와 종목의 날짜가 다르다는 이유로
임의 forward-fill하지 않는다.

## 6-1. 거래소 달력

품질검사는 단순 평일 대신 `exchange-calendars`의 XNYS/XKRX를 사용한다. 미국
NASDAQ·AMEX의 정규 주식 세션 휴장/조기폐장은 XNYS alias를 사용한다. 패키지
버전과 프로젝트 override 버전을 manifest에 함께 기록한다. 2026-06-03 지방선거와
2026-07-17 제헌절 KRX 휴장은 패키지 4.13.2에 아직 없어 KRX 공지 출처를 가진
`data_pipeline/calendar_overrides.json`으로 보완했다. 새 데이터 기간을 승인할 때는
공급자 전시장 무거래일과 공식 거래소 일정을 다시 대조한다.

## 7. 채택 게이트

전체 수집 전에 시장별 20~30종목과 다음 edge case를 대조한다.

1. 정상 상장 종목, 기간 중 신규상장, 상장폐지, ticker 변경, split/reverse split 각 1개 이상
2. 거래소 달력 대비 누락/비거래일, 중복, OHLC 오류, 45% 이상 gap
3. 공급자 raw/adjusted return과 독립 계산 return의 일치
4. 동일 dataset 재실행 시 hash와 행 수 불변, 중단 후 성공 항목 재호출 없음
5. 날짜별 universe가 상장 전·상폐 후 종목을 제외하고 당시 security type을 보존

이 게이트를 통과하고 manifest가 `survivorship_safe=true`일 때만 결과를
out-of-sample 성능으로 부른다.
