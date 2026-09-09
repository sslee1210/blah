# 무료 뉴스·공시·이벤트 데이터 소스 설계

작성 기준일: 2026-09-09

이 문서는 데이터 수집과 point-in-time 보관 계약만 정의한다. 수집된 자료의 개수,
제목 키워드 또는 GDELT tone은 운영 뉴스 점수, 65/35 종합점수, 등급, 행동 판단,
hard block에 연결하지 않는다.

## 1. 소스별 결론

| 소스 | 실제 제공 | 역사성 | 기업 수준 한계 | 현재 채택 |
|---|---|---|---|---|
| GDELT 2.0 Event | CAMEO 사건, actor, event code, Goldstein, 기사·source 수, AvgTone, 최초 URL | 2015-02-19 이후 15분 bulk | 금융 사건 전용이 아니며 기업명이 actor로 잡히지 않을 수 있음 | 글로벌 사건 보조/연결 연구 |
| GDELT 2.0 Mentions | Event ID별 후속 기사 URL, source, mention 시각, confidence, tone | 2015-02-19 이후 15분 bulk | 독립 사건 수가 아니라 보도 전파량 | 중복 보도·전파 연결 연구 |
| GDELT GKG 2.1 | URL, domain, organization/person/location, themes, tone, GCAM | 2015-02-19 이후 15분 bulk | 제목·본문 없음, 조직 추출 false positive와 누락 존재 | 기업 뉴스 후보 생성, EXPLORATORY |
| Naver News Search API | title, original link, Naver link, description, pubDate | 검색 시점 결과 | 완전한 역사 archive나 최초 노출 시각 보장 없음 | 국내 현재 뉴스+forward snapshot |
| OpenDART | corp code/stock code와 공식 공시 목록·원문 | 과거 접수일 검색 가능 | 목록 API는 접수일만 제공하고 정확한 장중 접수시각을 제공하지 않음 | 국내 공식 기업 사건, PARTIAL |
| SEC EDGAR | CIK/ticker, form, filing date, acceptance datetime, accession, primary document, 8-K items | 최근+분할 history 파일, bulk nightly archive | 현재 ticker map은 ticker 이력 원장이 아니며 8-K item 의미의 추가 검증 필요 | 미국 공식 기업 사건, PARTIAL~높음 |
| Google News RSS 검색 | 현재 검색 결과의 title/link/pubDate/source | 현재 snapshot | 공식 역사 API·완전성·고정 schema 보장 없음 | fail-soft fallback와 forward snapshot만 |

## 2. 공식 근거

### GDELT

- GDELT 2.0은 15분 단위 Event/GKG를 제공하고 Event, Mentions, GKG를 BigQuery와
  bulk CSV로 제공한다: <https://blog.gdeltproject.org/gdelt-2-0-our-global-world-in-realtime/>.
- 2.0 bulk 파일은 2015-02-19 늦은 오전부터 존재한다.
- GKG 2.1 codebook: <https://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf>.
  각 행은 문서 하나이며 publication batch time, source common name, document identifier,
  themes, locations, persons, organizations, tone, GCAM을 가진다. 문서 제목과 본문은 없다.
- Event codebook: <https://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf>.
- master file list: <https://data.gdeltproject.org/gdeltv2/masterfilelist.txt>.

GKG `V2.1DATE`는 원문에서 정밀 추출한 게시시각이라기보다 해당 15분 GKG batch의
publication time이다. `GKGRECORDID` 앞 14자리 batch time을 `first_seen_at`으로
보존한다. GKG tone/GCAM은 기사 문맥의 측정값이지 기업 실적 방향이 아니므로 점수화하지
않는다.

### Naver News Search API

- 공식 문서: <https://developers.naver.com/docs/serviceapi/search/news/news.md>.
- client ID/secret header가 필요하며 `display<=100`, `start<=1000`, `sort=date|sim`이다.
- 반환 필드는 `title`, `originallink`, `link`, `description`, `pubDate`다.
- 문서상 검색 API 일 한도는 25,000회다.

`pubDate`와 실제 수집한 `collected_at`을 함께 저장한다. 과거 게시 기사라도 archive가
수집하기 전의 백테스트 시점에는 strict PIT 입력으로 사용하지 않는다.

### OpenDART

- 공시검색 공식 문서: <https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001>.
- 고유번호 공식 문서: <https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018>.
- `corpCode.xml` ZIP은 `corp_code`, 회사명, `stock_code`, 최종변경일을 제공한다.
- `list.json`은 corp/date/type/page 필터와 접수번호, 법인·종목코드, 보고서명, 접수일을
  제공한다. 정정 포함 여부도 요청할 수 있다.

