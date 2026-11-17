# ANCHOR

oneM2M Hackathon 2026 · 팀 Ctrl+Sea

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
| 연안 `cse-edge` | `shipCommands/pch/pcu`: 선박 core가 폴링하는 경로 |
| 육상 `cse-in` | `edgeCommands/pch/pcu`: 연안 core가 폴링하는 경로; `anchor/events/sub-shore-dash`: 육상 센서 화면 구독 |

중괄호 안은 서로 다른 자식 리소스다. 선박의 `temperature`는 SDT 온도를 표현하는 **FCNT**이며 `curT0`는 그 속성이다. `reportingGroup`은 **GRP**이고 `fopt`는 가상 팬아웃 경로다. `pch`는 **PCH**, `pcu`는 가상 폴링 경로이며 명령 원문은 여기에 보관하지 않는다.

선박 core는 센서 CNT 6개와 장치 결과 CNT 2개 아래에 `anchor-core` SUB를 생성한다. BRIDGE/ENGINE 펌웨어는 각 `command` CNT 아래에 `sub-node` SUB를 생성한다. 연안·육상에는 원본 센서 트리를 복제하지 않고 `anchor/events`로 전달한다.

각 CSE 루트에는 다른 core의 신원을 등록하는 `peer-*` AE 2개, `dashboard` AE와 ACP도 있다. ACP는 루트에 두고 대상 리소스의 `acpi`가 참조한다. 이 `peer-*` AE는 ACME가 CSE 등록 시 만드는 CSR과 다르다. 실제 CSE에는 ACME 기본·관리 리소스 및 등록 상태에 따른 CSR이 추가될 수 있다.

전체 리소스 트리는 로컬 통합 문서 `docs/ANCHOR_OVERVIEW.pdf`·`.md`의 **20절 통합본(선박 → 연안 → 육상) · 21절 선박 · 22절 연안 · 23절 육상**에 흰 바탕 도형으로 수록했다. 모든 리소스를 옆으로 나누지 않고 한 열로 내려 읽도록 배치했으며, 들여쓰기와 연결선으로 부모·자식 관계를 나타냈다. AE·CNT·CIN·SUB·PCH·GRP·FCNT·ACP와 가상 경로를 구분해 표시했다. **19절**에는 읽는 법, **24절**에는 ACP 연결·생성 시점·개수를 정리했다. 코드 근거는 `src/anchor/resources.py`, `runtime.py`, `commands.py`, 펌웨어 `AnchorNode.h`와 육상 `shore_server.py`다.

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
| 연안 Pi / MEC Host | `anchor-edge.tar.gz` | edge CSE, edge core | 센서 드라이버·카메라·MQTT 브로커 |
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

