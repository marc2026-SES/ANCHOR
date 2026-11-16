# ANCHOR Shore Dashboard

육상 관제 대시보드입니다. 팀 저장소의 ANCHOR core(`src/anchor`, 이벤트 형식 schemaVersion 2) 구조에 맞춰 만들었습니다. 지금은 **하드코딩한 더미 데이터**로 동작하고, Shore Monitor AE가 붙으면 데이터 소스만 바꾸면 됩니다.

## 실행

```bash
npm install
npm run dev        # http://localhost:5180  (포트는 vite.config.ts에서 변경)
```

빌드: `npm run build` → `dist/` (외부 CDN 없이 오프라인으로 동작, Pretendard 폰트 포함)

## 구조 전제 (팀 core)

```
선박 Pi 5 (ship CSE + core)  ←A→  연안 (edge CSE + Edge 앱)  ←B→  육상 Pi 4 (shore CSE + core)
```

- A(선박↔연안)와 B(연안↔육상)는 따로 연결되고, 동시에 연결돼 있을 필요가 없습니다.
- 데이터는 각 CSE의 `anchor/events` 저널에 같은 이벤트 ID로 저장되고, `anchor/receipts`에 연안 보관 확인(custody)과 육상 최종 확인(final)이 따로 남습니다.
- 대시보드는 이벤트마다 **연안 사본의 저장 시각(coastAt)** 과 **육상 사본의 저장 시각(shoreAt)** 을 씁니다(각 CSE의 CIN `ct`).

## 화면

**항상 보이는 영역** (탭과 상관없이 위에 고정)

| 영역 | 내용 |
| --- | --- |
| 상태 바 | 링크 A·B 상태, 육상 마지막 수신 후 경과, 선박 보관 건수, 연안 대기 건수, ANCHOR / Baseline 전환 |
| 재생 바 | 재생·일시정지, 속도(1·10·30·60배), 시점 이동 |
| 지표 카드 | 육상 최종 확인 비율, 손실, 실시간 지연(A·B 구간), 보관 데이터 지연(긴급 따로), 밀린 데이터 처리 시간, 육상 명령 상태 |

**탭**

| 탭 | 내용 |
| --- | --- |
| Overview | 데이터 연속성(10초당 육상 도착 건수, 선박·연안 대기 건수), 선박·연안·육상에 데이터가 몇 건씩 있는지, 이벤트(링크·기울기 사건·명령·재부팅) |
| Sensors | BRIDGE(roll·pitch), ENGINE(진동 RMS, MQ-2 전압 V), CARGO(온도, 해치) 그래프와 센서 품질(bootId, seq 공백, GPS fix, DHT11, MQ-2 예열). x축 = 노드 생성 시각, 기다렸다 도착한 데이터는 보라색 |
| Measurement | ANCHOR vs Baseline 비교표, 육상 도착 지연 분포, 실행별 CSV 내보내기 |
| Security | bootstrap이 만드는 ACP 목록(자원 이름 = ACP 이름), 거부 테스트 T1~T8, 전송 구간·프라이버시 |

## 측정 지표

| 지표 | 정의 |
| --- | --- |
| 손실 | 끝까지 전달되지 않은 이벤트 수. `(deviceId, bootId)`별 seq 공백으로 확인 (재부팅하면 새 bootId라 공백으로 세지 않음) |
| 실시간 지연 | `shoreAt − generatedAt`, 10초 넘게 기다리지 않은 이벤트. A 구간 = `coastAt − generatedAt`, B 구간 = `shoreAt − coastAt` |
| 보관 데이터 지연 | 같은 계산, 선박이나 연안에서 기다렸던 이벤트 (긴급 = 사건·명령 결과·현장 확인) |
| 밀린 데이터 처리 시간 | 링크가 다시 연결된 시각부터, 그 전에 대기하던 마지막 이벤트가 건너편에 저장될 때까지 |
| 육상 명령 | 육상 발행 → 연안 보관 → 선박 수신 → ESP32 적용 결과(`applied`), 유효시간(TTL) 안에 적용됐는지 |

Baseline은 보관 없이 실시간으로만 전달하는 방식입니다. A나 B가 끊긴 동안 생긴 데이터는 전달되지 않습니다.

## 구조

```
src/
  types.ts                 이벤트(schemaVersion 2) + coastAt/shoreAt, 링크 변화, 명령
  mock/scenario.ts         더미 데이터: README 촬영 순서(A·B를 따로 끊음), 20분, 약 4,000건
  lib/metrics.ts           측정 지표 정의 (실데이터에도 그대로 사용)
  lib/csv.ts               결과 파일 내보내기 (events.csv, links.csv)
  security/acp.ts          bootstrap ACP 목록, 거부 테스트, 전송 구간 보호
  components/              패널·탭별 컴포넌트
  App.tsx                  화면 배치, 탭, 재생 상태
```

## 다음 단계

- [ ] Shore Monitor AE: 육상(·연안) `anchor/events`를 구독하고 CIN `ct`를 `shoreAt`(`coastAt`)으로 저장 → `mock/scenario.ts` 대신 연결
- [ ] 육상 `anchor-events`·`anchor-receipts` ACP에 대시보드 identity 읽기 권한 추가 (시우와 협의)
- [ ] 수조 시험 ANCHOR / Baseline 실측 CSV
- [ ] 거부 테스트 T1~T8 실행 후 Security 탭 결과 반영
