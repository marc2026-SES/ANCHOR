# ANCHOR · Hardware part

> 통합 실행은 [프로젝트 README](../README.md)를 따른다. 아래는 기존 하드웨어 구축 기록이다. 현재 동기화는 선박·연안·육상 core로 교체했으며, 구 FIFO 동기화기를 함께 실행하지 않는다.

**A**daptive **N**etwork **C**ontinuity **H**ub for **O**cean **R**esilience. 팀 Ctrl+Sea, oneM2M Hackathon 2026.

선박(RC 보트)의 센서 데이터를 선박 안의 oneM2M MN-CSE에 모으고, 육상 IN-CSE로 복제한다. 선박과 육상 사이 링크가 끊겨도 선박은 혼자 계속 돌고, 끊긴 동안 쌓인 데이터는 링크가 돌아오면 빠짐없이 육상으로 넘어간다. 이 폴더는 그 하드웨어 쪽 구현 전체다.

```
 ESP32 ×3 (센서 노드)                선박 게이트웨이 · Raspberry Pi 5            육상 서버 · Raspberry Pi 4
 ┌──────────────────────┐  Wi-Fi   ┌───────────────────────────────────┐ 선박↔육상 ┌───────────────────────────┐
 │ BRIDGE  S3  MPU6050  │  MQTT    │ Mosquitto ─▶ ACME MN-CSE (/id-ship)│   링크   │ ACME IN-CSE (/id-in)      │
 │              GPS     │ (oneM2M  │                 │ SUB 알림          │  HTTP   │  └ ship-gw/<노드>/<센서>   │
 │ ENGINE WROOM ADXL345 │  MQTT    │                 ▼                   │ ══════▶ │                           │
 │              MQ-2    │ binding) │ anchor-sync: SQLite 큐 → 육상 복제  │         │ 육상 관제 대시보드 :8090    │
 │ CARGO   C6  DHT11    │ ───────▶ │            + 링크 모니터            │         │  (두절/복구, 빈 구간 채움)   │
 │              MC-38   │          │ 선박 대시보드 :8090 (3D 선체, 카메라)│         └───────────────────────────┘
 └──────────────────────┘          └───────────────────────────────────┘
```

## 폴더 구성

| 폴더 | 돌아가는 곳 | 내용 |
|---|---|---|
| [`firmware/`](firmware/) | ESP32 3대 | 센서 노드 펌웨어 3종과 공통 oneM2M 라이브러리 `AnchorNode`, OTA 스크립트, 센서 테스트 스케치 |
| [`ship-gateway-pi5/`](ship-gateway-pi5/) | Raspberry Pi 5 (선박) | ACME MN-CSE 설정, 선박→육상 동기화 에이전트(`anchor-sync`), 선박 대시보드, Mosquitto·chrony 설정 |
| [`shore-server-pi4/`](shore-server-pi4/) | Raspberry Pi 4 (육상) | ACME IN-CSE 설정, 육상 관제 대시보드 |
| [`docs/`](docs/) | — | [하드웨어 구성 현황](docs/hardware-overview.md), [센서 배선도](docs/wiring.md) |

## oneM2M 자원 구조

| CSE | 위치 | 자원 |
|---|---|---|
| `/id-ship` · `cse-ship` (MN) | Pi 5 | AE `bridge`(`CBridge`) → `attitude`, `position`, `command`<br>AE `engine`(`CEngine`) → `vibration`, `gas`<br>AE `cargo`(`CCargo`) → `climate`, `hatch`<br>AE `sync`(`CSync`) → `link` (링크 상태) |
| `/id-in` · `cse-in` (IN) | Pi 4 | `remoteCSE` `id-ship` (선박 CSE 등록)<br>AE `ship-gw`(`CShipGW`) → `<노드>/<센서>` 선박 데이터 복제본 |

- **Mca, ESP32 → MN-CSE:** oneM2M MQTT 바인딩을 쓴다. 요청 토픽은 `/oneM2M/req/<Originator>/id-ship/json`이다.
- **Mcc, MN ↔ IN:** HTTP로 연결한다. 선박 CSE가 육상 CSE에 `remoteCSE`로 등록되므로, 육상에서 `/~/id-ship/...`으로 선박 자원을 조회할 수 있다.
- **복제:** `anchor-sync`가 선박 컨테이너를 구독(SUB)한다. 새 CIN이 생기면 육상에 같은 내용으로 CIN을 만들고, `con`에 다음 두 값을 덧붙인다.
  - `ship_ct`: 선박에서 생성된 시각
  - `fwd_delay_s`: 육상에 도착하기까지 걸린 시간(초)

## 처음 설치하는 순서

1. **ESP32:** [`firmware/README.md`](firmware/README.md)를 따라 `anchor_config.h`를 만들고 세 노드에 업로드한다.
2. **Pi 5 (선박):** [`ship-gateway-pi5/README.md`](ship-gateway-pi5/README.md)를 따라 Mosquitto, ACME, 대시보드, 동기화 에이전트를 설치한다.
3. **Pi 4 (육상):** [`shore-server-pi4/README.md`](shore-server-pi4/README.md)를 따라 ACME IN-CSE와 관제 대시보드를 설치한다.
4. **확인:** 선박 대시보드와 육상 대시보드를 둘 다 열어 값이 들어오는지 본다. Pi 5에서 Pi 4로 가는 통신을 잠깐 막았다 풀면 육상 화면에서 두절과 복구를 볼 수 있다.

## 시연 시나리오

선박 대시보드(Pi 5)와 육상 관제 대시보드(Pi 4)를 나란히 띄운다.

1. **평상시:** 두 화면 모두 실시간으로 갱신된다. 육상 화면의 복제 지연은 1초 미만이다.
2. **보트가 멀어져 링크가 끊길 때:** 육상 화면이 빨갛게 바뀌고 "선박 통신 두절"이 뜬다. 선박 화면은 계속 정상으로 갱신된다. 이걸로 선박 단독 운용을 보여 준다.
3. **보트가 돌아올 때:** 선박에 보관돼 있던 데이터가 순서대로 넘어오면서 육상 그래프의 빈 구간이 주황색으로 채워진다. 각 ESP32가 붙이는 순번(`seq`)으로 누락이 없는지도 검증한다.

리허설 결과(2026-11-15): 91초 단절에서 보관 데이터 406건을 복구했고, 순번 검증 결과 누락은 0건이었다.

## 하드웨어 상태와 남은 일

[docs/hardware-overview.md](docs/hardware-overview.md)의 "동작 확인 현황"과 "남은 하드웨어 작업"을 본다.