MQTTS의 클라이언트 인증과 **oneM2M ACP 권한 정책은 별개**다. 기존 ACP 검사는 유지했다. 인증서 CN ↔ MQTT 토픽 ↔ oneM2M Originator를 묶는 세부 권한 정책, HTTPS 호출자 인증 강화, 기존 센서 화면의 운영자 권한은 다음 ACP 단계의 대상이다. TLS 적용만으로 모든 사용자의 데이터 접근 권한까지 완성된 것은 아니다. DDS는 아직 추가하지 않았다.

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
./deploy/ship/run.sh cse          # 터미널 1
./deploy/ship/run.sh bootstrap    # 터미널 2: CSE 준비 후
./deploy/ship/run.sh core         # 터미널 2
./deploy/ship/run.sh dashboard    # 터미널 3: picamera2 시스템 Python
```

연안 Pi / MEC Host:

```bash
cd /opt/anchor
./deploy/edge/run.sh check
./deploy/edge/run.sh init-cse
./deploy/edge/run.sh init-token
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
# 연안 Pi (Docker를 쓸 때는 이 줄을 실행하지 않음)
sudo systemctl enable --now anchor-cse@edge anchor-core@edge
# 육상 Pi
sudo systemctl enable --now anchor-cse@shore anchor-core@shore anchor-dashboard@shore
```

## 연안 컨테이너 실행 선택지

Linux 연안 호스트에서만 `deploy/edge/compose.yaml`을 사용한다. 연안 네이티브 서비스와 동시에 실행하지 않는다. CSE와 앱 컨테이너는 분리되어 있고 같은 연안 인증서 묶음을 읽는다. TLS 디렉터리의 소유 UID/GID와 컨테이너 UID/GID가 같아야 한다.

```bash
./deploy/edge/run.sh init-token   # 아직 토큰이 없을 때만
export ANCHOR_UID="$(id -u)" ANCHOR_GID="$(id -g)"
docker compose -f deploy/edge/compose.yaml build
docker compose -f deploy/edge/compose.yaml up -d edge-cse
docker compose -f deploy/edge/compose.yaml run --rm edge-app --config /config/edge.json bootstrap
docker compose -f deploy/edge/compose.yaml up -d edge-app
```

컨테이너는 `core.container.json`을 사용한다. 기본 Docker 네트워크 DNS는 호스트의 `.local` 이름을 보장하지 않으므로 실제 DNS를 구성하거나 compose에 `extra_hosts`를 지정한다. CSE 데이터는 `edge-cse-data`에 저장한다. 이전 볼륨을 재사용하면 UID/GID 권한을 먼저 확인한다. `down -v`는 DB 볼륨을 삭제하므로 정상 재시작에 사용하지 않는다. Docker compose 구성 검사는 통과했지만 컨테이너 빌드·실행과 MEC Mp1 등록·Sandbox 연동은 미검증이다.


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
scripts/check-firmware.sh
```

일반 테스트는 실제 센서 JSON 계약, 단절·재시작·중복·충돌·용량 제한·명령 결과 경계를 검사한다. 시나리오 단위 테스트는 CSE API 대역을 사용한다. MCU 시간 정책은 호스트 C++ 컴파일로 검사한다. 별도 ACME 통합 테스트는 임시 DB와 포트로 CSE 세 개를 실행한다. 결과 CIN fixture를 사용하는 부분은 실제 ESP32 시험과 구분했다.

현재 자동 테스트는 66 passed / 18 skipped다. 실제 인증서로 TLS MemoryBIO 핸드셰이크, 정상 CA/SAN, 잘못된 CA/호스트명 거부, MQTT 클라이언트 인증서 필수, 역할별 묶음 분리와 비밀키 제외를 검사했다. 3-CSE 테스트는 HTTPS로 전환했고 MQTTS 상·하향 통합 테스트를 추가했다. 이 환경에서는 포트 개설이 차단되고 Mosquitto 실행 파일의 공유 라이브러리가 누락되어 실제 소켓 시험을 실행하지 못했다. ESP32 툴체인도 없어 전체 펌웨어 빌드와 실물 시연은 미검증이다. TLS 메모리 시험 통과는 실제 Pi 통신 성공을 뜻하지 않는다.

| 경로 | 역할 |
|---|---|
| `hardware/firmware/` | 실제 센서 드라이버와 공통 통신·명령 처리 |
| `src/anchor/events.py`, `incidents.py` | 원본 변환, 단위·품질, 기울기 사건 |
| `src/anchor/runtime.py`, `store.py` | 역할 분리, CSE 보관·전달·확인서 |
| `src/anchor/commands.py`, `resources.py` | 실제 명령, PCH/GRP/SUB/ACP/SDT |
| `src/anchor/service.py`, `dashboard.html` | 세 위치 관제 API·화면 |
| `deploy/{ship,edge,shore}/` | 역할 고정 실행기·HTTPS 설정, 선박 MQTTS, 연안 컨테이너 |
| `src/anchor/tls.py`, `acme_tls.py` | 인증서 검증·ACME TLS 호환 실행기 |
| `scripts/provision-tls.py`, `package-role.py` | 인증서 발급·역할별 배포 묶음 |
| `src/anchor/lab/` | 이전 모의 구현 |

개발 문서·PDF·도표는 로컬 자료이며 커밋에서 제외한다. README는 포함한다.
