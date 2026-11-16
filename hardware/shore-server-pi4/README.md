# 육상 서버 (Raspberry Pi 4)

> 통합 실행은 [프로젝트 README](../../README.md)를 따른다. 아래는 기존 하드웨어 구축 기록이다. 현재 동기화는 선박·연안·육상 core로 교체했으며, 구 FIFO 동기화기를 함께 실행하지 않는다.

육상 관제실 역할을 하는 Pi 4다. **oneM2M IN-CSE**를 돌리면서 선박 MN-CSE의 등록을 받고, 선박이 보내는 데이터 복제본을 보관한다. 같은 Pi에서 관제 대시보드도 띄운다.

| 서비스 (systemd) | 포트 | 소스 | 역할 |
|---|---|---|---|
| `anchor-shore-cse` | 8080 | [`shore-cse/`](shore-cse/) | ACME IN-CSE `/id-in` (`cse-in`) |
| `anchor-shore-dashboard` | 8090 (내부 알림용 8091) | [`shore-dashboard/`](shore-dashboard/) | 육상 관제 대시보드 |

## IN-CSE 설정 요점 (`shore-cse/acme.ini`)

- `cseType=IN`, `cseID=id-in`, `cseName=cse-in`. 선박 CSE는 `registrarCse*`에 이 값들을 적어 이 CSE에 등록한다.
- **ACP 검사는 켜 둔다.** 대신 `[cse.registration] allowedCSROriginators=/id-ship`로 선박 CSE 하나만 `remoteCSE` 등록을 허용한다.
  - 값 앞에 `/`를 붙여 `/id-ship`으로 써야 한다. ACME 기본 설정 파일 주석에는 "붙이지 말라"고 되어 있지만, 붙이지 않으면 시작 단계에서 설정 오류로 바로 꺼진다.
- **복제 데이터 위치:** `cse-in/ship-gw/<노드>/<센서>`. 선박의 `anchor-sync`가 AE `ship-gw`(`CShipGW`)로 만든다. 컨테이너마다 최근 5000건(`mni`)을 보관한다.
- **선박 직접 조회:** 링크가 살아 있을 때는 `http://<pi4>:8080/~/id-ship/cse-ship/...`처럼 선박 자원을 바로 조회할 수 있다. IN이 요청을 선박으로 전달한다.

## 관제 대시보드 (`shore-dashboard/`)

선박을 직접 조회하지 않고, **육상 IN-CSE에 복제된 데이터만** 읽는다. 그래서 링크가 끊기면 실제 육상 운영자가 보는 것과 똑같이 값이 멈추고, 복구되면 빈 구간이 채워진다.

| 동작 | 내용 |
|---|---|
| **데이터 수신** | `ship-gw` 컨테이너들을 구독해서, 복제되는 CIN을 하나도 빠짐없이 알림으로 받는다. 복구 직후 몰려오는 데이터도 놓치지 않는다. |
| **상태 판정** | `ONLINE` / `OUTAGE`(12초 넘게 수신 없음) / `CATCHING_UP`(복구 후 보관 데이터를 받는 중). 상단 배너 색과 문구가 이 상태를 따른다. |
| **그래프** | 시간축은 도착 시각이 아니라 `ship_ct`(선박 생성 시각)다. 두절 구간은 띠로 표시하고, 늦게 도착한 값은 주황색으로 그린다. |
| **누락 검증** | 동기화가 끝나면 ESP32가 붙인 `seq` 순번에 빈 번호가 있는지 세어 이벤트 로그에 남긴다. |
| **화면 구성** | 3D 선체(1Hz, 두절 중에는 회색으로 "마지막 보고 자세" 표시), 선박 카메라 중계, 이벤트 로그. |

선박 카메라 주소는 환경변수 `ANCHOR_SHIP_CAMERA`로 바꿀 수 있다. 기본값은 `http://10.251.196.250:8090/stream.mjpg`다.

## 설치

```bash
mkdir -p ~/anchor && python3 -m venv ~/anchor/venv && ~/anchor/venv/bin/pip install acmecse   # 2026.5.3에서 확인
cp -r shore-cse shore-dashboard ~/anchor/
# shore-cse/acme.ini의 cseHost를 이 Pi의 IP로, *.service의 User=pi와 /home/pi를 실제 계정으로 고친 뒤
sudo cp ~/anchor/*/anchor-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anchor-shore-cse anchor-shore-dashboard
```

접속 주소: `http://<pi4-ip>:8090`

## 단절 리허설

선박 Pi 5에서 아래 명령을 실행하면 육상 Pi 4로 가는 통신을 90초 동안 막았다가 자동으로 푼다.

```bash
sudo sh -c 'iptables -I OUTPUT -d <pi4-ip> -j DROP; iptables -I INPUT -s <pi4-ip> -j DROP; sleep 90;
            iptables -D OUTPUT -d <pi4-ip> -j DROP; iptables -D INPUT -s <pi4-ip> -j DROP' &
```

육상 대시보드는 12초 안에 "선박 통신 두절"로 바뀐다. 차단이 풀리면 "보관 데이터 동기화 중"을 거쳐 "동기화 완료 · 순번 검증: 누락 없음"이 이벤트 로그에 남는다.
