# ANCHOR AI — 합성 데이터 & 규칙 기준선

해상 IoT 이상탐지(oneM2M 해커톤 2026)의 AI 파트.
선박 센서의 합성 데이터를 생성하고, ML(Isolation Forest)과 비교할 고정 임계값
규칙 기준선을 평가한다. 프로젝트 전체 맥락은 [CLAUDE.md](CLAUDE.md) 참조.

## 구성

| 파일 | 역할 |
|---|---|
| `generator.py` | 합성 데이터 생성기 + `to_cin_payload(row)` (oneM2M CIN 바디 변환) |
| `baseline_rule.py` | config.yaml 임계값 기반 규칙 탐지 + 평가 |
| `config.yaml` | 규칙 기준선 임계값 |
| `features.py` | 윈도우 특징 추출 — 합성 프로필 30s/5s, 선박 프로필 10s/1s |
| `model_iforest.py` | 서브시스템별 Isolation Forest 학습·점수화 + `to_anomaly_payload(row)` |
| `evaluate.py` | IF vs 규칙 비교 평가 + 그래프 |
| `link_forecast.py` | 항로 전방 LTE 커버리지 이탈/복귀 예측 (+단독 시뮬) |
| `prognosis.py` | 고장 예지: 센서 추세 투영 → 임계값 도달 N분 전 예고 (+단독 시뮬) |
| `route_conflict.py` | CPA/TCPA 경로 겹침 계산 — fleet-ai가 쓰는 순수 계산 모듈, CSE 무관 |
| `tools/fleet_scan.py` | 연안 MEC fleet-ai: 충돌·재해 경고, 태풍·충돌 회피 경로 변경, 항로 이탈 경고 |
| `fleet_route_model.py` | 배별 항로 학습(최근접 이웃) — 항로 이탈 탐지 모델 |
| `tools/train_publish.py` | 재학습 모델을 육상 CSE `modelRegistry`에 발행 (모델 배포 루프) |
| `tools/rebaseline.sh` · `tools/restart_ai.sh` · `tools/deploy_pi.sh` | 선박 Pi: 기준선 재수집 · AE 재시작 · 배포 |
| `tools/deploy_mec.sh` · `tools/restart_fleet.sh` | 연안 MEC 서버: fleet-ai 설치 · 재시작 |
| `onem2m_client.py` | oneM2M(ACME) HTTP 클라이언트 — **모든 oneM2M 호출은 여기에만** |
| `analytics_ae.py` | 분석 AE: 구독 수신 → 윈도우 추론 → anomaly/summary 기록 (+`--replay`) |
| `tools/feed_csv.py` | 센서 시뮬레이터: CSV를 CIN으로 CSE에 주입 (라이브 데모용) |
| `acme/anchor-anomaly.fcp` | anomaly flexContainer specialization (ACME 정책 파일) |
| `requirements.txt` | numpy, pandas, PyYAML, scikit-learn, joblib, matplotlib, requests |

실행 산출물 (git에는 올리지 않음):

| 경로 | 내용 |
|---|---|
| `data/train/train.csv` | 정상만 (label 전부 0) — 학습용 |
| `data/eval/eval.csv` | 이상 포함 (label, anomaly_type) — 평가 전용 |
| `data/eval/events.json` | 주입한 이상 이벤트 원장 (시작/끝/파라미터) |
| `results/baseline.json` | 규칙 기준선 지표 |
| `models/iforest_*.joblib`, `models/model_card.json` | 서브시스템별 모델 + 학습 카드 |
| `results/evaluation.json`, `results/plot_*.png` | IF 지표·규칙 비교 + 유형별 그래프 |
| `results/link_events.jsonl`, `results/replay_link.jsonl` | 링크 예측 이벤트 타임라인 (단독 시뮬 / replay) |

