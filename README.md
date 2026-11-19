# ANCHOR

oneM2M Hackathon 2026 · 팀 Ctrl+Sea

**현재 상태** · 애플리케이션 코어를 제거했다. 아래의 코어 구조와 명령은 이전 버전의 기록이다. 현재 실행 가능한 구성은 역할별 `cse.ini`와 `host.json`을 사용하는 ACME CSE, 센서 펌웨어, 독립 DDS 경로다. DDS IPE 자원은 `./deploy/edge/run.sh dds-provision`으로 준비한다.

실제 ESP32 센서를 선박 Pi가 수집하고, 별도 연안 호스트가 데이터를 보관하여 육상으로 전달한다. **선박→연안과 연안→육상이 동시에 연결되어 있을 필요가 없다.** 육상 명령도 연안에 사전 보관했다가 선박에 전달한다.

`anchor`는 실물 통합 실행 경로다. 이전 온도·팬·부저 모의 구현은 `src/anchor/lab/`과 `anchor-lab`으로 분리했다. 새 실행 경로는 모의 측정이나 장치 성공 결과를 만들지 않는다.

## 구조와 기능

```text
선박 Pi 5                  연안 호스트                       육상 Pi 4
ESP32 3대                  edge CSE                        shore CSE
 ↓ oneM2M MQTTS              ↕                               ↕
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

## 리소스 트리

아래는 **현재 코드가 생성하는 ANCHOR 리소스 구조**다. 실제 Pi의 CSE를 조회한 덤프는 아니다. AE는 앱·장치, CNT는 기록 컨테이너, CIN은 개별 기록, SUB는 구독, ACP는 권한 정책이다.

세 CSE 모두 `anchor` AE 아래에 다음 CNT를 생성한다. 전체 구조는 통합 문서의 상자·연결선 그림으로 확인할 수 있다.

| 공통 CNT | 저장 내용 |
|---|---|
| `events` | 측정·사건·운영자 확인·장치 결과 CIN |
| `receipts` | 연안 인수 / 육상 최종 수신 확인 CIN |
| `commands` | 명령 원문 CIN |
| `dispatches` | 선박의 장치 명령 전달 이력 CIN; 연안·육상은 생성만 함 |
| `quarantine` | 잘못된 기록의 identity·reason CIN |
| `ruleState` | 예약 리소스; 현재 기록 경로 없음 |
| `audit` | 촬영 대조용 고정 배치 manifest CIN |

| CSE | 공통 `anchor` 외의 주요 리소스 |
|---|---|
| 선박 `cse-ship` | `bridge/{attitude,position,command,commandResults}`, `engine/{vibration,gas,command,commandResults}`, `cargo/{climate,hatch,temperature}`, `anchor/reportingGroup/fopt` |
| 연안 `cse-edge` | `shipCommands/pch/pcu`: 선박 core가 폴링하는 경로; DDS 옵션: `ddsIpe/{vibration,status}` |
| 육상 `cse-in` | `edgeCommands/pch/pcu`: 연안 core가 폴링하는 경로; `anchor/events/sub-shore-dash`: 육상 센서 화면 구독 |

중괄호 안은 서로 다른 자식 리소스다. 선박의 `temperature`는 SDT 온도를 표현하는 **FCNT**이며 `curT0`는 그 속성이다. `reportingGroup`은 **GRP**이고 `fopt`는 가상 팬아웃 경로다. `pch`는 **PCH**, `pcu`는 가상 폴링 경로이며 명령 원문은 여기에 보관하지 않는다.

선박 core는 센서 CNT 6개와 장치 결과 CNT 2개 아래에 `anchor-core` SUB를 생성한다. BRIDGE/ENGINE 펌웨어는 각 `command` CNT 아래에 `sub-node` SUB를 생성한다. 연안·육상에는 원본 센서 트리를 복제하지 않고 `anchor/events`로 전달한다.

각 CSE 루트에는 다른 core의 신원을 등록하는 `peer-*` AE 2개, `dashboard` AE와 ACP도 있다. ACP는 루트에 두고 대상 리소스의 `acpi`가 참조한다. 이 `peer-*` AE는 ACME가 CSE 등록 시 만드는 CSR과 다르다. 실제 CSE에는 ACME 기본·관리 리소스 및 등록 상태에 따른 CSR이 추가될 수 있다.

전체 리소스 트리는 로컬 통합 문서 `docs/ANCHOR_OVERVIEW.pdf`·`.md`의 **20절 통합본(선박 → 연안 → 육상) · 21절 선박 · 22절 연안 · 23절 육상**에 흰 바탕 도형으로 수록했다. 모든 리소스를 옆으로 나누지 않고 한 열로 내려 읽도록 배치했으며, 들여쓰기와 연결선으로 부모·자식 관계를 나타냈다. AE·CNT·CIN·SUB·PCH·GRP·FCNT·ACP와 가상 경로를 구분해 표시했다. **19절**에는 읽는 법, **24절**에는 ACP 연결·생성 시점·개수를 정리했다. 코드 근거는 `src/anchor/resources.py`, `runtime.py`, `commands.py`, 펌웨어 `AnchorNode.h`와 육상 `shore_server.py`다.

통합 문서 **25절**은 이번 ACP·명령 재시도 수정, **26절**은 장비 전체 시험과 T1-T15, **27절**은 Shore Monitor 연결·토큰 배치·baseline 준비 상태다.

**28-30절**은 native DDS 원본 파형 경로, QoS 6종, IPE 권한·실행·시험 범위를 정리한다.

## 하드웨어 통합

센서 핀, I2C/GPS 드라이버, 자세 필터, 진동 계산, 카메라, 실시간 자세 화면, BRIDGE 영점 조정은 유지했다. 실시간 화면은 MQTTS로 연결하며, 기존 ArduinoOTA 코드는 남겨 두되 기본 비활성화했다. 기존 `bridge/attitude`, `engine/vibration` 등의 리소스 이름도 같다. 공통 통신 계층에 재부팅 ID, 재전송, 명령 중복 방지와 장치 결과를 추가했다.

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

## 역할별 배포와 실행

배포 설정은 `deploy/ship/`, `deploy/edge/`, `deploy/shore/`로 분리했다. 각 폴더의 `run.sh`가 해당 역할의 설정을 고정해 실행한다. 공통 코드는 `src/anchor/` 하나를 사용하고, 실행 프로세스·CSE DB·토큰·서버 키는 호스트별로 분리한다.

| 호스트 | 올릴 소스 묶음 | 실행할 프로세스 | 실행하지 않는 것 |
|---|---|---|---|
| 선박 Pi 5 | `anchor-ship.tar.gz` | ship CSE, ship core, 센서·카메라 화면, Mosquitto, chrony | edge/shore CSE·core |
| 연안 MEC Host | `anchor-edge.tar.gz` | edge CSE, edge core | 센서 드라이버·카메라·MQTT 브로커 |
| 육상 Pi 4 | `anchor-shore.tar.gz` | shore CSE, shore core, 육상 센서 화면 | 센서 직접 수집·MQTT 브로커 |

ESP32 펌웨어는 개발 PC에서 빌드해 각 보드에 올린다. 선박 Pi가 ESP32 센서 코드를 실행하는 것은 아니다. 육상은 자기 CSE에 도착한 기록으로 관제하고, 카메라만 선박의 HTTPS 스트림을 중계한다.

개발 PC에서 역할별 파일을 만든다. 비밀키, 토큰, DB, 로컬 설정, lab 모의 구현은 소스 묶음에 포함하지 않는다. 묶음의 `.anchor-role`은 다른 역할로 실행하는 것을 막는다.

```bash
mkdir -p .runtime-packages
scripts/export-source.sh ship  "$PWD/.runtime-packages/anchor-ship.tar.gz"
scripts/export-source.sh edge  "$PWD/.runtime-packages/anchor-edge.tar.gz"
scripts/export-source.sh shore "$PWD/.runtime-packages/anchor-shore.tar.gz"
```

각 Pi에서는 **자기 묶음만** `/opt/anchor`에 푼다. 아래 서비스 예시는 사용자 `pi`, 경로 `/opt/anchor` 기준이다. 실제 계정이 다르면 systemd의 `User=`와 경로를 맞춘다. 기존 설치 디렉터리 위에 그대로 덮어쓰기 전에 CSE DB와 설정을 백업한다.

## HTTPS·MQTTS 설정

| 구간 | 프로토콜 / 포트 | 검증 |
|---|---|---|
| ESP32 ↔ 선박 브로커 ↔ ship CSE | MQTTS / 8883 | CA·서버 이름 검증 + 클라이언트 인증서 필수 |
| 20 Hz 센서 화면의 MQTT 구독 | MQTTS / 8883 | 선박 Pi 인증서로 브로커에 접속 |
| core ↔ CSE, CSE 등록·상호 요청 | HTTPS / 8080 | CA·서버 이름 검증 |
| 관제 API·화면, core SUB 알림 | HTTPS / 8790 | TLS + 기존 관제 Bearer 토큰 |
| 센서·카메라 화면 / 영상 중계 | HTTPS / 8090 | CA·서버 이름 검증 |
| 육상 센서 화면의 SUB 수신 | HTTPS / 8091, loopback만 | CSE가 CA·서버 이름 검증 |

8080은 그대로 쓰되 HTTPS로 접속한다. 인증서가 없거나 신뢰 검증에 실패하면 연결을 중지한다. HTTP/MQTT로 자동 우회하지 않는다. HTTP 리다이렉트도 따라가지 않는다. NTP·mDNS는 시각 동기화·주소 검색용으로 남으며, ArduinoOTA는 기본 비활성화하고 USB로 펌웨어를 올린다.

MQTTS의 클라이언트 인증과 **oneM2M ACP 권한 정책은 별개**다. ACP는 자원별로 분리했고 센서 화면은 `CAnchorDashboard`로 읽는다. 인증서 CN ↔ MQTT 토픽 ↔ oneM2M Originator 연결과 HTTPS 호출자 신원 연결은 아직 남아 있다. TLS만으로 Originator 사칭이 차단되지는 않는다. DDS 옵션은 아래 별도 보안 범위를 따른다.

## Native DDS IPE: ADXL345 원본 진동 파형

ROS 2·rclpy 없이 Cyclone DDS Python 11.1.0을 직접 사용한다. [참고 IPE의 고정 커밋](https://github.com/parksiw0/ROS2-oneM2M_Interworking_Proxy_Entity/tree/e87a0aab6da21c63ae88918d204a9fb15fe27fe3)에서 QoS·변환·CSE 저장을 나누는 구조를 참고해 ANCHOR용으로 작성했다. ROS 토픽 탐색·서비스·액션·22종 QoS는 가져오지 않았다.

| 센서 | 코드상 목표 측정률 | 기존 전송 | 이번 DDS 대상 |
|---|---|---|---|
| ENGINE ADXL345 | 200 Hz, 5 ms 간격 XYZ 가속도 | 기본 약 1초 RMS·피크 요약 | **원본 XYZ 파형** |
| BRIDGE MPU6050 | 100 Hz, 10 ms 간격 자세 계산 | 실시간 자세 20 Hz, CSE 기본 1 Hz | 기존 경로 유지 |
| GPS / MQ-2 / DHT11 / MC-38 | 각각 2초 / 2초 / 5초 / 개폐 시 | 기존 센서 기록 | 대상 아님 |

이는 코드 설정값이다. ESP32의 통신 지연·센서 읽기 실패로 실제 측정률이 낮아질 수 있으며 실측 200 Hz를 보장하지 않는다.

```text
ENGINE ADXL345 (목표 200 samples/s)
  -> ESP32: 최대 20 samples / 약 100 ms 묶음
  -> MQTTS QoS 0: anchor/raw/<vesselId>/engine/vibration
  -> 선박 Pi: native DDS publisher
  -> DDS/RTPS over UDP: AnchorVibrationV1
  -> 연안 MEC Host: native DDS IPE
  -> HTTPS: cse-edge/ddsIpe/vibration/<CIN> (기본 1초 창)
            cse-edge/ddsIpe/status/<CIN>    (5초마다 상태)