접수일만 있는 공시는 같은 날 장중에 미리 사용하지 않도록 해당 날짜 23:59:59 KST를
가용 경계로 저장한다. 따라서 일봉 다음 세션부터는 재현 가능하지만 장중 공시 백테스트는
별도의 정확한 접수시각 원장이 없으면 불가능하다. 공시 개수는 긍정/부정 신호가 아니다.

### SEC EDGAR

- 공식 API 설명: <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>.
- 인증키 없이 `data.sec.gov/submissions/CIK##########.json`을 사용할 수 있다.
- 최근 1년 또는 1,000개 이상 filings와 추가 history JSON 파일, current/former name,
  exchange/ticker 정보를 제공한다. 전체 submissions bulk ZIP은 nightly rebuild된다.
- ticker/CIK JSON: <https://www.sec.gov/files/company_tickers.json>.
- 자동 접근은 SEC 정책에 맞는 식별 가능한 User-Agent가 필요하므로 코드가
  `SEC_USER_AGENT`를 요구한다.

`acceptanceDateTime`을 source availability 경계로 보존한다. 8-K item 2.02는 EARNINGS,
2.01은 M&A, 3.02는 CAPITAL_RAISE, 5.02는 MANAGEMENT_CHANGE 후보로 정규화하지만,
해당 item만으로 경제적 방향을 추정하지 않는다. 10-Q/10-K/6-K도 form 그대로 보존한다.

### Google News RSS

Google은 검색 RSS endpoint를 역사 데이터 API로 문서화하지 않는다. Google의 공식
Publisher Center 도움말도 뉴스 노출은 알고리즘으로 구성되고 모든 기사의 게재를
보장하지 않는다고 설명한다:
<https://support.google.com/news/publisher-center/answer/9606634?hl=en>.
따라서 현재 검색 fallback으로만 사용하고 `published_at`과 실제 `collected_at`을 모두
보존한다.

## 3. 공통 이벤트 계약

필수 필드:

- `event_id`, `market`, `symbol`, `company_name`
- source 원형인 `source_event_type`과 공통 taxonomy인 `normalized_event_type`
- 이전 소비자 호환용 `event_type` (`normalized_event_type`과 항상 동일)
- `event_time`, `first_seen_at`, `collected_at`, `event_time_precision`, `pit_trust`
- `source_type`, `source_name`, `source_url`, `title`, `article_id`
- `cluster_id`, `cluster_method`, `query`, `entities`, `is_correction`
- `source_level`(1/2/3), `scope`, `relevance_status`
- `relevance`, `direction`, `confidence`, `raw_reference`

`relevance/direction/confidence`는 현재 `null`이다. source 원문 hash와 source 고유 ID,
공시/filing metadata, GDELT entities/themes/tone은 `raw_reference`에 보존한다.

공통 taxonomy는 `EARNINGS`, `GUIDANCE`, `SUPPLY_CONTRACT`, `M&A`,
`CAPITAL_RAISE`, `BUYBACK`, `DIVIDEND`, `CORPORATE_ACTION`,
`MANAGEMENT_CHANGE`, `LAWSUIT`, `REGULATION`, `PRODUCT`, `TECHNOLOGY`,
`CYBER`, `SANCTION`, `TARIFF`, `INTEREST_RATE`, `FX`, `COMMODITY`,
`GEOPOLITICS`, `OTHER`다. 분류는 관측 자료의 종류를 정규화할 뿐 방향이나 점수를
부여하지 않는다.

소스 신뢰도 계층은 메타데이터다. LEVEL 1은 `OpenDART`/`SEC EDGAR`, LEVEL 2는
`Naver`/`Google News`의 기사 원문 연결, LEVEL 3은 `GDELT` 구조화 후보다. 이 값을
운영 뉴스 점수 가중치로 사용하지 않는다.

scope는 `COMPANY`, `SECTOR`, `MARKET`, `MACRO`, `GLOBAL_EVENT`로 분리한다.
금리·환율과 기업 실적을 한 묶음의 기업 사건으로 취급하지 않는다.

## 4. 저장 구조

```text
data/news/
  raw/{source}/{YYYY}/{MM}/{DD}/{sha256}.{json|xml|zip}
  processed/events/{source}/{YYYY}/{MM}/{batch_sha256}.jsonl
  index/events.sqlite3
  manifests/{collection_id}.json
  forward/analysis/{YYYY}/{MM}/{DD}/{analysis_id}.json
  forward/outcomes/{analysis_id}.json
```

