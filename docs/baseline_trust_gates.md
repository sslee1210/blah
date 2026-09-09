# TECHNICAL_BASELINE 신뢰 게이트

`trusted_baseline_available`은 수익률이 좋아 보이는지와 무관하다. 데이터가 아래
재현성 요건을 통과했는지만 판정한다. 구현 위치는
`evaluation/point_in_time.py::_metadata`, `_finalize_trust`와
`evaluate_analyzer.py::_manifest_flags`다.

## 차단 요인 우선순위

| 우선순위 | 게이트/코드 필드 | 왜 필요한가 | historical_sample 부족 | 해결 데이터 | 해결 가능성 |
|---|---|---|---|---|---|
| P0 | 역사 universe / `dataset_point_in_time_verified` | 현재 생존 종목을 과거에 소급하면 등급·ranking 모두 생존편향 | 수집일 1일의 현재 rank만 있음 | 날짜별 permanent_id, ticker, 거래소, security_type, eligibility | 국내 KRX 날짜별 원장으로 부분~높음; 미국 CRSP/Norgate로 높음, 무료는 제한적 |
| P0 | 상장폐지 / `delisted_securities_included` | 실패한 기업과 상폐 직전 손실을 빼면 성과가 구조적으로 부풀려짐 | 상폐 종목 0, 상폐수익 0 | 상장/상폐일, 마지막 거래 가능 가격, delisting return | CRSP는 높음; Norgate/날짜별 KRX는 부분~높음; 무료 미국은 불완전 |
| P0 | 기업행사 / `price_policy_verified` | split 때문에 가짜 ±50~100% 수익이 생기고 미래 조정계수가 과거 신호를 바꿀 수 있음 | Kiwoom `upd_stkpc_tp=1` 외 raw/adjusted/factor 명세 없음 | raw/as-known OHLC, split event/factor, outcome용 adjusted 경로, 검증 표본 | 공급자별 가능. Kiwoom 표본만으로는 불가 |
| P0 | 독립 분석일 / `independent_dates_sufficient` | 20일 outcome과 purge/embargo 후 Train/Validation/Test가 모두 있어야 OOS 평가 가능 | 미국 43, 국내 34 분석일; 요구 100일 | 더 긴 역사와 동일 규칙의 날짜별 universe | 장기 원천 확보 시 가능 |
| P1 | 시장 프록시 / `market_proxies` | 기존 판단이 SPY/QQQ 또는 KOSPI/KOSDAQ 방향을 사용 | 최근 프록시는 있으나 장기 동일 기간 아님 | 같은 공급자·세션·cutoff의 장기 프록시 | 가능 |
| P1 | 거래소 달력 / `exchange_calendar_verified` | 휴장과 결측, 미국 조기폐장/DST를 구분 | 초기 평일 추정에서 개선됨 | 버전형 XNYS/XKRX + 공식 변경 override | 현재 표본은 적용 완료. 새 기간마다 공식 일정 대조 필요 |
| P1 | ticker/permanent ID | ticker 변경·재사용 시 서로 다른 증권을 합치지 않기 위해 필요 | 영구식별자/이력 없음 | PERMNO/FIGI 또는 KRX 표준코드와 유효기간 | 유료 미국은 가능; 국내는 추가 기본/행사 자료 필요 |
| P1 | 당시 security type/거래상태 | ETF·ADR·SPAC·우선주·정지종목을 당시 기준으로 제외해야 함 | 현재 master 분류뿐 | 날짜별 type, exchange, halt/management state | 부분 해결 가능; 공급자별 검증 필요 |
| P2 | 뉴스 archive / `intelligence_complete` | FULL_CONTEXT만 재현 | 없음 | `published_at`+`first_seen_at` 역사 snapshot | Technical에는 불필요; Full은 현재 불가 |
| P2 | 60분봉 | 진입 타이밍 ablation | 최대 500행뿐 | 3~5년 정규장 timezone-aware 완료 60분봉 | 유료 minute source로 가능 |

## 신뢰도 상태

| 상태 | 의미 | 허용되는 사용 |
|---|---|---|
| `TRUSTED` (LEVEL A) | 역사 universe, 상폐, 기업행사, 시장 프록시, 실제 캘린더, 충분한 독립일이 모두 통과. FULL_CONTEXT는 뉴스도 통과 | 보존된 최종 Test의 OOS Baseline 보고 가능 |
| `PARTIALLY_TRUSTED` (LEVEL B) | 핵심 universe·상폐·기업행사는 통과했지만 달력 공식 대조 또는 독립 기간 같은 P1/기간 게이트가 남음 | Validation 실험은 가능하나 최종 성능 주장·알고리즘 변경 근거로 단독 사용 금지 |
| `EXPLORATORY` (LEVEL C) | 현재 생존 종목, 상폐 부재, 기업행사 불명 중 하나 이상 | 파이프라인/기능 smoke test만 가능 |
| `NOT_EVALUABLE` | 가격·프록시·신호 또는 Forward 결과 자체가 없음 | 성능표 생성 금지 |

현재 `historical_sample`은 manifest의 `purpose=PIPELINE_VALIDATION_ONLY`,
`trust_level=EXPLORATORY`로 고정했다. 신뢰 데이터셋과 자동 병합하지 않는다.
