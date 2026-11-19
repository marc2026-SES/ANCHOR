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
4. 육상이 4105(같은 이름이 이미 있음)를 반환하면 해당 CIN 하나만 GET으로 읽는다. 저장된 `con` 문자열이 선박 큐의 본문과 정확히 일치할 때만 전달 완료로 처리한다. 본문이 다르거나 조회에 실패하면 큐 맨 앞에 원본을 유지하고 0.5~5초 간격으로 재시도한다. 이 기록이 확인될 때까지 뒤의 기록도 대기한다.

envelope 형식과 이름 규칙은 제거 전 코어와 같아서, 육상의 기존 기록·읽는 쪽(Shore Monitor)과 호환된다.

## 필요한 CSE 자원과 권한

relay는 자원이나 ACP를 만들지 않는다. 아래가 미리 있어야 한다.

| CSE | 자원 | relay originator(`CAnchorShip`) 권한 |
| --- | --- | --- |
| 선박 | `bridge/{attitude,position}`, `engine/{vibration,gas}`, `cargo/{climate,hatch}` | 조회, 구독 생성 |
| 육상 | `anchor/events` | CIN 생성 (C·R·Di = 35) |

새 CSE를 시작한 뒤 각 호스트에서 한 번 `./deploy/<role>/run.sh acp-provision`을 실행한다. 역할은 `ship`, `edge`, `shore` 중에서 고른다. 이 명령은 해당 CSE에 `main`과 같은 ACP와 리소스별 연결을 설정한다. 정책을 다시 설치할 때는 달라진 권한과 연결만 갱신한다.

연안 CSE를 Compose로 배포했다면 저장소 루트에서 `docker compose -f deploy/edge/compose.yaml exec edge-cse python -m anchor.provision --config /config/host.container.json`을 실행한다.

릴레이의 대상 CSE, 요청자, 컨테이너, 작업(CREATE), 리소스 타입(CIN)은 실행 중 고정된다. 릴레이는 이 조합의 첫 CREATE 성공을 메모리 한 칸에 기록한다. CSE가 권한을 거부하면 즉시 비우고 해당 기록을 큐에 남긴다. 첫 전송과 이후 전송 모두 별도 권한 조회 요청은 0회다.

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