원본은 content-addressed immutable artifact다. processed와 SQLite index는 재생성
가능하다. 완료된 개별 분석 snapshot에는 `analysis_id`, 분석시각, market/symbol/name,
기술점수, 등급, 기술판단, 최종판단과 그 시점에 보인 DART/EDGAR/Naver/Google/GDELT ID가
들어간다. `lineage`는 raw hash → normalized event ID → cluster ID를 연결하고 당시
`market_intelligence` 결과도 별도 layer로 보존한다. 1/5/10/20일 outcome은 원 입력을
고치지 않고 `forward/outcomes`에 별도로 연결한다.

## 5. 중복 처리

1. tracking parameter를 제거한 canonical URL, article ID, 정규화 제목으로 동일 기사 제거.
2. 같은 기업·event type·36시간 안에서 source event ID가 같으면 동일 cluster.
3. source ID가 없으면 제목 token Jaccard, entity, 시간, 제목의 금액·비율 정보를 함께
   사용한다. 금액이 서로 다르면 같은 공급계약처럼 보여도 합치지 않는다.
4. 이 결과는 동일 사건의 **후보**이며 확정 의미 관계가 아니다. 제목이 없는 GDELT GKG는
   Event ID/URL 연결이 없는 한 공격적으로 합치지 않는다.

GDELT GKG의 기업 연결은 ticker 단독 또는 부분문자열을 허용하지 않는다. organization
entity가 회사명/legal alias와 정확히 일치해야 한다. `Apple` 단독 일반명,
`Microsoft Office`, `Samsung SDI`를 각각 Apple Inc., Microsoft Corporation,
삼성전자 직접 사건으로 승격하지 않는다. 통과한 항목도 LEVEL 3 후보이며 GDELT 자체를
기업 뉴스 확정 source로 부르지 않는다.

## 6. Point-in-time 정책

strict 조회는 다음을 모두 만족해야 한다.

```text
event_time <= as_of
first_seen_at is not null
first_seen_at <= as_of
```

- Naver/Google/GDELT DOC forward snapshot: 실제 `collected_at`이 `first_seen_at`.
- GDELT GKG bulk: GKG record batch time이 `first_seen_at`.
- SEC: acceptance datetime이 source availability와 `first_seen_at`.
- OpenDART date-only: 당일 23:59:59 KST의 보수적 가용 경계.
- 역사 검색 결과에 게시시각만 있고 최초 관측시각이 없으면 strict 조회에서 제외한다.

## 7. 역사 백테스트 사용 범위

- **공식 공시 사건**: OpenDART+EDGAR로 2015년 이후 EARNINGS/정기공시/8-K/기업행사
  event-study가 가능하다. OpenDART 당일 장중 시각은 불가해 다음 세션 기준이 안전하다.
- **글로벌 사건**: GDELT Event/Mentions를 15분 bulk로 재구성할 수 있다.
- **기업 뉴스**: GDELT GKG로 후보를 만들 수 있지만 조직명 false positive/누락, 제목·본문
  부재, source coverage 변화 때문에 완전한 기업뉴스 backtest로는 신뢰할 수 없다.
- **현재 뉴스**: Naver/Google을 오늘부터 쌓으면 시간이 지날수록 가장 엄격한 자체
  first-seen archive가 된다.

따라서 GDELT+OpenDART+EDGAR 조합은 무료 **historical event backtest**에는
`PARTIALLY_TRUSTED`, 모든 기업 뉴스를 포괄하는 historical sentiment backtest에는
`EXPLORATORY`다.

## 8. Official Filing Event Study와 가중치 변경 전 조건

공식 공시가 실제로 누적되면 `SEC 8-K/10-Q/10-K`와 DART 주요사항 공시를 독립적으로
1/5/10/20 거래일 성과에 연결할 수 있다. 이는 운영 news score가 아니라
`Official Filing Event Study`로만 부른다. DART date-only 사건은 같은 날 장중 분석에서
제외하고 다음 세션 기준으로 평가한다.

뉴스 가중치는 고정된 임의 표본 수를 채웠다는 이유로 바꾸지 않는다. 다음을 모두 점검한
뒤 날짜별/종목별 cluster bootstrap 또는 신뢰구간의 안정성으로 충분성을 판단한다.

- 독립적인 분석일이 여러 시장 국면을 포함하는가
- 특정 종목·섹터·단일 사건 cluster가 결과를 지배하지 않는가
- Forward 20거래일 outcome이 충분히 완료되었는가
- 동일 사건 중복을 cluster 단위로 통제했는가
- validation 기간의 효과가 rolling 구간에서도 방향과 크기가 안정적인가
- 최종 test는 어떤 가중치·임계값 선택에도 사용하지 않았는가