```

ESP32가 DDS를 직접 실행하는 구조는 아니다. 초당 약 10개 DDS 묶음에 총 약 200개 원본 샘플을 싣는다. 실제 샘플의 상대 시각(`offsetsUs`)과 `bootId`·별도 순번을 보존한다. 기존 1초 요약값은 `engine/vibration → anchor/events` 경로로 계속 전달하고 재전송·최종 수신 확인을 유지한다. 새 원본 파형은 **보조 실시간 경로**이며 단절 후 전량 복구나 육상 저널 자동 복제를 제공하지 않는다. Monitor가 연안 CSE를 조회할 수 있지만 파형 차트 UI는 이번 변경에 포함하지 않았다.

| QoS 종류 | 기본값 | 적용 의미·지원 선택지 |
|---|---|---|
| Reliability | BEST_EFFORT | 최신 파형 우선. RELIABLE 선택 시 DDS 구간 재전송; write 대기는 최대 20 ms |
| Durability | VOLATILE | 늦게 붙은 reader에 이전 기록 재전달 안 함. TRANSIENT_LOCAL은 살아 있는 writer의 캐시 이용 |
| History | KEEP_LAST, depth=20 | 최근 DDS 묶음 20개. KEEP_ALL도 내부 ResourceLimits 256개 한도를 지킴 |
| Deadline | 300 ms | 발행/수신 간격 위반을 native DDS 상태 카운터로 관측; 전송 주기를 강제하지 않음 |
| Lifespan | 1,500 ms | writer에 원본 첫 샘플 시각을 넣어 만료 처리. publisher/IPE도 오래된 입력을 거부 |
| Liveliness | AUTOMATIC, lease=2,000 ms | 참여자 생존 감시. MANUAL_BY_TOPIC은 쓰기가 생존을 알리며 별도 유휴 heartbeat는 없음 |

6종은 실제 native DDS 정책으로 설정한다. 메모리 보호용 ResourceLimits는 고정 내부 한도다. [공식 QoS API](https://cyclonedds.io/docs/cyclonedds-python/latest/cyclonedds.qos.html)와 [UDP·인터페이스·peer 설정](https://cyclonedds.io/docs/cyclonedds/latest/config/config_file_reference.html)을 따른다. `lifespan_ms`는 DDS 표본의 유효기간이며, `retention_seconds=60`은 **이미 CSE에 저장한 CIN의 별도 보관 기간**이다. IPE는 CSE 쓰기 큐 8개·창당 최대 400샘플로 제한하고 만료·드롭·중복·관측 순번 공백·쓰기 실패를 집계한다. `observedBatchGaps`는 늦은 도착으로 메워질 수 있는 관측 공백이며 확정 패킷 손실률이 아니다. RELIABLE을 골라도 앞단 MQTTS QoS 0과 CSE 쓰기 실패까지 무손실이 되지는 않는다.

### IPE 권한과 실행

연안 `core.json`의 `dds_ipe_enabled=true`일 때 bootstrap이 `ddsIpe` AE와 `vibration`·`status` CNT 및 각각의 ACP를 만든다. 실행 IPE는 `CAnchorDDSIPE`로 조회·검색하고 두 CNT에 **CIN(4)만 생성**한다. ACP 수정, SUB 생성, 명령·기존 anchor 저널 접근은 허용하지 않는다. Dashboard와 Edge core는 조회·검색만 한다. 기존 CSE에서는 `./deploy/edge/run.sh bootstrap`을 다시 실행해야 한다. 설정을 끄더라도 기존 리소스·ACP를 자동 삭제하지 않는다.

1. 선박·연안에 기존 core 환경과 TLS 파일을 준비하고 `scripts/install-dds.sh`를 실행한다. Pi/Linux ARM에서는 고정 Cyclone DDS C 소스를 빌드하므로 git·cmake·C/C++ 빌드 도구가 필요하다. 기본 venv는 `.venv`; 다른 환경은 `ANCHOR_VENV`로 지정한다. Python 3.11-3.13을 권장한다.
2. `deploy/ship/dds.json`, `deploy/edge/dds.json`에서 `interface`를 **자기 호스트 IPv4**, `peers`를 **상대 호스트 IPv4 배열**로 맞춘다. 기본은 loopback이라 다른 호스트와 통신하지 않는다. 같은 domain/topic과 호환 QoS를 사용한다. 선박 `mqtt_host`는 인증서 SAN과 일치해야 한다.
3. 시각을 동기화하고 기존 브로커·CSE를 실행한다. 연안 bootstrap 후 아래 명령을 각각 해당 호스트에서 실행한다.

```bash
# 선박
./deploy/ship/run.sh dds-check
./deploy/ship/run.sh dds
# 연안
./deploy/edge/run.sh bootstrap
./deploy/edge/run.sh dds-check
./deploy/edge/run.sh dds
```

`dds-check`는 설정 검사이며 통신 성공 검사가 아니다. 필요하면 `deploy/systemd/anchor-dds@.service`를 설치해 ship 또는 edge 인스턴스로 실행한다. ENGINE 펌웨어의 `ANCHOR_ENABLE_DDS_SOURCE=1`은 원본 묶음을 활성화하며 `0`으로 끌 수 있다. 원본 재전송 큐는 없다.

연안 Linux 컨테이너에서는 `dds.container.json`의 interface/peers와 CSE 주소를 먼저 맞춘 뒤 `docker compose -f deploy/edge/compose.yaml --profile dds up -d --build edge-dds-ipe`로 실행한다. 이 서비스는 UDP discovery를 위해 host network를 사용하며 일반 core 컨테이너와 별도다. CSE bootstrap이 선행되어야 한다. ARM 네이티브 빌드·컨테이너 실행·실제 MEC 연동은 아직 미검증이다.

**DDS Security는 활성화하지 않았다.** DDS 구간에는 TLS가 적용되지 않으므로 격리된 신뢰 네트워크 또는 인증·암호화 터널을 사용한다. HTTPS 쪽 ACP가 DDS 송신자를 인증해 주지는 않는다. MQTTS·HTTPS 보안은 그대로 유지한다. 시연은 진동 입력 → DDS 수신·sampleCount 증가 → CSE 파형 창 저장을 보여주고, 링크 단절 시 실시간 파형 중단과 기존 요약 저널의 복구를 구분한다. UDP의 지연·손실 이점은 동일 조건 비교 측정 전까지 성능 결과로 주장하지 않는다.

## ACP 최소 권한과 대시보드 연결

ACP 이름은 `acp-<자원 경로에서 /를 -로 바꾼 값>`이다. 예: `acp-anchor-events`, `acp-bridge-position`. 모든 ACP는 CSEBase 직속이고, 보호 대상의 `acpi`가 참조한다. `pvs`는 `CAdmin`만 허용하며 `CAdmin`은 bootstrap에만 사용한다. CSE 실행 설정의 `enableACPChecks=true`가 필요하고 `run.sh cse`는 이 검사가 꺼져 있으면 실행을 거부한다. bootstrap이 실행 중인 CSE의 검사 설정 자체를 켜는 것은 아니다.

| 주체·자원 | 허용 |
|---|---|
| 각 core → 자기 `anchor`와 저널 | 기존 전체 권한 유지 |
| 선박 → 연안·육상 `anchor/events`, 연안 → 육상 `anchor/events` | C·R·Di(35); 삭제·수정 불가 |
| 하위 core → 상위 `receipts`·`commands` | R·Di(34) |
| 육상 Dashboard → `events`·`receipts`·`commands` | R·Di(34) |
| 육상 Dashboard → `events` 아래 자식 생성 | SUB(23)만: `acop=1`, `acod=[{"chty":[23]}]`; CIN 생성 불가 |
| 연안 Dashboard → `events`·`commands` | `dashboard_edge_read=true`일 때 R·Di(34). 연안 설정은 기본 활성화; receipts·SUB는 허용하지 않음 |
| BRIDGE·ENGINE → 자기 `command` | R(2) + SUB만 C. 이 장치 명령 CNT에 CIN을 기록하는 주체는 선박 core |
| 선박 Dashboard → 센서 | R(2), `bridge/position` 제외 |
| 선박 core → `anchor/reportingGroup` | `acp-anchor-reportingGroup`으로 전체 권한; 다른 앱은 그룹 제어 불가 |
| `CAnchorAgent` → 선박 센서 6개 | R·Di(34). `bridge/attitude`, `bridge/position`, `engine/vibration`, `engine/gas`, `cargo/climate`, `cargo/hatch` |
| `CAnchorAgent` → 선박 부모 AE | `bridge`·`engine`·`cargo` 및 자기 `agent` AE에 R·Di(34). 탐색용이며 다른 자식의 ACP를 우회하지 않음 |

센서·명령 결과·SDT FCNT에도 자원마다 별도 ACP를 둔다. 초안의 장치 `command`에 대한 일반 C·R(3)은 장치가 명령 CIN을 직접 만들 수 있어 **R + SUB 전용 C**로 좁혔다. `acod/chty`는 생성할 자식 종류를 제한하는 조건이다. [oneM2M TS-0001 9.6.2.4](https://www.onem2m.org/images/files/deliverables/Release2A/TS-0001-Functional_Architecture-v_2_18_1.pdf)

명령의 최초 발행은 육상 운영자 API에서 한다. 육상 core가 `anchor/commands`에 저장하고, 연안을 거쳐 선박 core가 `bridge/command`·`engine/command`에 기록한다. 장치는 명령을 수신·실행하고 자기 `commandResults`에 결과를 쓴다. 장치의 명령 CIN 생성 제한은 육상 명령 발행이나 장치의 수신·실행·결과 보고를 막는 것이 아니다.

기존 CSE는 DB·설정을 백업하고 세 역할에서 `bootstrap`을 다시 실행한다. 새 ACP를 생성한 뒤 AE·CNT·FCNT·GRP의 `acpi`를 갱신하며, 기존 CIN과 관측 온도는 유지한다. 옛 이름 ACP는 자동 삭제하지 않는다. 사용 중인 참조가 없는지 확인한 뒤 정리한다. 육상 하드웨어 화면은 Dashboard 소유의 `sub-shore-dash-reader`를 사용하며, 기존 core 소유 `sub-shore-dash`도 자동 삭제하지 않는다.

AI 에이전트는 실행 위치와 관계없이 **선박 CSE**에 `X-M2M-Origin: CAnchorAgent`로 요청한다. 요청대로 GPS `bridge/position`을 포함한 6개 센서를 허용했다. 센서 CNT와 하위 CIN·`la` 조회, 센서/CIN 검색만 가능하며 CIN·SUB 생성, 수정·삭제, 명령·결과·SDT `cargo/temperature`·anchor 저널·DDS IPE 자원 접근은 허용하지 않는다. 선박 Dashboard의 GPS 접근 제한은 유지한다. 추가 센서는 자동으로 AI에 공개하지 않는다.

기존 선박 CSE에 적용하려면 갱신한 코드로 `./deploy/ship/run.sh bootstrap`을 실행한다. `agent` AE를 등록하고 `acp-agent`, 부모 AE 및 센서별 ACP를 갱신한다. 기존 센서 기록은 유지한다. 예를 들어 `GET /~/id-ship/cse-ship/bridge/position/la`로 최신 GPS CIN을 읽고, `GET /~/id-ship/cse-ship/bridge?fu=1&ty=3&drt=1`로 허용된 센서 CNT를 찾는다. 기본 주소는 `https://ship.local:8080`이며 실제 배포 주소와 인증서 SAN에 맞춰야 한다. CA PEM으로 서버 인증서를 검증한다. core API용 `api-token`·`status-token`과 CSE ACP는 별개다.

