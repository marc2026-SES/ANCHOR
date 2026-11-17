# ESP32 센서 노드 펌웨어

ESP32 세 대가 각자 센서를 읽어 선박 Pi 5의 ACME MN-CSE에 oneM2M 리소스로 저장한다. 통신은 Wi-Fi 위의 **oneM2M MQTT 바인딩 + TLS(MQTTS 8883)**이다.

| 스케치 | 보드 (FQBN) | 센서 | AE / Originator | 컨테이너와 주기 |
|---|---|---|---|---|
| [`node_bridge`](node_bridge/node_bridge.ino) | ESP32-S3 (`esp32:esp32:esp32s3`) | MPU6050, GPS NEO-6M | `bridge` / `CBridge` | `attitude` 1Hz, `position` 0.5Hz, `command` (명령 수신) |
| [`node_engine`](node_engine/node_engine.ino) | ESP32-WROOM-32E (`esp32:esp32:esp32`) | ADXL345, MQ-2 | `engine` / `CEngine` | `vibration` 1Hz (200Hz 샘플링, 1초 RMS와 최대값), `gas` 2초 |
| [`node_cargo`](node_cargo/node_cargo.ino) | ESP32-C6 (`esp32:esp32:esp32c6:CDCOnBoot=cdc`) | DHT11, MC-38 | `cargo` / `CCargo` | `climate` 5초, `hatch` 상태가 바뀔 때 즉시 + 30초마다 |

핀 배선은 [../docs/wiring.md](../docs/wiring.md)에 있다.

## 통합 core·보안 전송 변경 (2026-11-17)

센서 드라이버·핀·영점 보정은 유지했다. 통신은 MQTTS로 전환했으며 ArduinoOTA는 코드만 보관하고 기본 비활성화했다. 새 core에 맞춰 CSE의 CNT 자동 축출을 끄고 `bootstrap`한 뒤 펌웨어를 갱신한다. 자세한 전환 순서는 [루트 README](../../README.md)에 있다.

- `bootId`, `vesselId`, `deviceId`, `eventId`, `uptimeMs`, `droppedBefore`를 원래 JSON에 추가한다.
- 최대 32건 RAM 큐에 넣고 같은 CIN 이름·본문으로 재시도한다. CSE가 중복을 응답하면 기존 본문을 읽어 비교한다. 큐 초과와 CSE 수신 전 전원 손실은 구분한다.
- `node.enableReportProfile()`을 쓰는 BRIDGE/ENGINE은 `setReportProfile`을 처리한다. TTL·장치 대상·범위를 검사하고, 최근 8개 명령 결과와 마지막 적용 순서를 NVS에 남긴다. 중복은 유지 시간을 연장하지 않는다.
- BRIDGE는 CIN 간격, ENGINE은 200 Hz 샘플 통계 창을 조절한다. 기본 1,000 ms, 명령 범위 200~5,000 ms, 유지 1~300초다. CARGO/DHT11 주기는 그대로다.
- 결과는 각 AE의 `commandResults`에 실제 요청·상태·적용 간격·적용 시각·복귀 시각으로 저장된다. Core는 유실된 명령 알림을 재전송한다.
- 시각 동기화 전에는 원격 프로파일을 적용하지 않는다. BRIDGE의 기존 문자열 `zero`/`unzero` 콜백은 유지한다.
- `scripts/check-firmware.sh`가 S3/WROOM/C6 세 타깃을 빌드한다. 현재 개발 환경에서는 ESP32 툴체인이 없어 전체 빌드·실물 검증이 남아 있다.


## 공통 라이브러리 `AnchorNode`

[`libraries/AnchorNode/AnchorNode.h`](libraries/AnchorNode/AnchorNode.h)는 헤더 하나로 된 라이브러리다. 세 노드의 통신 처리를 모두 맡기 때문에, 노드 스케치는 센서를 읽고 `node.publish("attitude", json)`을 부르는 일만 한다.

