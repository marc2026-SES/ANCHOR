# 선박 → 육상 전달 (relay)

애플리케이션 코어를 제거한 뒤, 선박 센서 기록을 육상 CSE로 넘기는 일은 `anchor_relay.py`가 맡는다.
현장 장비(선박 Pi 5, 육상 Pi 5)는 2026-11-19부터 이 구성으로 돌고 있다.

## 구성

| 호스트 | 실행 |
| --- | --- |
| 선박 | `anchor-cse@ship`, `anchor-relay`, `anchor-dashboard@ship`, MQTTS 브로커 |
| 육상 | `anchor-cse@shore`, Shore Monitor |

relay는 `src/anchor`의 `client`, `config.Endpoint`, `tls`만 쓴다. 설정은 `deploy/ship/relay.json`이다.

## 동작

1. 센서 컨테이너 6개에 구독 `anchor-core`(`nct=1`, `net=[3]`)를 유지한다. 선박 CSE가 새 CIN을 `notify_url`(8790)로 보낸다.
2. 알림에 담긴 CIN을 schemaVersion 2 envelope으로 감싼다. 이름은 `eventId`에서 정해지는 `e-<sha256 앞 32자>`다.
3. 선박의 SQLite 큐(`.runtime-ship/relay-queue.db`)에 넣고, 육상 `anchor/events`에 CIN 생성 요청을 **1번** 보낸다.
4. 실패하면 큐에 남기고 0.5~5초 간격으로 다시 시도한다. 4105(이미 있음)는 전달된 것으로 본다.

envelope 형식과 이름 규칙은 제거 전 코어와 같아서, 육상의 기존 기록·읽는 쪽(Shore Monitor)과 호환된다.

## 필요한 CSE 자원과 권한

relay는 자원이나 ACP를 만들지 않는다. 아래가 미리 있어야 한다.

| CSE | 자원 | relay originator(`CAnchorShip`) 권한 |
| --- | --- | --- |
| 선박 | `bridge/{attitude,position}`, `engine/{vibration,gas}`, `cargo/{climate,hatch}` | 조회, 구독 생성 |
| 육상 | `anchor/events` | CIN 생성 (C·R·Di = 35) |

현장 CSE에는 제거 전 bootstrap이 만든 ACP가 그대로 있다. **새로 초기화한 CSE에는 이 권한을 만드는 절차가 저장소에 없다**(bootstrap이 코어와 함께 제거됨).

## 하지 않는 것

- 영수증(`anchor/receipts`), 선박 저널, 원격 명령, 기울기 사고 판정, batch 대조
- relay가 꺼져 있는 동안 생긴 CIN의 보충: 알림을 받지 못한 기록은 넘어가지 않는다.
- `:8790/status`는 relay 카운터다(`status_token_files`의 토큰 필요). 제거 전 코어의 `/status`와 형식이 다르다.

## 측정 (ESP32 3대, 초당 약 3.2건)

| 구성 | ESP32 생성 → 육상 CSE 저장 |
| --- | --- |
| 제거 전 코어, 육상 Pi 4 | 약 18초, 기록이 쌓이면 분 단위 |
| relay, 육상 Pi 4 | 중앙값 0.57초, p90 2.0초 |
| relay, 육상 Pi 5 | 중앙값 0.19초, 최대 0.54초 |

육상 호스트를 끈 약 2분 동안 411건이 큐에 쌓였고, 다시 켠 뒤 약 3분 만에 중복·실패 없이 전달됐다.
장시간(수천 건 이상) 운용은 아직 측정하지 않았다.

## 설치

```bash
# deploy/ship/relay.json의 nodes.ship/shore 주소·포트를 실제 배포에 맞춘다
sudo install -m 644 deploy/systemd/anchor-relay.service /etc/systemd/system/   # User= 를 실행 계정으로 맞춘다
sudo systemctl daemon-reload
sudo systemctl enable --now anchor-relay
```

읽기 전용 점검(큐·CSE에 쓰지 않음):

```bash
.venv/bin/python hardware/ship-gateway-pi5/sync/anchor_relay.py --config deploy/ship/relay.json --check
```