육상 `anchor/events`에는 위치 이벤트도 있으므로 Dashboard의 저널 조회 권한에는 위치 조회도 포함된다. AI에는 별도 `CAnchorAgent` 신원을 사용하고 Dashboard 신원이나 운영자 토큰을 공유하지 않는다. 이번 권한은 선박 원본 센서 6개에만 적용하며 육상·연안 저널 권한은 추가하지 않는다.

## Shore Monitor 주소와 읽기 전용 토큰

아래는 **배포 기본 주소**이며 실제 IP·DNS·인증서 SAN·접속 성공을 확인한 값은 아니다. 연안은 MEC Host에 배포하고, 임시 시험에서는 맥이 같은 edge 역할을 맡는다.

| 연결 | 주소 / 자격 |
|---|---|
| 육상 CSE | `https://shore.local:8080/~/id-in/cse-in`, Originator `CAnchorDashboard` |
| 연안 CSE(저장 시각 조회) | `https://edge.local:8080/~/id-edge/cse-edge`, 같은 Dashboard 신원 |
| 선박 core 상태 | `https://ship.local:8790/status`, 선박 status 토큰 |
| 연안 core 상태 | `https://edge.local:8790/status`, 연안 status 토큰 |

운영자 `api-token` 대신 `.runtime-<role>/status-token`을 Monitor에 전달한다. `status_token_file` 설정으로 로드하며 `GET /status`만 허용한다. 반환 내용은 role·ready·scan 시각·전달 수·순번 공백·연결 상태·PCH/SUB 횟수다. `latest`·사건/명령 원문·진단 오류 본문은 반환하지 않는다. `/commands`, `/incidents/ack`, `/batches`는 이 토큰으로 호출하면 403이다. 토큰을 안 보내거나 잘못 보내면 401이다. 운영자 토큰은 기존 제어 기능을 유지한다.

