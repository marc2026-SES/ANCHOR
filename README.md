# ANCHOR

oneM2M Hackathon 2026 · 팀 Ctrl+Sea

실제 ESP32 센서를 선박 Pi가 수집하고, 별도 연안 호스트가 데이터를 보관하여 육상으로 전달한다. **선박→연안과 연안→육상이 동시에 연결되어 있을 필요가 없다.** 육상 명령도 연안에 사전 보관했다가 선박에 전달한다.

`anchor`는 실물 통합 실행 경로다. 이전 온도·팬·부저 모의 구현은 `src/anchor/lab/`과 `anchor-lab`으로 분리했다. 새 실행 경로는 모의 측정이나 장치 성공 결과를 만들지 않는다.

## 구조와 기능

```text
선박 Pi 5                  연안 호스트                       육상 Pi 4
ESP32 3대                  edge CSE                        shore CSE
 ↓ oneM2M MQTT              ↕                               ↕
ship CSE ↔ ship core  ←A→   ANCHOR Edge 앱             ←B→   shore core
센서·카메라 화면            사건 확인·보관·재전송              관제·명령 발행
```

A와 B는 독립된 연결이다. 각각 설정한 CSE에 직접 접근한다. 선박→육상 직접 경로는 `direct_shore: true`일 때만 추가된다. A를 육상 경유 SSH 터널로 만들면 연안의 독립성이 사라진다.

| 위치 | 실행 내용 |
|---|---|
| 선박 | 기존 MPU6050/GPS, ADXL345/MQ-2, DHT11/MC-38; 원본 CSE; 기울기 사건; 보고 주기 명령; 카메라·20 Hz 화면 |
| 연안 | 별도 CSE 보관; 사건·명령 결과 우선 전달; 일반 기록 전송 기회 보장; 명령 사전 보관; 담당자 사건 확인 |
| 육상 | 최종 수신·해시 확인서; 명령 발행; 장치별 실제 적용 결과·미확인 상태 표시 |

