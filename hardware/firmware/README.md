# ESP32 센서 노드 펌웨어

ESP32 세 대가 각자 센서를 읽어 선박 Pi 5의 ACME MN-CSE에 oneM2M 리소스로 저장한다. 통신은 Wi-Fi 위의 **oneM2M MQTT 바인딩**이다.

| 스케치 | 보드 (FQBN) | 센서 | AE / Originator | 컨테이너와 주기 |
|---|---|---|---|---|
| [`node_bridge`](node_bridge/node_bridge.ino) | ESP32-S3 (`esp32:esp32:esp32s3`) | MPU6050, GPS NEO-6M | `bridge` / `CBridge` | `attitude` 1Hz, `position` 0.5Hz, `command` (명령 수신) |
| [`node_engine`](node_engine/node_engine.ino) | ESP32-WROOM-32E (`esp32:esp32:esp32`) | ADXL345, MQ-2 | `engine` / `CEngine` | `vibration` 1Hz (200Hz 샘플링, 1초 RMS와 최대값), `gas` 1Hz |
| [`node_cargo`](node_cargo/node_cargo.ino) | ESP32-C6 (`esp32:esp32:esp32c6:CDCOnBoot=cdc`) | DHT11, MC-38 | `cargo` / `CCargo` | `climate` 5초, `hatch` 상태가 바뀔 때 즉시 + 30초마다 |

핀 배선은 [../docs/wiring.md](../docs/wiring.md)에 있다.

## 공통 라이브러리 `AnchorNode`

[`libraries/AnchorNode/AnchorNode.h`](libraries/AnchorNode/AnchorNode.h)는 헤더 하나로 된 라이브러리다. 세 노드의 통신 처리를 모두 맡기 때문에, 노드 스케치는 센서를 읽고 `node.publish("attitude", json)`을 부르는 일만 한다.

| 기능 | 하는 일 |
|---|---|
| **연결** | Wi-Fi에 접속하고, 끊기면 10초마다 다시 붙는다. Pi 5는 mDNS 이름(`piking`)으로 찾고, 실패하면 `CSE_HOST`로 접속한다. MQTT가 끊기면 3초마다 다시 접속한다. |
| **등록** | AE와 CNT를 만든다. 이미 있으면(4117/4105 응답) AE를 업데이트하는 것으로 넘어간다. 그래서 선박 CSE DB가 초기화돼도 노드가 알아서 다시 등록한다. |
| **데이터** | CIN의 `con`에 `seq`(노드별 순번)와 `ts`(UTC 밀리초)를 자동으로 붙인다. 시각은 Pi 5의 chrony에서 SNTP로 받는다. |
| **명령** | `<ae>/command` 컨테이너와 SUB를 만들어 둔다. CSE가 MQTT로 보낸 알림을 받으면 콜백을 호출한다. BRIDGE의 영점 조정이 이 경로를 쓴다. |
| **실시간 경로** | `publishLive()`로 oneM2M을 거치지 않는 일반 MQTT 메시지를 보낸다. BRIDGE 자세 20Hz 스트림(`anchor/live/bridge/attitude`)이 대시보드 3D 애니메이션용으로 이걸 쓴다. CSE에는 저장되지 않는다. |
| **OTA** | ArduinoOTA로 무선 업데이트를 받는다. 포트는 3232, 호스트명은 `anchor-<ae>`, 비밀번호는 `OTA_PASS`다. |

MQTT 메시지 예시 (CIN 생성):
```
topic: /oneM2M/req/CBridge/id-ship/json
{"op":1,"to":"cse-ship/bridge/attitude","fr":"CBridge","rqi":"CBridge-12","rvi":"3","ty":4,
 "pc":{"m2m:cin":{"cnf":"application/json:0","con":"{\"roll\":-4.3,\"pitch\":4.5,...,\"seq\":1098,\"ts\":1794728878289}"}}}
```

## 빌드와 업로드

1. **준비물:** Arduino IDE 또는 arduino-cli, esp32 보드 패키지(3.3.x에서 확인), **PubSubClient** 라이브러리.
2. **설정 파일:** `libraries/AnchorNode/anchor_config.example.h`를 `anchor_config.h`로 복사한 뒤 Wi-Fi 정보, `CSE_HOST`, `OTA_PASS`를 채운다. 이 파일은 git에 올라가지 않는다.
3. **USB로 처음 업로드:**
   ```bash
   arduino-cli compile --fqbn esp32:esp32:esp32s3 --library libraries/AnchorNode --output-dir /tmp/b node_bridge
   arduino-cli upload  --fqbn esp32:esp32:esp32s3 -p COM5 --input-dir /tmp/b node_bridge
   ```
   - 프로젝트 경로에 한글이 있으면 링커가 실패한다. `--output-dir`은 ASCII 경로로 지정한다.
4. **이후에는 무선으로(OTA):** `ota.env`를 만들어 `ANCHOR_PI`(Pi 5 ssh 주소), `ANCHOR_OTA_PASS`, `ARDUINO_CLI`를 적은 뒤 실행한다.
   ```bash
   ./ota.sh bridge 192.168.50.22   # <bridge|engine|cargo> <노드 IP>
   ```
   - 펌웨어는 노트북에서 ssh로 Pi 5에 올라간 뒤, Pi 5가 ESP32로 보낸다.
   - Pi 5의 `~/anchor/ota/`에 `espota.py`가 있어야 한다. esp32 Arduino 코어의 `tools` 폴더에 있는 파일을 복사해 둔다.

## 운용 메모

- **MPU6050 장착:** 부품면이 위, X 화살표가 선수 방향이 되게 단다. 이 방향일 때 roll +는 우현이 내려간 것이고, pitch +는 선수가 내려간 것이다.
- **자이로 보정:** 부팅 직후 2초 동안 자이로 바이어스를 보정한다. 그동안 보드를 움직이지 않는다.
- **영점 조정:** `bridge/command`에 `"zero"` CIN을 만들면 현재 자세가 0°로 플래시에 저장된다. 선박 대시보드의 버튼이 이걸 대신 해 준다. `"unzero"`를 보내면 해제된다.
- **MQ-2:** 켠 뒤 120초까지는 `warm=false`(예열 중)로 표시된다. AO 출력은 반드시 분압을 거쳐 GPIO34에 연결한다.

## `tools/` 센서 테스트 스케치

oneM2M 없이 센서만 시리얼로 확인할 때 쓴다.

| 스케치 | 용도 |
|---|---|
| `test_bridge`, `test_engine`, `test_cargo` | 노드별 센서를 읽어 시리얼로 출력 |
| `i2c_probe`, `i2c_probe_s3` | I2C 주소 스캔 (WROOM, S3) |
| `dht_probe_c6`, `pin_probe_c6` | C6에서 DHT11 응답과 핀 상태 확인 |
