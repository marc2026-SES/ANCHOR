# ANCHOR 실제 하드웨어

| 폴더 | 실행 위치와 역할 |
|---|---|
| `firmware/` | ESP32 3대: 센서 측정, MQTTS 업로드, 명령 수신 |
| `ship-gateway-pi5/dashboard/` | 선박 Pi 5: 센서·카메라 HTTPS 화면 |
| `shore-server-pi4/shore-dashboard/` | 육상 Pi 4: 최종 수신 데이터·카메라 중계 HTTPS 화면 |

선박·연안·육상 CSE 및 core 실행 설정은 `deploy/{ship,edge,shore}/`에 있다. 연안은 별도 Pi 또는 MEC Host에서 실행한다. 공통 core는 `src/anchor/`이며, 구 SQLite 동기화기는 새 배포에서 실행하지 않는다.

설치·TLS 발급·역할별 실행은 [루트 README](../README.md#역할별-배포와-실행), 센서 배선은 [배선 문서](docs/wiring.md)를 따른다. `docs/hardware-overview.md`는 원래 하드웨어 기록으로, 현재 배포 경로는 루트 README가 기준이다.
