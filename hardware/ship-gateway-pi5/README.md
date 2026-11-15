# 선박 게이트웨이 (Raspberry Pi 5)

보트에 싣는 Pi 5다. ESP32 노드의 데이터를 받는 **oneM2M MN-CSE**이자, 그 데이터를 육상으로 넘기는 게이트웨이다. 육상 링크가 끊겨도 혼자 계속 동작한다.

| 서비스 (systemd) | 포트 | 소스 | 역할 |
|---|---|---|---|
| `mosquitto` | 1883 | [`system/mosquitto-anchor.conf`](system/mosquitto-anchor.conf) | ESP32 ↔ ACME 사이 MQTT 브로커 (oneM2M MQTT 바인딩 중계) |
| `chrony` | 123/udp | [`system/chrony-anchor.conf`](system/chrony-anchor.conf) | ESP32 시각 동기화용 NTP 서버 |
| `anchor-ship-cse` | 8080 | [`ship-cse/`](ship-cse/) | ACME MN-CSE `/id-ship` (`cse-ship`), 육상 IN-CSE에 등록됨 |
| `anchor-sync` | 9091 (내부) | [`sync/anchor_sync.py`](sync/anchor_sync.py) | 선박→육상 복제(저장 후 전달)와 링크 모니터 |
| `anchor-dashboard` | 8090 | [`dashboard/`](dashboard/) | 선박 대시보드: 3D 선체 자세, 카메라, 노드 값, 영점 버튼 |

## 동기화 에이전트 `anchor-sync`

선박 컨테이너 7개를 구독(SUB)해 새 CIN을 알림으로 받는다.

| 기능 | 하는 일 |
|---|---|
| **저장 후 전달** | 받은 CIN은 먼저 SQLite 큐(`queue.db`, WAL 모드)에 쓴다. 그다음 육상 IN-CSE `cse-in/ship-gw/<노드>/<센서>`에 CIN으로 만들고, 성공하면 큐에서 지운다. 링크가 끊겨 있으면 큐에 계속 쌓는다(최대 20만 건). 복구되면 들어온 순서대로 다시 보낸다. |
| **링크 모니터** | 5초마다 육상 CSE의 응답 시간(RTT), 손실률, Pi 5의 Wi-Fi 신호 세기, 큐 길이를 재서 `cse-ship/sync/link`에 기록한다. 상태는 `ONLINE` / `DEGRADED` / `OFFLINE` 셋 중 하나다. 이 컨테이너도 복제 대상이라, 끊긴 동안의 링크 상태 변화가 나중에 육상에 남는다. |
| **자가 복구** | 1분마다 AE와 구독이 있는지 확인한다. 알림이 끊긴 구독은 다시 만든다. 선박 CSE DB가 초기화되는 경우와, ACME 재시작 뒤 구독이 남아 있는데도 알림을 보내지 않는 경우 둘 다 이걸로 복구된다. |

육상 주소는 환경변수 `ANCHOR_SHORE_HOST`로 바꿀 수 있다. 기본값은 `10.251.196.64`다.

## 설치

```bash
sudo apt install -y mosquitto chrony python3-venv python3-picamera2
sudo cp system/mosquitto-anchor.conf /etc/mosquitto/conf.d/anchor.conf
sudo cp system/chrony-anchor.conf    /etc/chrony/conf.d/anchor.conf
sudo systemctl restart mosquitto chrony

mkdir -p ~/anchor && python3 -m venv ~/anchor/venv && ~/anchor/venv/bin/pip install acmecse   # 2026.5.3에서 확인
cp -r ship-cse sync dashboard ~/anchor/
# *.service의 User=pi 와 /home/pi 를 실제 계정에 맞게 고친 뒤
sudo cp ~/anchor/*/anchor-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anchor-ship-cse anchor-dashboard anchor-sync
```

`ship-cse/acme.ini`에서 고칠 값:
- `cseHost`: 육상에서 이 Pi에 접근할 IP. 아래 "네트워크"를 참고한다.
- `registrarCseHost`: 육상 Pi 4의 IP.

## 운용 메모

- **TinyDB 손상:** ACME는 TinyDB(JSON 파일)를 쓴다. 전원이 갑자기 끊기면 파일이 깨질 수 있다.
  - `check_db.py`(ExecStartPre)가 부팅 때 DB를 검사한다. 깨져 있으면 `~/anchor/ship-cse-db-corrupt-<시각>`으로 옮기고 새 DB로 시작한다. ESP32와 에이전트가 자원을 다시 만들기 때문에 서비스는 자동으로 복구된다.
  - **전원을 뽑기 전에는 `sudo shutdown -h now`로 종료한다.**
- **cseHost 변경:** ACME는 `cseHost`를 CSEBase를 처음 만들 때만 쓴다. 나중에 IP를 바꾸려면 두 가지가 필요하다.
  - DB에 저장된 CSEBase `poa`를 고친다.
  - 양쪽의 CSR(선박 `cse-ship/id-in`, 육상 `cse-in/id-ship`)을 지워서 다시 등록하게 한다.
- **ACP:** ACP 검사는 꺼 두었다(`enableACPChecks=false`). 팀의 AI 앱과 대시보드, 그리고 육상 CSE를 거쳐 오는 조회가 ESP32가 만든 자원을 읽어야 하기 때문이다. 육상 CSE는 ACP를 켜 두었다.
- **카메라:** 대시보드는 IMX219(CAM/DISP0)를 picamera2로 MJPEG 스트리밍한다. 프레임이 5초 넘게 들어오지 않으면 프로세스를 종료하고 systemd가 다시 띄운다. 리본 케이블이 헐거우면 이 증상이 생긴다.

## 네트워크

- **지금 (테스트):** ESP32, Pi 5, Pi 4가 모두 폰 핫스팟 하나에 붙어 있다.
  - Pi 5에는 보조 고정 IP(`10.251.196.250`)를 하나 더 붙여 `cseHost`로 쓴다. 핫스팟이 Pi와 ESP32에 같은 DHCP 주소를 주는 일이 있었기 때문이다.
  - ESP32는 mDNS로 Pi를 찾는다.
- **예정:** Pi 5 내장 Wi-Fi(`wlan0`)를 AP로 만들어 선박 내부망으로 쓰고, 외장 Netgear A6210(`wlan1`)을 육상 링크로 쓴다. 이렇게 망을 나누면 육상 링크가 끊겨도 ESP32 → MN-CSE 수집은 그대로 이어진다.