신규 설치 또는 기존 설정에 `status_token_file`을 추가할 때, 각 역할에서 `./deploy/<role>/run.sh init-status-token`을 최초 한 번 실행하고 core를 다시 시작한다. 이미 전달받은 토큰 파일을 설치했다면 다시 생성하지 않는다. 기존 파일은 덮어쓰지 않는다. 파일은 실행 사용자 소유·권한 600으로 배치하고 로그·채팅·브라우저 저장소에 넣지 않는다. MEC 컨테이너는 `/secrets/status-token`을 읽는다. 양쪽에 같은 파일을 배치하기 전에는 새 토큰이 실행 중인 core에 적용된 것으로 간주하지 않는다.

개발 작업공간에는 전달용 `.runtime-handoff/shore-monitor/ship-status-token`, `edge-status-token`, `connections.json`을 별도로 준비했다. 이 폴더는 Git과 소스 묶음에 포함하지 않는다. 실제 호스트 주소는 연결 전에 확인해야 한다.

현재 이 체크아웃의 `dashboard/`는 더미 데이터 UI다. 팀원이 별도로 완성한 Shore Monitor에는 위 주소·읽기 전용 토큰과 배포 CA 파일을 연결한다. CA 검증을 끄지 않는다. 현재 추적 중인 파일에는 `anchor-korea-strait.json`과 `Integration_API.md`가 없어 해당 파일의 평문 비밀번호 제거·회전은 확인하지 못했다. 로컬 런타임과 토큰은 Git·역할별 소스 묶음에서 제외한다.