연안의 **ANCHOR Edge는 향후 MEC Host에 올릴 앱**으로 패키징했다. CSE와 앱은 별도 컨테이너다. PCH/SUB/GRP는 CSE 리소스다. Docker 배치만으로 MEC 플랫폼·Mp1 등록·Sandbox 연동이 완료되는 것은 아니다. 해당 연동은 보류 상태다. 배치 경계는 [ETSI MEC 003](https://www.etsi.org/deliver/etsi_gs/mec/001_099/003/04.01.01_60/gs_mec003v040101p.pdf)의 앱·플랫폼·가상화 인프라 구분을 따른다.

## 하드웨어 통합

센서 핀, I2C/GPS 드라이버, 자세 필터, 진동 계산, 카메라, 실시간 MQTT 화면, OTA, BRIDGE 영점 조정은 유지했다. 기존 `bridge/attitude`, `engine/vibration` 등의 리소스 이름도 같다. 공통 통신 계층에 재부팅 ID, 재전송, 명령 중복 방지와 장치 결과를 추가했다.

기존 `anchor_sync.py`의 SQLite FIFO 전송은 새 core 실행 진입점으로 교체했다. 기존 `queue.db`와 CSE DB는 삭제하지 않는다. 새 core는 CSE에 남아 있는 원본을 다시 탐색한다. 구 동기화기와 새 ship core를 동시에 실행하지 않는다. 육상 센서 화면은 `cse-in/anchor/events`를 읽는다.

| 항목 | 처리 |
|---|---|
| 센서 데이터 | 원래 JSON을 `payload`에 보존하고 단위·품질·원본 리소스 참조를 붙임 |
| 재부팅 | `bootId` + 노드 전체 공유 `seq`; 다른 부팅의 순번으로 누락을 메우지 않음 |
| 구 펌웨어 | 원본 CIN ID로 수집 가능; 재부팅별 연속성·기울기 지속시간 검증에서는 제외 |
| 센서 품질 | GPS fix, DHT11 실패, MQ-2 예열을 구분; MQ-2 전압을 가스 농도로 표시하지 않음 |
| SDT | 유효한 DHT11 온도만 `cargo/temperature`의 표준 `cod:tempe` FCNT에 반영 |

## 보관·사건·명령의 의미

- 같은 이벤트 ID와 본문을 CSE에 저장하고 읽어 확인한다. 중복은 한 건으로 유지하며, 같은 ID의 다른 본문은 충돌로 격리한다.
- **연안 보관 확인과 육상 최종 확인은 별도 `receipts`**다. 선박은 연안 수신만으로 최종 완료를 표시하지 않는다. 확인된 원본도 자동 삭제하지 않는다.
- SUB는 빠른 감지용이다. CSE history discovery가 알림 누락과 앱 재시작을 복구한다. 앱 캐시는 재구성 가능하다.
- CNT 자동 축출을 끈다. 앱 journal은 기본 100,000건/256 MB, 펌웨어 raw CNT는 20,000건/5 MB에서 새 저장을 멈춘다. 여러 쓰기 주체 사이의 엄밀한 용량 예약까지 구현한 것은 아니다.
- ESP32는 32건의 RAM 송신 큐를 사용한다. 초과는 `droppedBefore`와 순번 공백으로 나타난다. 전원 손실 전 CSE가 받지 못한 RAM 데이터까지 보존하지는 않는다.
- 저장장치 공간·리소스 만료는 별도 조건이다. 기본 TinyDB의 강제 전원 차단 내구성은 미검증이다. 장기 운용에는 최종 확인 데이터 정리 정책이 필요하다.
- 기울기는 기본 25° 이상 2초에 사건을 열고, 15° 이하 2초에 회복시킨다. 4초보다 큰 관측 공백을 지속 증거로 쓰지 않는다. 규칙 기반 데모다.
- `setReportProfile`은 BRIDGE의 CIN 저장 간격과 ENGINE의 진동 통계 창을 바꾼다. 예: 1,000→200 ms, 30초 후 복귀. DHT11과 20 Hz 실시간 화면은 대상이 아니다.
- PCH 수신, GRP 명령 생성, ESP32 적용, 육상 결과 수신은 다르다. `applied`는 명령 본문·장치·설정·적용 시각이 일치하는 ESP32 결과가 있어야 표시된다. 실제 측정 주기는 연속 측정 시각과 `reportIntervalMs`로 추가 확인한다.
- 펌웨어는 TTL을 검사하고 NVS에 최근 8개 결과와 마지막 명령 순서를 보관한다. 중복은 기간을 연장하지 않으며 과거 명령이 새 설정을 덮어쓰지 않는다. 시각 동기화 전에는 원격 설정을 적용하지 않는다.

## Pi 설치와 기존 환경 전환

Python 3.11+, ACME, 선박 Mosquitto/chrony가 필요하다. ACME 기준 커밋은 `283bd7d696090e983c07c149511102caab99642e`다. ACME 원본은 수정하지 않는다.

1. 같은 소스를 세 호스트에 둔다. 서비스 예시는 `/opt/anchor`, 사용자 `pi`다. 기존 경로와 계정에 맞춰 조정한다.
2. 기존 CSE DB·설정을 백업하고 구 `anchor-sync`를 중지한다. DB를 삭제하거나 새 DB로 덮어쓰지 않는다. CSE 변경 적용 중에는 센서 송신을 잠시 멈춘다.
3. `scripts/install-pi.sh`로 설치한다. 카메라는 기존 picamera2 시스템 Python 서비스를 유지한다.
4. `deploy/cse/{ship,edge,shore}.ini`를 참고해 설정한다. `ship.local`, `edge.local`, `shore.local`을 실제 주소로 바꾼다. **`[resource.cnt] enableLimits=false`, ACP 활성화 후 CSE를 재시작**한다. 기존 DB 경로는 유지한다.
5. `deploy/{ship,edge,shore}.json`의 CSE URL·ID·리소스 이름을 맞춘다. 선박은 `cse-ship / id-ship`, 육상은 `cse-in / id-in`이다. 예제는 기존 HTTP/MQTT LAN 구성이다. 외부 연결에는 신뢰하는 LAN/VPN을 사용하고, 비신뢰 망에서는 TLS·MQTT 인증을 구성한다. HTTPS의 CA 검증은 끌 수 없다.
6. 역할별로 아래를 실행한다. `bootstrap`은 최초 권한 구성에 CAdmin을 사용한다. 기존 CNT 제한을 제거하지만 이미 저장된 CIN의 기존 만료시간은 바꾸지 않는다.

```bash
source .venv/bin/activate
# 각 호스트에서 ship 대신 edge 또는 shore를 선택
anchor --config deploy/ship.json init-token
anchor --config deploy/ship.json bootstrap
anchor --config deploy/ship.json serve
```

토큰은 `.runtime-<role>/api-token`에 저장되며 `init-token`은 기존 값을 덮어쓰지 않는다. `http://<호스트>:8790/`에서 토큰을 입력한다. 설정 예시는 데모 LAN 접근용 `bind: 0.0.0.0`이다. SSH만 사용할 때는 `127.0.0.1`로 바꾼다. 토큰을 URL이나 브라우저 저장소에 저장하지 않는다.

7. ESP32 빌드 환경에 **ArduinoJson 7**, PubSubClient, DHT sensor library와 ESP32 3.3.x 보드 패키지를 준비한다. `anchor_config.h`를 설정하고 `scripts/check-firmware.sh`로 세 보드를 빌드한 다음 기존 USB/OTA 절차로 업로드한다. NVS 명령 상태와 BRIDGE 보정값은 별도 namespace다.
8. 자동 실행에는 `deploy/anchor-core@.service`의 역할별 인스턴스를 사용한다. 호환용 `anchor-sync.service`와 `anchor-core@ship`은 같은 core이므로 하나만 실행한다.

기존 센서·카메라 화면은 8090, 새 관제는 8790이다. 육상 센서 화면의 “수신 재개”는 원본 전체 전달 완료와 다르다. 완료 검증은 새 관제의 고정 배치를 쓴다.

## 연안 컨테이너

`deploy/compose.edge.yaml`은 **Linux 연안 호스트**용이다. `edge-cse-data` 볼륨에 CSE 데이터를 보관하고 앱을 분리한다. MEC 플랫폼 설치와 실제 Mp1 연동은 포함하지 않았다.

```bash
# 먼저 edge.container.json과 cse/edge.ini의 주소를 수정
anchor --config deploy/edge.json init-token
export ANCHOR_UID="$(id -u)" ANCHOR_GID="$(id -g)"
docker compose -f deploy/compose.edge.yaml build
docker compose -f deploy/compose.edge.yaml up -d edge-cse
docker compose -f deploy/compose.edge.yaml run --rm edge-app --config /config/edge.json bootstrap
docker compose -f deploy/compose.edge.yaml up -d edge-app
```

호스트 네트워크를 쓰므로 기존 연안 CSE와 8080 포트를 중복 사용하지 않는다. `docker compose down -v`는 저장 볼륨을 삭제한다. 이 환경에서는 컨테이너 빌드를 검증하지 못했다.

## 촬영 순서

1. 실제 센서 3대와 세 위치의 관제를 보여 준다. 기울기에 자세 값이 반응하는지 확인한다.
2. 선박 외부 연결을 끊는다. 육상에서 명령을 발행하고 연안 저장을 확인한 다음 B를 끊는다. 명령은 아직 적용되지 않는다.
3. 선박 내부 ESP32↔Pi Wi-Fi를 유지한 채 기울기 사건을 만든다. 선박 원본·사건 기록이 늘어난다.
4. A만 연결한다. 연안 사건 수신·담당자 확인, 사전 저장 명령 전달, BRIDGE/ENGINE 주기 변경을 보여 준다. 육상 최종 확인은 아직 없다.
5. A를 끊고 B만 복구한다. **선박이 없는 동안** 육상에 사건과 장치 결과가 도착한다.
6. 다음 A 접촉에서 최종 확인서를 선박으로 돌려준다. 같은 배치 ID/해시로 대조한다.
7. 별도로 연안 앱 재시작, ACK 손실 후 재전송, 만료 명령 미적용을 촬영한다.

```bash
# 육상: 촬영 단절 시간을 포함하도록 TTL 설정
anchor --config deploy/shore.json command --target reportingGroup \
  --interval-ms 200 --duration-ms 30000 --ttl-ms 300000 --id demo-profile-01
# 선박: 수집분의 분모를 고정한 뒤 같은 분모로 대조
anchor --config deploy/ship.json capture-batch demo-01
anchor --config deploy/ship.json batch-status demo-01
```

영상에는 실제 패킷 차단/물리 단절 방식을 밝힌다. 화면의 모의 스위치는 통신 단절 증거가 아니다. GPS 실외 fix, MQ-2 배선·예열, MC-38 장착도 촬영 전에 확인한다.

## 검증

```bash
python -m pytest -q
ANCHOR_HARDWARE_INTEGRATION=1 python -m pytest tests/test_hardware_acme.py -v
scripts/check-firmware.sh
```

일반 테스트는 실제 센서 JSON 계약, 단절·재시작·중복·충돌·용량 제한·명령 결과 경계를 검사한다. 시나리오 단위 테스트는 CSE API 대역을 사용한다. MCU 시간 정책은 호스트 C++ 컴파일로 검사한다. 별도 ACME 통합 테스트는 임시 DB와 포트로 CSE 세 개를 실행한다. 결과 CIN fixture를 사용하는 부분은 실제 ESP32 시험과 구분했다.

현재 작업 환경에서는 로컬 포트 개설이 차단되어 ACME 통합 실행이 불가능했다. ESP32 툴체인과 Docker daemon도 없어 전체 펌웨어·컨테이너 빌드 및 실물 시연은 미검증이다. 일반 테스트 통과는 실물 연결 성공을 뜻하지 않는다.

| 경로 | 역할 |
|---|---|
| `hardware/firmware/` | 실제 센서 드라이버와 공통 통신·명령 처리 |
| `src/anchor/events.py`, `incidents.py` | 원본 변환, 단위·품질, 기울기 사건 |
| `src/anchor/runtime.py`, `store.py` | 역할 분리, CSE 보관·전달·확인서 |
| `src/anchor/commands.py`, `resources.py` | 실제 명령, PCH/GRP/SUB/ACP/SDT |
| `src/anchor/service.py`, `dashboard.html` | 세 위치 관제 API·화면 |
| `deploy/` | 역할별 설정, systemd, 연안 컨테이너 |
| `src/anchor/lab/` | 이전 모의 구현 |

개발 문서·PDF·도표는 로컬 자료이며 커밋에서 제외한다. README는 포함한다.
