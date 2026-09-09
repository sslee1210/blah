# Point-in-time 평가 데이터 형식

`evaluate_analyzer.py`는 운영 분석 규칙을 변경하지 않고 과거 시점별로
기존 함수를 다시 호출한다. 캐시를 그대로 읽거나, 아래 구조의 별도 데이터셋을
`--dataset-root`로 지정하거나, manifest가 있는 관리 데이터셋을 `--dataset`으로
지정할 수 있다.

```text
dataset/
  US/
    stocks.csv
    stocks/AAPL.csv
    proxies/SPY.csv
    proxies/QQQ.csv
  KR/
    stocks.csv
    stocks/005930.csv
    proxies/KOSPI.csv
    proxies/KOSDAQ.csv
```

가격 CSV에는 `timestamp`(또는 `date`, `datetime`)와
`open, high, low, close, volume` 열이 필요하다. `trade_value`가 없으면
종가×거래량으로 계산된다. 수정주가 여부와 조정 정책은 전 종목·전 기간에 걸쳐
동일해야 한다.

원천이 두 가격 계열을 제공하면 `raw_open/raw_high/raw_low/raw_close`는 신호 계산에,
`adjusted_open/adjusted_high/adjusted_low/adjusted_close`는 Forward 성과 계산에만
사용한다. `raw_volume`, `adjusted_volume`도 선택적으로 지원한다. 이 분리는 오늘
알려진 분할계수로 과거 신호 가격을 바꾸는 일을 막기 위한 것이다.

신뢰 가능한 평가에는 다음 두 입력도 필요하다.

- `--universe-snapshots`: `date,market,symbol,eligible` 열의 CSV. 각 평가일에
  당시 실제 후보였던 종목을 기록해야 하며 상장폐지 종목을 포함해야 한다.
- `--intelligence-snapshots`: 한 줄에 한 JSON 객체인 JSONL. 객체는
  `as_of`, `market`, `symbol`과 `MarketIntelligence` 필드를 가진다. 공통 시장
  스냅샷은 `symbol`을 `*`로 둔다. 게시시각뿐 아니라 최초 수집시각 기준으로
  그 분석 시점에 이용 가능했던 기사만 포함해야 한다.

예시:

```powershell
py -3 evaluate_analyzer.py --market BOTH --dataset-root D:\pit-stock-data `
  --universe-snapshots D:\pit-stock-data\universe.csv `
  --intelligence-snapshots D:\pit-stock-data\intelligence.jsonl
```

프로젝트 관리 데이터셋은 다음처럼 실행한다.

```powershell
py -3 evaluate_analyzer.py --market KR --dataset historical `
  --baseline-mode TECHNICAL_BASELINE
```

`TECHNICAL_BASELINE`은 가격·당시 universe·시장지수까지만 요구하며 뉴스 부재 때문에
실패시키지 않는다. `FULL_CONTEXT_BASELINE`은 여기에 point-in-time 뉴스·이벤트
스냅샷을 추가로 요구한다. 단순히 universe CSV가 존재한다고 신뢰하지 않고,
`data/metadata/manifests/<dataset>.json`의 survivorship 및 기업행사 정책 검증 상태까지
통과해야 `trusted_baseline_available=true`가 된다.

신뢰 상태는 `TRUSTED`, `PARTIALLY_TRUSTED`, `EXPLORATORY`, `NOT_EVALUABLE`로
나뉜다. 정의와 P0/P1/P2 차단 조건은 `docs/baseline_trust_gates.md`에 있다.

최종 Test는 결과 보고에만 사용한다. 이 도구는 Test를 사용해 임계값이나
가중치를 선택하지 않는다. Forward 20일 outcome 때문에 split 사이에는 기본
20거래일 purge 구간을 둔다.

## 데이터 수집 기반

```powershell
py -3 collect_historical_data.py --market BOTH --sample-size 20 `
  --dataset historical_sample
```

이 명령의 Kiwoom 표본은 수집→content hash 원본 저장→품질검사→평가 adapter의 E2E
검증용이다. 현재 master/ranking을 과거에 소급하므로 신뢰할 수 있는 역사 성능에
사용하면 안 된다. 전체 소스 비교와 채택 조건은
`docs/historical_data_sources.md`에 있다.

## KRX OPEN API adapter

승인된 `KRX_AUTH_KEY`가 있는 경우 날짜별 KOSPI/KOSDAQ 주권 일별매매정보,
종목기본정보와 대표지수를 기존 evaluation dataset 계약으로 변환할 수 있다.

```powershell
py -3 collect_historical_data.py --source krx-open-api --market KR `
  --dataset krx_historical --start-date 2015-01-02 --end-date 2025-12-30
```

수집기는 현재 종목목록을 과거에 소급하지 않고 각 `basDd` 응답으로 universe를 만든다.
원본 JSON은 content hash와 함께 별도 보존하며, `universe.csv`에는 표준코드,
종목코드, 시장, 주식종류, 상장일, 거래대금과 날짜별 거래대금 순위를 남긴다.
다만 KRX 일별 원천만으로 split-adjusted outcome과 상장폐지 수익을 확정할 수 없으므로
실제 edge-case 대조 전 manifest는 `EXPLORATORY`이고 성능 해석에 사용할 수 없다.