## 인증서 준비와 주소 설정

MQTT 인증서 설정은 [Mosquitto 공식 설명](https://mosquitto.org/man/mosquitto-conf-5.html), ESP32의 주소·인증서 이름 분리는 [Arduino-ESP32 3.3.0 API](https://github.com/espressif/arduino-esp32/blob/3.3.0/libraries/NetworkClientSecure/src/NetworkClientSecure.h)를 기준으로 구현했다.

고정 ACME 커밋 `283bd7d696090e983c07c149511102caab99642e`에는 HTTPS 수신의 클라이언트 인증과 송신 인증서 검증을 같은 옵션으로 처리하는 문제, MQTT 호스트명 검증을 끄는 코드가 있다. `anchor.acme_tls` 실행기가 이를 보완한다. 원본 ACME 파일은 수정하지 않으며 소스 해시가 달라지면 실행을 거부한다. **이 구성에서는 `acmecse`를 직접 실행하지 말고 `run.sh cse`를 사용한다.**

개발 PC에서 인증서를 한 번 발급한다. `openssl`이 필요하다. 기본 SAN은 각 역할의 `ship.local` / `edge.local` / `shore.local`, `localhost`, loopback IP다. 다른 이름이나 IP로 접속하려면 발급할 때 해당 값을 추가한다.

```bash
python3 scripts/provision-tls.py --output .runtime-pki \
  --ship-host 192.168.0.10 --edge-host 192.168.0.20 --shore-host 192.168.0.30
```

위 IP는 예시이므로 실제 주소로 바꾼다. 인증서 생성은 기존 디렉터리를 덮어쓰지 않는다. 호스트·장치 인증서는 365일 유효하다. 키 묶음을 안전하게 보관하고 만료 전에 교체한다.

- `.runtime-pki/ship/`의 파일 3개 → 선박 `/opt/anchor/.runtime-ship/tls/`
- `.runtime-pki/edge/`의 파일 3개 → 연안 `/opt/anchor/.runtime-edge/tls/`
- `.runtime-pki/shore/`의 파일 3개 → 육상 `/opt/anchor/.runtime-shore/tls/`
- `.runtime-pki/firmware/anchor_{bridge,engine,cargo}_tls.h` → 개발 PC의 `hardware/firmware/libraries/AnchorNode/`
- `issuer/`와 CA 개인키는 Pi에 복사하지 않는다. 각 펌웨어에는 해당 장치의 키만 포함된다.

각 호스트의 `.runtime-<role>`과 `tls` 디렉터리는 해당 실행 사용자 소유, 권한 700으로 두고 `server.key`는 600으로 둔다. 브라우저를 사용하는 PC에는 발급한 `ca.crt`를 신뢰할 CA로 등록한다. 경고 무시나 `verify=False`로 연결하지 않는다.

세 호스트에서 `ship.local`, `edge.local`, `shore.local`이 올바른 IP로 해석되고 라우팅되어야 한다. 서브넷을 넘으면 mDNS만 기대하지 말고 DNS 또는 `/etc/hosts`를 설정한다. `core.json`의 URL, `cse.ini`의 `cseHost`·registrar 주소, 인증서 SAN이 서로 일치해야 한다. ESP32의 `MQTT_TLS_HOST`는 선박 인증서의 DNS 이름, `CSE_MDNS`는 주소 검색 이름이다.

## Pi 최초 준비와 기존 DB 이관

세 호스트 공통으로 Python 3.11+, git, openssl, python3-venv가 필요하다. `/opt/anchor`는 실행 사용자가 쓸 수 있어야 한다.

```bash
cd /opt/anchor
scripts/install-pi.sh
```

처음 설치하는 CSE는 아래 역할별 `init-cse` 명령으로 초기화한다. `deploy/<role>/cse.ini`를 `.runtime-<role>/cse/acme.ini`로 한 번 복사하고, 이미 있는 런타임 디렉터리는 덮어쓰지 않는다. 이후 실제 CSE가 읽는 설정은 **런타임의 `acme.ini`**다.

기존 CSE를 옮길 때는 센서 송신과 기존 CSE·core를 멈추고 DB·설정·로그를 백업한다. 기존 CSE 런타임을 `.runtime-<role>/cse/`로 이관한 뒤 새 `cse.ini`의 TLS·registrar·ACP 설정을 `acme.ini`에 반영한다. ID·리소스 이름·DB 경로를 유지하고, 이 경우 `init-cse`는 실행하지 않는다. 구 `anchor-sync`, `anchor-ship-cse`, `anchor-shore-cse`, 구 대시보드 서비스가 새 서비스와 동시에 실행되지 않게 중지·비활성화한다. DB 자동 초기화나 손상 DB 이동 스크립트는 새 실행 경로에서 사용하지 않는다.

선박에만 브로커·NTP·카메라 패키지를 설치한다.

```bash
sudo apt install -y mosquitto mosquitto-clients chrony python3-picamera2
sudo install -d -m 750 -o root -g mosquitto /etc/anchor/mqtt
sudo install -m 644 .runtime-ship/tls/ca.crt .runtime-ship/tls/server.crt /etc/anchor/mqtt/
sudo install -m 640 -o root -g mosquitto .runtime-ship/tls/server.key /etc/anchor/mqtt/server.key
# 기존 1883 listener 설정을 백업 후 비활성화한다. 중복 listener가 남지 않게 확인한다.
sudo install -m 644 deploy/ship/mosquitto.conf /etc/mosquitto/conf.d/anchor.conf
sudo install -m 644 deploy/ship/chrony.conf /etc/chrony/conf.d/anchor.conf
sudo systemctl restart mosquitto chrony
```

chrony가 실제 UTC로 동기화되었는지 확인한다. ESP32는 시각을 확보한 뒤 TLS를 연결한다. `anchor_config.example.h`를 `anchor_config.h`로 복사해 Wi-Fi, 선박 주소, MQTTS 포트 8883을 설정한다. 생성한 장치별 TLS 헤더를 넣고 Arduino-ESP32 3.3.x, ArduinoJson 7, PubSubClient, DHT 라이브러리로 `scripts/check-firmware.sh`를 실행한 뒤 USB로 업로드한다.

## 호스트별 실행 명령

CSE는 **육상 → 연안 → 선박** 순서로 시작한다. `cse`, `core`, `dashboard`는 계속 실행되는 프로세스이므로 수동 실행 시 각각 별도 터미널을 쓴다. `bootstrap`은 해당 로컬 CSE가 준비된 뒤 실행한다. CAdmin으로 초기 리소스·기존 ACP를 설정한다. 기존 CNT 제한을 제거하지만 이미 저장된 CIN의 만료시간은 변경하지 않는다.

선박 Pi 5:

```bash
cd /opt/anchor
./deploy/ship/run.sh check
./deploy/ship/run.sh init-cse     # 신규 설치만
./deploy/ship/run.sh init-token   # 최초 1회, 기존 토큰은 덮어쓰지 않음
./deploy/ship/run.sh init-status-token  # 최초 1회; 전달받은 파일을 설치했다면 생략
./deploy/ship/run.sh cse          # 터미널 1
./deploy/ship/run.sh bootstrap    # 터미널 2: CSE 준비 후
./deploy/ship/run.sh core         # 터미널 2
./deploy/ship/run.sh dashboard    # 터미널 3: picamera2 시스템 Python
```

연안 MEC Host:

```bash
cd /opt/anchor
./deploy/edge/run.sh check
./deploy/edge/run.sh init-cse
./deploy/edge/run.sh init-token
./deploy/edge/run.sh init-status-token  # 최초 1회; 전달받은 파일을 설치했다면 생략
./deploy/edge/run.sh cse          # 터미널 1
./deploy/edge/run.sh bootstrap    # 터미널 2
./deploy/edge/run.sh core         # 터미널 2
```

육상 Pi 4:

```bash
cd /opt/anchor
./deploy/shore/run.sh check
./deploy/shore/run.sh init-cse
./deploy/shore/run.sh init-token
./deploy/shore/run.sh init-status-token  # 최초 1회; 전달받은 파일을 설치했다면 생략
./deploy/shore/run.sh cse         # 터미널 1
./deploy/shore/run.sh bootstrap   # 터미널 2
./deploy/shore/run.sh core        # 터미널 2
./deploy/shore/run.sh dashboard   # 터미널 3
```

관제 주소는 `https://<역할>.local:8790/`, 센서 화면은 선박·육상의 `https://<역할>.local:8090/`이다. 관제 토큰은 각 호스트의 `.runtime-<role>/api-token`에 있다. 육상 센서 화면의 “수신 재개”와 원본 전체 전달 완료는 다르다. 최종 완료는 core 관제의 고정 배치로 확인한다.

자동 실행은 수동 프로세스를 중지한 뒤 설정한다. 아래에서 **현재 Pi에 해당하는 한 줄만** 선택한다. CSE·토큰 준비와 bootstrap은 앞에서 완료해야 한다.

```bash
sudo cp deploy/systemd/anchor-*.service /etc/systemd/system/
sudo systemctl daemon-reload
# 선박 Pi
sudo systemctl enable --now anchor-cse@ship anchor-core@ship anchor-dashboard@ship
# 연안 Linux 호스트의 네이티브 실행 선택지 (Docker와 동시 실행 금지)
sudo systemctl enable --now anchor-cse@edge anchor-core@edge
# 육상 Pi
sudo systemctl enable --now anchor-cse@shore anchor-core@shore anchor-dashboard@shore
```

## 연안 컨테이너 실행 선택지

Linux 연안 호스트에서만 `deploy/edge/compose.yaml`을 사용한다. 연안 네이티브 서비스와 동시에 실행하지 않는다. CSE와 앱 컨테이너는 분리되어 있고 같은 연안 인증서 묶음을 읽는다. TLS 디렉터리의 소유 UID/GID와 컨테이너 UID/GID가 같아야 한다.

```bash
./deploy/edge/run.sh init-token   # 아직 토큰이 없을 때만
./deploy/edge/run.sh init-status-token  # 최초 1회; 전달받은 파일을 설치했다면 생략
export ANCHOR_UID="$(id -u)" ANCHOR_GID="$(id -g)"
docker compose -f deploy/edge/compose.yaml build
docker compose -f deploy/edge/compose.yaml up -d edge-cse
docker compose -f deploy/edge/compose.yaml run --rm edge-app --config /config/edge.json bootstrap
docker compose -f deploy/edge/compose.yaml up -d edge-app
```

컨테이너는 `core.container.json`을 사용한다. 기본 Docker 네트워크 DNS는 호스트의 `.local` 이름을 보장하지 않으므로 실제 DNS를 구성하거나 compose에 `extra_hosts`를 지정한다. CSE 데이터는 `edge-cse-data`에 저장한다. 이전 볼륨을 재사용하면 UID/GID 권한을 먼저 확인한다. `down -v`는 DB 볼륨을 삭제하므로 정상 재시작에 사용하지 않는다. Docker compose 구성 검사는 통과했지만 컨테이너 빌드·실행과 MEC Mp1 등록·Sandbox 연동은 미검증이다.


## Pi 두 대와 임시 연안 맥으로 전체 시험

실제 배포는 **선박 Pi → 연안 MEC Host → 육상 Pi**다. MEC 준비 전에는 맥에서 edge CSE·core를 네이티브로 실행해 같은 세 역할을 구성한다. 맥에 선박·육상 CSE까지 중복으로 띄우지 않는다. Linux용 `network_mode: host` Compose를 맥에서 그대로 동작한다고 가정하지 않는다.

맥에서도 Python 3.11+·git·openssl과 고정 ACME 환경을 준비한 뒤 `deploy/edge/run.sh`를 사용한다. 이미 설치된 가상환경이 있다면 `ANCHOR_VENV`로 경로를 지정한다. 맥의 IP가 `edge.local`로, 두 Pi는 `ship.local`·`shore.local`로 해석되게 세 장비의 DNS/hosts와 인증서 SAN을 맞춘다. ETSI MEC 플랫폼 등록은 이 임시 시험의 검증 범위가 아니다.

기동 순서는 육상 CSE → 연안 CSE → 선박 CSE → 각 bootstrap → 각 core → ESP32·대시보드다. 새 ACP와 토큰 설정을 적용한 뒤 다음 순서로 기록한다.

| 순서 | 시험 | 통과 근거 |
|---|---|---|
| 1 | DNS·시각·TLS·CSE 신원 | 세 CSE HTTPS 연결, 잘못된 CA/호스트명 거부, 시각 동기화 |
| 2 | 정상 수집·SDT·화면 | 6개 센서 원본 CIN → events → 육상 동일 eventId·해시, 유효 온도만 FCNT 갱신 |
| 3 | 링크 A 단절 | ESP32↔선박 내부 연결 유지, 선박 원본·사건 기록 증가, 육상 완료로 표시되지 않음 |
| 4 | A만 복구·B 단절 | 연안 custody, 육상 final 미확인; 이어 A 단절·B만 복구하면 연안 데이터가 육상 도착 |
| 5 | 재시작·중복·용량 | core/연안 CSE 재시작 후 복구, 같은 ID 중복 증가 없음, 충돌 격리, 가득 차면 기록 유실 대신 backpressure |
| 6 | 명령 전체 경로 | 육상 원문 → 연안 캐시 → PCH → 선박 GRP → BRIDGE·ENGINE 실제 결과; 한 노드만 먼저 저장된 재시도·TTL·중복도 확인 |
| 7 | ACP·API 권한 | T1-T15: Dashboard 조회·events SUB만 허용, CIN 쓰기/타 장치/ACP 변경·읽기 토큰 제어 거부 |
| 8 | 실제 하드웨어 | ESP32 재부팅 bootId·seq, 센서 품질·장치 적용 주기, MQTTS 상·하향, 카메라·20 Hz 화면 |

단위/계약 검사, 임시 DB의 3-CSE 통합 검사, 실제 3개 장비 시험 결과는 구분해서 기록한다. 아래 pytest의 3-CSE 시험은 한 호스트의 임시 CSE를 사용하므로 Pi·MEC 간 물리 통신 성공을 대신하지 않는다. 실제 장비에서는 배치 ID, eventId, 해시, CIN ct, 명령 결과와 로그를 함께 저장한다. 강제 전원 차단 내구성은 프로세스 재시작과 다른 시험이다.

## 수조 비교 시험의 baseline

비교 기준은 같은 센서·전송 간격·TLS·선박→연안→육상 경로에서 **단절 중 데이터의 보관 후 재전송·재시작 복구를 하지 않는 실시간 전달**로 잡는다. ANCHOR와 baseline에 같은 A/B 단절 시간표·측정 구간을 적용하고, 선박 원본 CIN/bootId·seq로 분모를 고정해 육상 수신율·지연·복구 시간을 비교한다. 원본은 비교 근거로 남겨도 baseline 전달기가 그 과거 기록을 복구 전송하면 안 된다.

**현재 실제 장비용 baseline 전달기는 없다.** `dashboard/`의 Baseline 전환은 더미 데이터 비교이고, `direct_shore=true`도 보관·재시도 기능이 남아 있어 무보관 baseline이 아니다. 수조 baseline 실행 전에는 위 조건의 별도 전달기를 준비해야 하며, 현재 UI 전환 결과를 실측 결과로 사용하지 않는다. 이번 수정은 ACP·모니터 권한·명령 재시도를 대상으로 했고 baseline 전달기를 추가한 것으로 표시하지 않는다.

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
./deploy/shore/run.sh command --target reportingGroup \
  --interval-ms 200 --duration-ms 30000 --ttl-ms 300000 --id demo-profile-01
# 선박: 수집분의 분모를 고정한 뒤 같은 분모로 대조
./deploy/ship/run.sh capture-batch demo-01
./deploy/ship/run.sh batch-status demo-01
```

영상에는 실제 패킷 차단/물리 단절 방식을 밝힌다. 화면의 모의 스위치는 통신 단절 증거가 아니다. GPS 실외 fix, MQ-2 배선·예열, MC-38 장착도 촬영 전에 확인한다.

## 검증

```bash
python -m pytest -q
ANCHOR_HARDWARE_INTEGRATION=1 python -m pytest tests/test_hardware_acme.py -v
ANCHOR_TLS_WIRE_INTEGRATION=1 python -m pytest tests/test_mqtt_tls_integration.py -v
python -m pytest tests/test_dds.py -q
ANCHOR_DDS_WIRE_INTEGRATION=1 python -m pytest tests/test_dds_wire_integration.py -v
scripts/check-firmware.sh
```

일반 테스트는 실제 센서 JSON 계약, 단절·재시작·중복·충돌·용량 제한·명령 결과 경계를 검사한다. 시나리오 단위 테스트는 CSE API 대역을 사용한다. MCU 시간 정책은 호스트 C++ 컴파일로 검사한다. 별도 ACME 통합 테스트는 임시 DB와 포트로 CSE 세 개를 실행한다. 결과 CIN fixture를 사용하는 부분은 실제 ESP32 시험과 구분했다.

현재 `main`의 테스트는 171개다. **수집은 실행·통과를 뜻하지 않는다.** 2026-11-18 DDS 병합 후 비통합 테스트는 **137 passed / 1 skipped**였다. Python 3.14 환경의 Cyclone DDS 미설치로 건너뛴 native QoS·CDR 검사는 별도 Python 3.12 + Cyclone DDS 11.1.0 환경에서 실행했다. 같은 환경의 **별도 프로세스 간 실제 DDS UDP 송수신**과 함께 **2 passed**를 확인했다. UDP 검사는 같은 맥의 loopback에서 수행했으며 Pi 간 무선 통신이나 성능 측정 결과는 아니다.

로컬 ACME 3개를 띄운 HTTPS 권한 검사(`-k 'agent or provision_is_repeatable'`)는 **3 passed / 18 deselected**였다. 6개 센서 CNT·CIN·최신값 조회, 부모/기록 검색, 재bootstrap 후 데이터 유지, 쓰기·구독·수정·삭제·명령 접근 거부를 확인했다. 대시보드 TypeScript 검사와 빌드도 통과했다. 기존 Wi-Fi 복구 코드와 AI 센서 권한을 유지해 DDS를 병합했다. 나머지 통합 시험, 실제 Pi·ESP32·MEC 연결과 MQTTS → DDS → CSE 전체 경로 시험은 이번에 실행하지 않았다.

이전 TLS 단계의 66 passed / 18 skipped는 이번 수정 이후 결과가 아니다. 당시 메모리 TLS 핸드셰이크·CA/SAN·클라이언트 인증서·역할별 묶음 분리를 검사했다. 실제 소켓 시험은 도구 환경의 포트 개설 제한과 Mosquitto 공유 라이브러리 누락으로 실행하지 못했다. 3-CSE HTTPS와 MQTTS 상·하향 시험 코드는 준비되어 있다. ESP32 전체 빌드·플래시와 실물 통신은 아직 미검증이다.

| 경로 | 역할 |
|---|---|
| `hardware/firmware/` | 실제 센서 드라이버와 공통 통신·명령 처리 |
| `src/anchor/events.py`, `incidents.py` | 원본 변환, 단위·품질, 기울기 사건 |
| `src/anchor/runtime.py`, `store.py` | 역할 분리, CSE 보관·전달·확인서 |
| `src/anchor/commands.py`, `resources.py` | 실제 명령, PCH/GRP/SUB/ACP/SDT |
| `src/anchor/service.py`, `dashboard.html` | 세 위치 관제 API·화면 |
| `src/anchor/auth.py` | 운영자·상태 조회 토큰 분리, 모니터 응답에서 원본·위치 제외 |
| `deploy/{ship,edge,shore}/` | 역할 고정 실행기·HTTPS 설정, 선박 MQTTS, 연안 컨테이너 |
| `src/anchor/tls.py`, `acme_tls.py` | 인증서 검증·ACME TLS 호환 실행기 |
| `src/anchor/dds/`, `deploy/{ship,edge}/dds.json` | native DDS publisher·IPE, QoS 6종, 파형 변환·최소 ACP |
| `scripts/provision-tls.py`, `package-role.py` | 인증서 발급·역할별 배포 묶음 |
| `src/anchor/lab/` | 이전 모의 구현 |

개발 문서·PDF·도표는 로컬 자료이며 커밋에서 제외한다. README는 포함한다.