## 설치 & 실행

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python generator.py --hours 12 --seed 42 --anomaly-rate 1.0
python baseline_rule.py
python model_iforest.py        # 서브시스템별 IF 학습 -> models/
python evaluate.py             # 규칙 vs IF 비교 -> results/
```

- `--hours` 분할당 길이(시간; train/eval 각각), `--seed` 난수 시드(재현성),
  `--anomaly-rate` eval 이상 이벤트 비율(건/시간, 유형별 최소 1회 보장, 0이면 주입 안 함)

## 데이터 스키마

CSV 컬럼: `ts, ship_id, subsystem, container, values, label, anomaly_type`

- `ts` ISO8601 UTC, `values`는 JSON 문자열(→ oneM2M CIN `con`에 그대로 대응)
- `label` 0/1, `anomaly_type` ∈ spike | drift | level_shift | freq_change | cargo_temp | hatch_open | ""(정상)
- 행 = (ts, subsystem, container) 샘플 1개

| subsystem/container | 주기 | values |
|---|---|---|
| BRIDGE/position | 1s | `{lat, lon, sog(kn), cog(°)}` |
| BRIDGE/attitude | 1s | `{roll(°), pitch(°)}` |
| ENGINE/vibration | 1s | `{rms}` |
| ENGINE/temperature | 5s | `{temp(°C)}` |
| ENGINE/gas | 5s | `{ppm}` |
| CARGO/climate | 10s | `{temp(°C), humidity(%)}` |
| CARGO/hatch | 이벤트 | `{state: open\|closed}` (상태 변화 시에만 행) |
| ENV/barometric | 30s | `{hpa}` |
| ENV/illuminance | 30s | `{lux}` |

## 합성 데이터 생성 규칙

(CLAUDE.md 규칙에 따라 생성 규칙을 여기에 문서화한다. 모든 난수는
`numpy.random.default_rng(seed)` 계열에서 나오며 같은 옵션이면 결과가 동일하다.)

### 공통

- train은 2026-01-01T00:00Z부터 `--hours`시간: **출항 시나리오** —
  정박(10분, 부하 0.08) → 램프업(20분) → 순항. 정박 중 hatch open→closed 1회는 **정상**.
- eval은 train 종료 직후부터 `--hours`시간: 순항만, 여기에만 이상 주입.
- 잠재 변수 **엔진 부하 load(t)**: 순항 0.70 ± 느린 사인 변동(주기 2h) + 평균회귀 노이즈, [0.05, 0.95] 클립.
- `sog ≈ 14×(load−0.06)` kn (순항 ≈9kn, 정박 ≈0.3kn). 항적(lat/lon)은 sog/cog 적분 —
  부산→쓰시마 동쪽→하카타 방면(방위 ~143°), MEC 파트 시나리오와 같은 해역.
  train은 부산항 출항, eval은 쓰시마 북동에서 시작.

### 정상 패턴

| 스트림 | 모델 |
|---|---|
| roll | 주기 8.5~11.5s(기본 9~11s에서 추출, 느린 요동) 사인파 × 진폭 1.2~4.8°(느린 변동) + N(0, 0.25) |
| pitch | 주기 ~6.5s, 진폭 ~1° 사인파 + N(0, 0.15) |
| vibration | `rms = 0.4 + 1.8×load` + 평균회귀 노이즈(σ0.05) + N(0, 0.06) → 순항 ≈1.66 |
| engine temp | `82 + 6×load(5분 평활)` + 작은 평균회귀 랜덤워크(σ0.6, τ30분) → 순항 ≈86±1°C |
| gas | baseline 5ppm + 평균회귀 노이즈(σ0.7) + N(0, 0.25), 하한 0.3 → 정상 3~8ppm |
| cargo climate | temp 4°C(리퍼 가정) + 평균회귀 랜덤워크(σ0.3); humidity 85% ± σ1.5 |
| barometric | 1013 + 4×sin(주기 12h) + 평균회귀 노이즈 |
| illuminance | 주/야 사이클(06~18시 사인 고도) × 구름 변동 + 야간 ≈5 lux |

### 이상 주입 (eval에만)

시작 시각·지속시간은 seed 고정 난수. 같은 스트림 안에서는 비중첩(간격 120s),
구간 양끝 10분 여유. 이상 효과는 라벨 윈도우 `[start, end)` 안에 들어가도록
램프/감쇠를 포함한다. 이벤트 수 = `max(6, round(hours × anomaly_rate))`,
**6개 유형 각 1회 이상 보장** 후 나머지는 랜덤 유형.

| 유형 | 대상 | 모양 | 지속 | 파라미터 |
|---|---|---|---|---|
| spike | ENGINE/gas | 급등 후 지수 감쇠 | 20~90s | Δ 60~250ppm |
| drift | ENGINE/temperature | 선형 상승(60%)→유지(20%)→회복(20%) | 10~30분 | Δ 10~20°C |
| level_shift | ENGINE/vibration | rms ×1.5~2.2 계단 상승 | 5~20분 | factor |
| freq_change | BRIDGE/attitude | roll 주기를 대역 밖으로(5~6.5s 또는 15~19s) | 5~20분 | target_period_s |
| cargo_temp | CARGO/climate | temp 램프 상승→유지→회복 | 10~30분 | Δ 8~16°C |
| hatch_open | CARGO/hatch | 항해 중 open 발생, 끝에 closed | 3~10분 | — |

라벨: 대상 스트림의 윈도우 내 행에 `label=1, anomaly_type=<유형>`.
hatch는 open 이벤트 행이 라벨 1, 복구 closed 행은 0. 전체 윈도우는
`data/eval/events.json`에 기록된다.

## 규칙 기준선 (baseline_rule.py)

[config.yaml](config.yaml)의 고정 임계값으로 탐지한다. 규칙 ↔ 유형이 1:1이라
유형별 지표를 그대로 산출할 수 있다.

| 규칙 | 임계값(기본) | 대응 유형 |
|---|---|---|
| gas ppm 초과 | 50 | spike |
| engine temp 초과 | 95°C | drift |
| vibration rms 초과 | 2.2 | level_shift |
| roll 주기 대역 이탈 (60s 윈도우 zero-crossing 추정) | 8~12s | freq_change |
| cargo temp 초과 | 8°C | cargo_temp |
| hatch open ∧ sog 초과 (position merge_asof 결합) | 3kn | hatch_open |

평가 (`results/baseline.json`):

- **행 단위** precision / recall / F1 — 해당 스트림에서 알람 vs 라벨
- **이벤트 단위** — events.json 기준 detected/total과
  **탐지 지연(초)** = 이벤트 시작 → 윈도우 내 첫 알람 (mean/median/per_event)
- `overall`은 eval 전체 행 기준 micro 지표

기대 특성: spike/level_shift/cargo_temp/hatch_open은 잘 잡지만, drift는 임계값을
넘을 때까지 **지연이 크고** 램프 초반을 놓친다(행 단위 recall 낮음).
freq_change는 주기 추정 윈도우만큼 지연된다. 이 약점이 ML 모델이 개선해야 할 지점이다.

## ML 모델 (Isolation Forest)

### 특징 (features.py)

슬라이딩 윈도우(합성 프로필 30초/5초 stride, 선박 프로필 10초/1초), 서브시스템별 특징 행렬:

| 서브시스템 | 입력 | 특징 |
|---|---|---|
| ENGINE | vibration.rms(1s), temperature.temp(5s), gas.ppm(5s) | 센서별 mean/std/rms/max/slope (+vibration FFT) |
| BRIDGE | attitude.roll/pitch(1s) | 위와 동일 (+roll FFT) |
| CARGO | climate.temp/humidity(10s), hatch(이벤트) | 위와 동일 + `hatch.open_frac`(윈도우 중 개방 비율) |

- FFT 특징(`domfreq`): rfft 최대 성분을 **포물선 보간**해 빈 해상도(1/30Hz)보다
  정밀하게 추정. 보간 없이는 정상 roll 주기(9~11s)가 전부 0.1Hz 한 빈에 떨어져
  상수가 되고, 상수 특징은 IF가 쓰지 못한다.
- roll·vibration에는 진폭 불변 주파수 특징 `zc_rate`(zero-crossing/초),
  `madr`(평균|Δ|/std)를 추가 — 주기 변화가 여러 특징에서 동시에 드러나야
  IF가 안정적으로 분리한다(단일 특징 이탈은 특징 수에 희석됨).

### 학습 (model_iforest.py)

- 서브시스템별(ENGINE/BRIDGE/CARGO) `IsolationForest(n_estimators=150,
  max_samples=4096)`. **train(정상)만 사용** — 시간순 앞 80% fit, 뒤 20% 검증.
- **경계 증강**: 특징 박스를 ±1.0×range 넓힌 균일 합성점을 5% 섞어 학습.
  IF의 분할값은 학습 데이터 범위 안에서만 생기므로 범위 밖 이상치의 점수가
  포화되는데(범위 밖 둔감), 배경점이 범위 밖 분할을 만들어 이를 해소한다.
  검증 분위수·z-score 통계는 실제 정상 데이터로만 계산.
- **허용오차 증강**(ship 프로필, `features.PROFILES["ship"]["tolerance"]`): 눈금이 거친
  센서는 정상 구간 내내 값이 사실상 상수라(DHT11 온도 30.3~30.4°C, 습도 10% 고정, MQ-2
  0.213V 고정) std·slope가 정확히 0으로만 학습되고, 한 눈금만 오가도 critical이 된다.
  관측 분산이 0일 때 믿을 수 있는 정상 폭은 센서 정확도뿐이므로, 원본 윈도우마다 수준
  이동(±tol)과 윈도우 내 변동(±tol/4)을 mean/std/rms/max/slope에 일관되게 반영한 복제본
  8개를 fit·검증 양쪽에 섞는다. tol: 온도 ±2°C·습도 ±5%RH(DHT11 데이터시트), 가스
  ±0.05V, 자세각 ±1°(정지 상태 IMU 드리프트). 합성 프로필은 tolerance가 없어 영향 없음.
- **충격 윈도우 제외**(ship 프로필 `trim`): 실측 '정상' 수집분에 섞인 순간 충격이 검증
  구간에 하나만 들어가도 q99.5 기준이 그 수준으로 올라가 탐지가 무뎌진다. 전체 윈도우로
  한 번 적합해 원점수가 중앙값 + 3×MAD를 넘는 윈도우를 빼고 다시 학습한다. (5×MAD로는
  사람이 2~3분마다 건드린 13분 구간에서 충격이 남아 ENGINE이 진동 40배에도 무반응이었다.)
- 점수 정규화(0~1): `score01 = clip((raw − median_val) / (3×(q99.5_val − median_val)), 0, 1)`
  (raw = −score_samples). **warn=1/3**은 검증 정상의 상위 0.5% 지점, **critical=2/3**.
- 근거: fit 분포 기준 z-score 절대값 상위 3개 → `topFeatures`.
  출력은 CLAUDE.md anomaly flexContainer
  `{subsystem, level, score, topFeatures, windowStart, windowEnd, srcContainers}` 대응
  (`to_anomaly_payload(row)`).
- 추론 비용: 윈도우당 ~5ms(M대 맥 기준, model_card.json에 기록) — RPi5 50ms 예산 내.

### 평가 (evaluate.py)

- 윈도우 단위 유형별 P/R/F1(같은 서브시스템의 다른 유형 윈도우는 제외하고 계산)과
  이벤트 단위 탐지율·지연(초, 탐지 시각 = 알람 윈도우의 끝)을 계산해
  `results/baseline.json`과 비교. 규칙은 행 단위, IF는 윈도우 단위라 F1 절대값은
  참고용이고 **이벤트 탐지율·지연이 직접 비교 지표**.
- `results/evaluation.json` + 유형별 `results/plot_<type>.png`
  (시계열 + 점수 + 규칙/IF 탐지 시점).
- 관측된 특성(seed 42, 12h): drift·cargo_temp는 IF가 수백 초 먼저 탐지(slope 특징),
  freq_change도 IF가 탐지. hatch_open은 '항해 중'이라는 맥락(sog)이 CARGO 특징에
  없어 규칙(sog 결합)이 우세 — 규칙과 ML이 상호 보완하는 구조.

## oneM2M 연동 (analytics_ae.py + onem2m_client.py)

분석 AE 동작 (ACME CSE, 설정은 전부 [config.yaml](config.yaml)의 `onem2m:`):

1. 시작 시 AE 등록(`anchorAI`), **anomaly flexContainer**(cnd `edu.anchor.anomaly`)와
   **summary 컨테이너** 생성 — 이미 있으면 재사용
2. `onem2m.sensors`의 센서 컨테이너마다 subscription 생성(없으면 10초마다 재시도),
   로컬 HTTP 서버(`notification.host:port`)로 notification 수신
3. 윈도우가 차면 추론 → anomaly flexContainer **UPDATE** + 같은 내용을
   `anomaly/history` 컨테이너 CIN으로도 기록(`anomaly.history_rn`)
   (`anomaly.skip_normal: true`면 normal은 기록 생략, `--record-normal`로 무시 가능)
4. 1분(`summary.period_s`)마다 센서별 mean/max/min summary CIN 생성 (데이터 시각 기준)
5. flexContainer 생성이 거부되면(`acme/anchor-anomaly.fcp` 미등록 등) container+CIN으로
   자동 대체 (`anomaly.fallback_container`)
6. `BRIDGE/position` 구독은 링크 예측 전용 — 이벤트를 `linkForecast` 컨테이너에 CIN으로 기록
7. 어느 서브시스템이든 critical 진입 시 알람 group에 fanOut("on"), 전원 해제되면 "off"
8. `onem2m.poll.enabled: true`면 구독 대신 **폴링 수집** — 구독 권한이 없는 팀 CSE에
   읽기 전용으로 붙을 때 사용 ([config.live.yaml](config.live.yaml): 선박 CSE 직결).
   센서 읽기는 `poll.read_originator`(선박 CSE는 `CAnchorAgent`), 결과 쓰기는 `ae.originator`.
   - `poll.mode: latest`(기본): 컨테이너별 최신 CIN(`<cnt>/la`) 1건을 센서 주기의 0.7배
     간격으로 읽고 `ct`로 중복 제거. 요청 비용이 누적 CIN 수와 무관하다.
   - `poll.mode: discovery`: CIN 경로 전체 디스커버리 차집합. 빠짐은 없지만 비용이 누적
     CIN 수에 비례한다 — `mni` 없는 컨테이너에 3천 건이 쌓이자 요청당 5초가 걸려 타임아웃이
     났다(2026-11-19 선박 CSE). 상시 운용에는 쓰지 않는다.
9. `onem2m.status` 설정 시 `aiStatus` 컨테이너에 주기(30s) 하트비트 CIN
   \{state, profile, windows, levels, sirenOn\} — 대시보드의 "AI 연결 상태" 표시용

모든 oneM2M HTTP(X-M2M-Origin/RI/RVI 헤더, `application/json;ty=<타입>`)는
`onem2m_client.py` 한 곳에만 있다(CLAUDE.md 규칙). `generator.to_cin_payload(row)`는
CSV 행 → CIN 바디, `model_iforest.to_anomaly_payload(row)`는 스코어 행 → anomaly
flexContainer 속성으로 변환한다.

### 팀 연동 규약 (oneM2M core 파트)

육상 전송·누락 복구는 oneM2M core 파트(ship/shore/edge CSE + core 앱) 담당이라,
AI 산출물은 그쪽 파이프라인이 바로 집계할 수 있게 다음 규약을 따른다:

- **eventId**: 모든 산출 CIN(anomaly history·summary·linkForecast)에 결정적 ID 포함 —
  `anm-{서브시스템}-{windowEnd}`, `sum-{windowEnd}`, `lnk-{ts}`. 같은 창을 재전송해도
  같은 ID라, 누락 복구의 eventId·본문 대조(같으면 중복, 다르면 충돌)가 그대로 동작한다.
- **anomaly = FCNT(최신 상태) + history CIN(이력)**: core 파트의 "현재 상태 FCNT +
  이력 컨테이너" 패턴과 동일. FCNT UPDATE는 CIN이 안 남아 eventId 동기화에 안 잡히므로
  이력·재전송은 `anomaly/history`가 담당한다.
- **routeMode**: linkForecast 이벤트에 경로 선택용 모드 포함 — `coastal`(연안 LTE,
  edge 경유) / `opensea`(공해, VSAT 직접). core 파트 '경로 선택'의 현재 시뮬레이터
  입력을 이 컨테이너 구독으로 대체할 수 있다. 음영(원격 중지) 판정은 미모델링.
- 재전송 우선순위 제안: anomaly(critical>warn) > summary > 원시 — level은 CIN 본문에 있다.

core 파트에서 받아야 확정되는 것: ship CSE 실제 리소스 트리(센서 경로·`history` 위치,
CSE-ID/RN·포트), 측정 CIN 본문 스키마(eventId 형식 합의), 명령 JSON 포맷과
fan·buzzer·safetyGroup 경로(`grpAlarm`과 통합 여부), CAnchorAgent용 ACP(특히 position
읽기 — 위치 조회 제한 중), edge 중계 방식(Mcc 등록 체인 vs 앱 프록시).

### 링크(통신) 예측 — link_forecast.py

합성 연안 LTE 커버리지(config.yaml `link_forecast.lte_cells`, 원형 셀) 기준으로
위치·침로·속력을 직선 투영해 "horizon 안에 커버리지 이탈/복귀"를 예측한다.
이벤트는 loss_predicted(이탈 warn_min 안) / lte_lost / lte_restored — 끊기기 **전에**
밀린 summary를 flush(batchNotify)하고 VSAT 모드로 전환하는 판단 근거가 된다.
모든 이벤트에 `eventId`·`routeMode`(coastal/opensea)가 들어간다 (팀 연동 규약 참조).
셀 배치는 MEC 파트의 대한해협 시나리오(anchor-korea-strait)와 같은 해역 —
부산·거제 LTE-M(r45km) + 일본 연안 LTE 5(쓰시마 남·북, 이키, 후쿠오카, 시모노세키).
좌표는 실제 지리 근사값이고, MEC 시나리오(scenario/gen_anchor.py)의 실제 셀 좌표를
받으면 교체한다.

단독 시뮬 `python link_forecast.py` (seed 42 기준: 12:05 경고 → 12:15 tsushima-north
이탈 → 16:30 fukuoka 복귀 → 21:48 재이탈(정박 미모델링: 하카타 통과 후 내륙 방향))
→ `results/link_events.jsonl`.

### 고장 예지 — prognosis.py

규칙이 "임계값을 넘었다", IF가 "패턴이 평소와 다르다"를 알린다면, 예지는
"**이 추세면 N분 뒤 임계값 도달**"을 미리 알린다. 채널별 최근 trend_min(5분)
샘플에 최소제곱 직선을 맞춰 ETA를 계산하고, [min_eta_s, warn_min] 안이면
threshold_predicted 이벤트 1회(결정적 eventId) → `anchorAI/prognosis` 컨테이너 CIN.
대상 4채널(ENGINE temp·gas·vibration, CARGO temp)의 임계값은 config.yaml
`thresholds` 섹션을 참조한다(중복 정의 금지). 급변(spike)은 min_eta_s에 걸러진다 —
그건 규칙/IF 몫이고, 예지는 drift처럼 서서히 오르는 이상에서 빛난다
(seed 42: 12:18:45에 "95°C까지 577초" 예고 — 규칙 알람 도달보다 수 분 먼저).

단독 시뮬 `python prognosis.py` → 12h에서 13 이벤트(4채널 전부 발화),
`results/prognosis_events.jsonl`.

### 사이렌 group 제어 (analytics_ae)

`onem2m.actuators` 설정 시 분석 AE가 알람 group(`grpAlarm`, 멤버: `ALARM/siren`,
`ALARM/beacon`)을 만들고, 어느 서브시스템이든 `on_level`(기본 critical) 진입 시
`<group>/fopt`에 CIN을 한 번 CREATE → oneM2M fanOut이 모든 멤버 컨테이너에
`{"cmd":"on", reason...}`을 복제한다. 전부 해제되면 "off". 멤버 컨테이너와 ACP는
feed_csv.py가 센서 트리와 함께 준비한다 (실제 선박에서는 ESP32 alarm 노드가 구독).

### 경로 겹침 경고 — route_conflict.py (anchor-fleet 이식용)

자선·타선의 위치/속력/침로로 CPA(최근접 거리)·TCPA(도달 시간)를 계산해
`cpa<0.5nm ∧ 0<tcpa<20min`이면 warn, `cpa<0.25nm ∧ tcpa<10min`이면 danger.
연안 MEC 앱(anchor-fleet)이 MEC013 위치로 호출할 순수 계산 모듈이라 CSE 의존이
없다. 데모: `python route_conflict.py` (교차 조우 시나리오 타임라인).

### 실데이터 모드 — ship 프로필 (config.ship.yaml)

팀 실물(ESP32 3노드 → ship CSE → 육상 복제)의 센서 구성은 합성과 다르다 —
ENGINE 온도 센서 없음, 가스는 MQ-2 **전압**(2s), climate 5s, 주기 흔들림 ≈1.05s.
features.py의 센서 **프로필**(`sensor_profile: ship`)로 전환하며, 윈도우는 격자
보간으로 채우고(tolerant) ts는 epoch 밀리초를 받는다. 모델은 `models-ship/`에 분리.

```bash
python tools/collect_live.py --minutes 30     # 선박 CSE에서 읽기 전용 수집 (Tailscale)
python tools/live_to_csv.py --last-min 90     # 연속 구간만 위상 정렬 격자 CSV로
python model_iforest.py train --profile ship --csv data/ship/train.csv --models-dir models-ship
python analytics_ae.py --config config.ship.yaml --replay data/ship/train.csv
```

2026-11-16 책상 위 43분 실데이터 검증: 1,526 윈도우(미완성 0), 경보는 장비
부팅·손대기 구간(06:30~34, 06:37~42)에 군집하고 정지 구간 오경보 ≈0.3%.
육상 CSE 라이브 부착은 ACP(CAnchorAgent) 승인 후 `--config config.ship.yaml`로.
육상 준비 전엔 **선박 CSE 직결 폴링** `--config config.live.yaml` — 구독 권한 없이
읽기 전용으로 붙어 `cse-ship/anchorAI/` 아래에 AI 결과를 쓴다 (대시보드 연동).

**창 길이·간격 (선박 프로필 10초/1초).** 합성 프로필은 평가 수치의 기준이라 30초/5초 그대로다.
선박은 2026-11-19 기준선(13분, 사람이 2~3분마다 테이블을 건드린 환경)으로 후보를 비교해 정했다:

| 창 / 간격 | 진동 warn · critical | 자세각 warn · critical | 비고 |
|---|---|---|---|
| 30s / 5s | 5배 · 20배 | +1.5° · +2° | 이전 설정 |
| 30s / 1s | 12배 · 도달 못 함 | +1.5° · +1.5° | 충격 하나가 창 30개를 오염시켜 오히려 둔해짐 |
| 20s / 1s | 3배 · 도달 못 함 | +1.5° · +1.5° | |
| 15s / 1s | — | — | 가스 2s·온습도 5s 주기의 공배수가 아니라 계산 불가 |
| **10s / 1s** | **2배 · 2배** | +1.5° · +2° | 채택 — 짧은 충격에 민감, 경보는 10초 뒤 해제 |

10초/1초 모델의 나머지 경계: 온도 ±2°C 이내 normal · +2.5°C warn · +3.5°C critical /
가스 +0.1V warn. 기준선을 다시 잡으면 경계도 달라진다. 모델에는 학습 때의 창·간격이
기록되고, 프로필과 다른 모델은 기동 시와 `modelRegistry` 핫스왑 시 모두 거부한다.
1초 간격 추론이 CSE에 초당 쓰기를 쏟지 않도록 anomaly는 등급이 바뀔 때 즉시, 같은 등급이
이어지는 동안은 5초마다 기록한다.

기준선 재수집은 Pi에서 `bash tools/rebaseline.sh [분]` — 수집 동안 CSE에 요청하지 않고 끝난 뒤
컨테이너당 1회 조회(`collect_live.py --batch`)한다. 결과는 `models-ship.new`에만 쓰므로 수집
구간에 배가 움직였는지(분 단위 자세·진동 범위) 확인한 뒤 교체한다.

선박 Pi에서 AE 재시작은 `bash ~/anchor-ai/tools/restart_ai.sh` — ssh 한 줄에 `pkill -f`와
기동 명령을 함께 쓰면 그 셸까지 죽는다.

### 연안 MEC — fleet-ai (tools/fleet_scan.py · fleet_route_model.py)

배 한 척이 못 보는 선단 전체를 연안 MEC 서버에서 본다. MEC013 위치 API를 2초마다 읽고
결과를 연안 CSE `cse-edge/fleetAI/`의 `fleetStatus`(30초)·`conflicts`(경고)로 발행한다.
`conflicts` CIN은 `type`으로 구분한다.

| type | 판단 | 학습 |
|---|---|---|
| `fleet_conflict` | 두 배의 직선 투영 CPA/TCPA — warn 0.5nm·20분, danger 0.25nm·10분 | 없음 |
| `hazard_warning` | 지도 서버 재해 목록: 태풍=이동하는 원, 해일=퍼지는 파면과 만나는 시각. 구역 안·30분 전 danger, 2시간 전 warn (시나리오 시간) | 없음 |
| `route_deviation` | 과거 항적(위치·침로)에서 가장 가까운 지점까지 거리 ≥1km warn, ≥3km danger, 연속 3회 | 최근접 이웃 |
| `conflict_avoidance` | 아래 조건에서 경로 변경을 실행했다는 기록 | — |

**항로 이탈에 Isolation Forest를 쓰지 않은 이유.** 시나리오 경로로 만든 가상 항적에서 경유점을
지나쳐 직진하는 이탈을 시험하면, IF(위치+침로, 배경점 증강)는 길에서 0.5~12km(최악 34km)
벗어난 뒤에야 warn을 냈고 2건을 놓쳤다. 축에 나란히만 자르는 분할로는 대각선으로 뻗은 가느다란
항로를 감싸지 못한다. 과거 항적까지의 최근접 거리는 0.3~0.9km(최악 2.3km), 놓침 0건, 정상
오경보 0~1.1%였다. 학습은 `--track-log`로 쌓인 정상 항적으로 하고(재해 중·대피 중·복귀 직후의
배는 기록 제외), 한 바퀴를 못 채운 배는 학습을 보류한다.

**경로 변경은 두 경우에만 한다** (`--reroute`, 지도 서버 `POST /api/ships/<배>/route`).
- 태풍: 구역에 들었거나 들어갈 배를 진로 옆 바다로 보낸다. 육지에 막히는 후보는 건너뛴다.
- 충돌: 최근접 0.2nm·8분 이내가 2회 연속이고, **둘 중 한 척만 학습된 항로를 벗어나 있을 때**
  항로를 지키는 배를 상대 뒤쪽으로 비키게 한다. 2026-11-19 실측에서 45분 동안 danger가 7번
  떴지만 실제로 0.25nm 안으로 접근한 쌍은 없었다(최소 0.43nm) — 배들이 경유점에서 방향을 틀어
  직선 가정이 빗나간 것이라, danger만으로 경로를 바꾸면 전부 불필요한 변경이 된다.

재해가 끝나거나 상대와 3nm 벌어지면 원래 경로·속력으로 되돌린다. 지도 API가 현재 위치를 맨 앞에
붙이므로 복귀한 경로는 경유점이 하나 늘어난다. AI가 경로를 건드린 배는 재학습 전까지 이탈 판정을
보류한다.

```bash
tools/deploy_mec.sh <계정>@<MEC 서버>          # 설치·자체 테스트·재부팅 자동 시작·기동
python fleet_route_model.py status              # (MEC에서) 배별 항적 수집 현황
python fleet_route_model.py train               # 항로 재학습 — 스캐너가 재시작 없이 다시 읽음
python tools/fleet_scan.py --selftest           # 네트워크 없이 로직 검증
```

검증 범위: 경고·태풍 회피·복귀·항로 이탈 탐지는 실제 샌드박스에서 확인했다. 충돌 회피는 판단
로직과 경로 변경·복귀 흐름을 `--selftest`(지도 서버를 흉내 낸 객체)로 검증했고, 실제 충돌
상황에서의 경로 변경은 재현하지 못했다(20배속 시뮬레이션에서 0.2nm 이내 접근을 만들지 못함).

### 테스트 ① — CSE 없이 (replay)

```bash
python analytics_ae.py --replay data/eval --max-hours 1
```

eval.csv를 notification처럼 시간순으로 흘려 같은 파이프라인을 태운다.
결과: `results/replay_anomalies.jsonl`, `results/replay_summary.jsonl`.
(drift 주입 32초 만에 warn 전환 — 배치 평가(evaluate.py)와 동일한 지연)

### 테스트 ② — 로컬 ACME 라이브

```bash
# 1) ACME 설치·기동 (별도 터미널)
pip install acmecse
cp acme/anchor-anomaly.fcp <ACME의 init 디렉터리>/   # flexContainer 정책 등록
acmecse --headless    # 기본 http://localhost:8080, cse-in