| 기능 | 하는 일 |
|---|---|
| **연결** | Wi-Fi에 접속하고, 끊기면 10초마다 다시 붙는다. Pi 5는 mDNS 이름(기본 `ship`)으로 찾고, 실패하면 `CSE_HOST`로 접속한다. MQTTS가 끊기면 3초마다 다시 접속한다. |
| **등록** | AE와 CNT를 만든다. 이미 있으면(4117/4105 응답) AE를 업데이트하는 것으로 넘어간다. 그래서 선박 CSE DB가 초기화돼도 노드가 알아서 다시 등록한다. |
| **데이터** | CIN의 `con`에 부팅·이벤트 ID, `seq`(노드별 순번), `ts`(UTC 밀리초)를 붙인다. 시각은 Pi 5의 chrony에서 SNTP로 받는다. |
| **명령** | `<ae>/command` 컨테이너와 SUB를 만들어 둔다. CSE가 MQTTS로 보낸 알림을 받으면 콜백을 호출한다. BRIDGE 영점 조정과 BRIDGE/ENGINE 보고 주기 명령이 이 경로를 쓴다. |
| **실시간 경로** | `publishLive()`로 oneM2M을 거치지 않는 별도 MQTTS 토픽의 메시지를 보낸다. BRIDGE 자세 20Hz 스트림(`anchor/live/bridge/attitude`)이 대시보드 3D 애니메이션용으로 이걸 쓴다. CSE에는 저장되지 않는다. |
| **인증** | Pi의 CA·서버 이름을 확인하고 보드별 클라이언트 인증서를 제출한다. 시각이 없거나 TLS 검증에 실패하면 센서·명령 통신을 열지 않는다. |
| **업로드** | USB를 기본으로 사용한다. 기존 ArduinoOTA 코드는 남아 있지만 `ANCHOR_ENABLE_OTA=0`이다. OTA 프로토콜은 이번 HTTPS·MQTTS 범위에 포함되지 않는다. |

MQTT 메시지 예시 (CIN 생성):
```
topic: /oneM2M/req/CBridge/id-ship/json
{"op":1,"to":"cse-ship/bridge/attitude","fr":"CBridge","rqi":"CBridge-12","rvi":"3","ty":4,
 "pc":{"m2m:cin":{"cnf":"application/json:0","con":"{\"roll\":-4.3,\"pitch\":4.5,...,\"seq\":1098,\"ts\":1794728878289}"}}}
```

## 빌드와 업로드

1. Arduino IDE 또는 arduino-cli, **Arduino-ESP32 3.3.x**, PubSubClient, ArduinoJson 7, DHT sensor library를 준비한다. TLS의 IP 주소·서버 이름 분리 API 때문에 3.3.x를 대상으로 한다.
2. `libraries/AnchorNode/anchor_config.example.h`를 `anchor_config.h`로 복사한다. Wi-Fi, `CSE_HOST`(주소 검색 실패 시 사용할 IP), `CSE_MDNS`(Pi 이름), `MQTT_TLS_HOST`(인증서 DNS 이름), `MQTT_PORT=8883`을 설정한다. 기존 설정 파일을 쓰는 경우에도 포트와 TLS 이름을 갱신한다.
3. 저장소 루트에서 `python3 scripts/provision-tls.py --output .runtime-pki`로 발급한 `firmware/anchor_bridge_tls.h`, `anchor_engine_tls.h`, `anchor_cargo_tls.h`를 `libraries/AnchorNode/`에 넣는다. 각 스케치는 자기 인증서·키만 포함한다. CA의 개인키는 장치에 넣지 않는다. 이 파일들과 Wi-Fi 설정은 Git·배포 소스 묶음에서 제외한다.
4. 선박 Pi의 Mosquitto에 서버 인증서를 배치하고 MQTTS·클라이언트 인증을 켠다. chrony 시각도 확인한다. [루트 README](../../README.md#httpsmqtts-설정)에 배치·실행 순서가 있다.
5. 저장소 루트에서 `scripts/check-firmware.sh`로 세 보드를 빌드한다. 보드 업로드 예시는 다음과 같다. 터미널 경로는 `hardware/firmware`이며 USB 포트는 실제 값으로 바꾼다.

```bash
arduino-cli compile --fqbn esp32:esp32:esp32s3 --library libraries/AnchorNode --output-dir /tmp/anchor-bridge node_bridge
arduino-cli upload --fqbn esp32:esp32:esp32s3 -p /dev/ttyACM0 --input-dir /tmp/anchor-bridge node_bridge
```

WROOM과 C6는 위 표의 FQBN과 각 스케치를 사용한다. C6는 `CDCOnBoot=cdc`를 사용한다. 기존 NVS 영점값·명령 이력은 별도 namespace에 있으므로 무조건 flash 전체를 지우지 않는다.

현재 환경에는 ESP32 툴체인이 없어 이번 TLS 펌웨어 전체 빌드는 미검증이다. 장치 업로드 후 CA·호스트명 불일치 차단, 센서 CIN 저장, 명령 수신·결과 회수, 시각 동기화와 재접속을 확인해야 한다.