# 2) 분석 AE
python analytics_ae.py

# 3) 센서 시뮬레이터로 데모 구간 주입 (별도 터미널)
#    12:04~12:22 한 창에 링크 예고(12:05)→이탈(12:15)→drift warn(12:16:45)
#    →critical+사이렌 fanOut(12:18:10)→예지(12:18:45, 95°C까지 577s)가 모두 들어간다
python tools/feed_csv.py --from-ts 2026-01-01T12:04:00Z --minutes 18
```

feed_csv.py는 센서 AE(`SHIP-01`)와 컨테이너 트리를 만들고, 분석 AE originator에
권한을 주는 ACP를 걸어둔 뒤(구독 생성에 대상 리소스 권한 필요) CIN을 주입한다.
확인: `GET {cse}/cse-in/anchorAI/anomaly` → 최신 이상 상태,
`GET {cse}/cse-in/anchorAI/anomaly/history/la` → 최신 이상 이력 CIN(eventId 포함),
`GET {cse}/cse-in/anchorAI/summary/la` → 최신 1분 요약,
`GET {cse}/cse-in/anchorAI/linkForecast/la` → 최신 링크 이벤트,
`GET {cse}/cse-in/anchorAI/prognosis/la` → 최신 예지 이벤트,
`GET {cse}/cse-in/anchorAI/aiStatus/la` → AI 상태 하트비트,
`GET {cse}/cse-in/SHIP-01/ALARM/siren/la` → 사이렌 상태.
