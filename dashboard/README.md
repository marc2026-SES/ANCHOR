# ANCHOR Shore Dashboard

육상 관제 대시보드입니다. 팀 저장소의 ANCHOR core(`src/anchor`, 이벤트 형식 schemaVersion 2) 구조에 맞춰 만들었습니다. 기본 화면은 **더미 데이터**이고, 주소 뒤에 `?live`를 붙이면 **Shore Monitor AE**(`server/shore_monitor.py`)가 육상 CSE에서 읽은 실데이터를 보여 줍니다.

## 실행

```bash
npm install
npm run dev        # http://localhost:5180        더미 데이터
                   # http://localhost:5180/?live  실데이터 (Shore Monitor 필요, 아래 참고)
```

빌드: `npm run build` → `dist/` (외부 CDN 없이 오프라인으로 동작, Pretendard 폰트 포함). `npm run preview`(5181)도 `/api` 연결이 됩니다.

## Shore Monitor AE (실데이터 연결)

육상 CSE의 `anchor/events`를 `CAnchorDashboard`로 구독하고, 이벤트를 SQLite에 저장한 뒤 대시보드에 `/api/run`으로 넘깁니다. Python 3.9 이상, 표준 라이브러리만 씁니다(설치할 것 없음).

| 무엇을 | 어디서 | 왜 |
| --- | --- | --- |
| 이벤트·명령 (`shoreAt`) | 육상 CSE `anchor/events`, `anchor/commands` | 육상에 도착한 데이터 = 측정의 기준 |
| 연안 저장 시각 (`coastAt`) | 연안 CSE `anchor/events`, `anchor/commands` (선택) | A·B 구간 지연을 나눠 보려면 필요 |
| 링크 A·B 상태, 선박 보관·연안 대기 건수 | 선박·연안 core의 `/status` (선택) | 육상 CSE만으로는 알 수 없는 값 |

구독 알림은 "새 기록이 있다"는 신호로만 쓰고, 기록은 항상 CSE에서 다시 읽습니다(알림 본문은 믿지 않음). 알림이 안 와도 2초마다 직접 확인합니다.

### 1. 준비 (시우 쪽, 11/17 반영됨)

팀 core(`src/anchor/resources.py`)의 bootstrap이 `CAnchorDashboard`에 아래 권한을 줍니다. 세 CSE에서 bootstrap을 다시 돌려야 적용됩니다.

| ACP | 권한 | 비고 |
| --- | --- | --- |
| 육상 `acp-anchor-events` | R·Di (34) + 구독 만들기만 (`acop` 1, `acod: [{chty: [23]}]`) | 구독은 되고 기록 쓰기는 안 됨 |
| 육상 `acp-anchor-receipts`, `acp-anchor-commands` | R·Di (34) | |
| 연안 `acp-anchor-events`, `acp-anchor-commands` | R·Di (34) | 연안 설정 `dashboard_edge_read`(기본 켜짐). `coastAt`용 |

모든 연결은 팀 사설 CA로 발급한 인증서를 쓰는 HTTPS입니다(`scripts/provision-tls.py`). core API는 모니터용 **읽기 전용 토큰**(`status-token`, `GET /status`만 됨)을 따로 둡니다. 시우한테 받을 것:

- 팀 CA 인증서 `ca.crt` (공개해도 되는 파일)
- 선박·연안 core의 `status-token` 파일 2개 (운영자 `api-token` 아님. 채팅 말고 파일로)
- 실제 주소. 팀 기본값은 `https://shore.local:8080`(육상 CSE), `https://edge.local:8080`(연안 CSE), `https://ship.local:8080`(선박 CSE), `https://ship.local:8790`·`https://edge.local:8790`(core API). 인증서에 들어간 이름이나 IP로만 접속됩니다.

### 2. 설정

```bash
cd server
cp shore-monitor.example.json shore-monitor.json   # git에 안 올라감
mkdir -p secrets                                    # git에 안 올라감
# secrets/ca.crt, secrets/ship-status-token, secrets/edge-status-token 넣기
```

`shore-monitor.json`에서 채울 것:

- `shore.url`: 육상 CSE 주소 (예: `https://shore.local:8080`)
- `edge.url`: 연안 CSE 주소 (선택, `coastAt`)
- `ship.url`: 선박 CSE 주소. 모니터는 안 쓰고 거부 테스트(5번)만 씀
- `cores.ship.url`, `cores.edge.url`: 선박·연안 core API 주소 (선택, 링크 상태·밀린 건수). 토큰은 `secrets/`의 `status-token` 파일에서 읽음
- `tls.ca`: 팀 CA 인증서 (기본 `secrets/ca.crt`). HTTPS 주소에는 필수이고, 검증은 끌 수 없음
- `notify.url`, `tls.cert`, `tls.key`: 구독 알림을 받을 때만 (아래)
- `run.id`, `run.mode`: 처음 시작할 때의 실행 이름과 방식(`anchor` / `baseline`)

**어디서 돌릴지.** 육상 CSE는 HTTPS로만, 인증서를 검증하고 알림을 보냅니다. 그래서 알림을 받으려면 이 컴퓨터에 팀 CA가 발급한 서버 인증서가 있어야 합니다.

| 실행 위치 | 알림 | 설정 |
| --- | --- | --- |
| 육상 Pi (권장) | 받음 | `notify.url`: `https://127.0.0.1:8796/notify`, `tls.cert`·`tls.key`: 육상 Pi의 `tls/server.crt`, `tls/server.key` |
| 노트북 | 안 받음 | `notify.url`을 비움. 2초마다 직접 읽음 |

알림은 "새 기록이 있다"는 신호일 뿐이라 측정값은 같습니다(`shoreAt`은 CSE가 기록한 시각). 화면 갱신만 최대 2초 늦어집니다.

육상 core와 운영자 `api-token`은 넣지 않습니다. 운영자 토큰으로는 명령을 보내거나 사건을 확인할 수 있는데 모니터에는 필요 없습니다. 육상 core를 넣으면 시작하지 않도록 막아 두었습니다.

### 3. 실행

```bash
python3 server/shore_monitor.py --config server/shore-monitor.json check   # 읽기만: 연결·권한 확인 표
python3 server/shore_monitor.py --config server/shore-monitor.json serve   # 계속 실행
npm run dev                                                                 # → http://localhost:5180/?live
```

`check`에서 `[--]`가 나오면 그 줄의 이유를 보고 고치면 됩니다. 예: `4103` = ACP 권한 없음, `CERTIFICATE_VERIFY_FAILED` = CA 파일이 다르거나 인증서에 없는 이름·IP로 접속함.

모니터가 육상 Pi에 있고 대시보드는 노트북에서 볼 때는, 모니터 쪽 `api.bind`를 `0.0.0.0`으로 바꾸고 `api.token_file`을 정합니다. 이때 API는 `tls.cert`로 HTTPS가 됩니다(토큰이 평문으로 다니지 않게). 노트북에서:

```bash
ANCHOR_MONITOR=https://shore.local:8795 ANCHOR_MONITOR_CA=server/secrets/ca.crt \
ANCHOR_MONITOR_TOKEN_FILE=<API 토큰 파일> npm run dev
```

### 4. 수조 시험 실행 구분

시험 하나마다 새 실행(run)을 시작합니다. **모니터가 도는 컴퓨터에서** curl로 합니다(대시보드 주소로는 막혀 있음).

```bash
curl -X POST localhost:8795/api/runs -d '{"runId":"pool-anchor-01","mode":"anchor"}'   # 시작 (이전 실행은 닫힘)
curl -X POST localhost:8795/api/runs/close                                             # 끝
curl localhost:8795/api/runs                                                           # 목록
```

### 5. 거부 테스트 (ACP 확인)

ACP 문서의 T1~T10을 실제 CSE에 보내고, 결과를 `src/security/deny-results.json`에 씁니다. Security 탭이 이 파일을 보여 줍니다.

```bash
python3 server/deny_tests.py --config server/shore-monitor.json             # 전부
python3 server/deny_tests.py --config server/shore-monitor.json --only T2,T8 # 일부만 (나머지 결과는 유지)
```

- 필요한 주소: `ship.url`(T1~T4), `shore.url`(T5, T6, T8~T10), `cores.ship.url`(T7). 비어 있으면 그 테스트는 `skipped`.
- 테스트마다 그 테스트의 주체 이름(`CEngine`, `CAnchorShip` 등)으로 요청을 보냅니다. ACP는 요청 헤더의 이름만 보기 때문입니다(ACP 문서 원칙 6).
- ACP가 잘못돼 요청이 통과해도 데이터는 남지 않습니다.
  - 쓰기 테스트(T1, T3, T4, T9)는 core가 격리하고 ESP32가 무시하는 표시 레코드를 쓰고, 통과하면 바로 지웁니다.
  - T5(육상 레코드 삭제)는 대상 레코드를 `server/data/`에 먼저 백업하고, 지워지면 다시 만듭니다. 다시 만들었는지 확인될 때만 백업을 지웁니다.
  - T6은 영수증 컨테이너를 지금 값 그대로 UPDATE합니다. 레코드(CIN) UPDATE는 ACME가 ACP를 보기 전에 4005로 막아서 권한 확인이 안 되기 때문입니다.
  - T10의 구독 알림 주소는 닫힌 포트라서 무엇도 받지 않습니다.
- 결과 파일에는 테스트 번호, 응답 코드, CSE 이름만 들어갑니다(주소 없음). 아티클 근거로 git에 올려도 됩니다.
- 기대와 다르면 종료 코드 1, 표에 `fail`과 이유가 나옵니다.

### 6. AI 결과 (AI 탭)

AI 결과는 `anchorAI/` 아래(`aiStatus`, `anomaly`, `summary`, `prognosis`, `linkForecast`)에 있습니다(자연의 "AI → 대시보드 연동 스펙"). 모니터는 5초마다 `CAnchorDashboard` 신원으로 읽고, `CAdmin`으로는 읽지 않습니다.

- 읽는 곳: 육상 CSE의 복제본(`cse-in/anchorAI`)을 먼저 보고, 없으면 선박 CSE(`ship.url`)를 봅니다. 화면 AI 상태 칸에 어디서 읽었는지 나옵니다. 순서는 `"ai": {"sources": ["shore", "ship"]}`로 바꿀 수 있습니다.
- 육상 복제본에 새 `aiStatus`가 안 들어오면 선박 CSE를 확인해서 원인을 나눕니다.
  - 선박에 접속 안 됨 → **Ship not reachable** (링크 A 끊김. AI 고장 아님)
  - 선박에는 새 결과가 있음 → 복제가 늦는 것. 따라잡을 때까지 선박에서 읽음
  - 선박에도 새 결과가 없음 → **AI not responding**
- 화면: AI 탭에 서브시스템별 상태(정상/주의/심각), 이상 탐지 목록(점수, 원인 상위 3개), 고장 예고와 통신 끊김 예측, 1분 요약. 고장 예고(남은 시간)와 최근 2분 안의 심각한 이상은 어느 탭에서든 위에 띄웁니다.
- 상태 표시

  | 상태 | 뜻 |
  | --- | --- |
  | AI online | 상태 신호가 들어오는 중 |
  | AI not responding | 새 `aiStatus`가 90초 넘게 없음 (육상 복제본·선박 둘 다) |
  | AI not running | 육상·선박 CSE 어디에도 `anchorAI`가 없음 (AI 시작 전) |
  | Ship not reachable | 새 결과가 없는데 선박 CSE에도 접속 안 됨 (링크 A 끊김). AI 고장 아님 |
  | No permission | ACP에 `CAnchorDashboard` 읽기 권한이 없음 |

- 필요한 것
  - `anchorAI` 아래 자원에 `CAnchorDashboard` R Di 권한 (시우). 선박과 육상 복제본 둘 다
  - AI가 결과를 쓸 때 `CAdmin`이 아닌 자기 신원 `CAnchorAgent`로 쓰기 (자연). 복제본을 쓰는 쪽도 같음
  - 육상 복제본 경로가 `cse-in/anchorAI/<같은 이름>`인지 확인 (자연·시우)
  - 선박 CSE 주소는 HTTPS (`https://ship.local:8080`)
- 같은 결과가 선박과 복제본 양쪽에서 읽혀도 `eventId`로 한 번만 보여줍니다.
- 한계: 링크 A가 끊긴 동안에는 새 AI 결과가 육상에 안 옵니다. 끊긴 동안 생긴 이상·예측은 선박에 남아 있다가 다시 연결되면 보입니다(시험에서 확인). 복제가 ANCHOR 저널을 타지 않으면 우선 전송·수신 확인은 없습니다.
- `linkForecast`의 `lat`·`lon`은 저장 전에 지웁니다(`redact_position`).
- 연안 선단 AI: `cse-edge/fleetAI/`의 `fleetStatus`(30초)·`conflicts`(충돌 경고)도 같은 방식으로 읽어 AI 탭의 "Fleet · coastal AI" 카드에 보여줍니다. `edge.url`이 있어야 하고, 끄려면 `"fleet": {"enabled": false}`. 필드 이름은 fleet-ai가 쓰는 그대로 보여주고, 위치 필드는 지웁니다.
- 끄기: 설정에 `"ai": {"enabled": false}`.

### 실데이터일 때 화면에서 달라지는 점

- 링크 A·B: 기본은 선박·연안 core의 보고입니다. core는 다음 전송 시도 때 끊김을 알아채므로 실제보다 몇 초 늦게 바뀝니다.
- core에 접속이 안 될 때는 데이터 공백으로 추정하고 **inferred**로 표시합니다. 배가 핫스팟에서 멀어지면 선박 core에도 닿지 않기 때문입니다.
  - A 끊김: 연안 도착이 `infer_gap_seconds`(기본 10초) 넘게 멈췄고, 그사이 선박에서 만든 데이터가 나중에 도착함.
  - B 끊김: 연안에 들어온 데이터가 그 시간 넘게 육상으로 못 넘어감.
  - 끊긴 시각 = 마지막 도착, 복구 시각 = 다시 도착한 첫 데이터(core의 재시도 지연 포함). 오차는 core 전송 간격 정도입니다.
  - 그 core에 접속되던 구간에서는 추정하지 않고 core 보고만 씁니다. 연안 저널(`edge.url`)이 없으면 A·B를 구분할 수 없어 추정하지 않습니다.
  - 추정도 판단할 근거가 없으면 회색 **no report**입니다.
- 손실: 육상 기준 `(deviceId, bootId)`별 seq 공백. 선박 보관·연안 대기 건수는 core의 수신 확인 숫자.
- 명령: 선박이 받은 시각은 육상에서 안 보이고, ESP32 결과(`applied`)가 도착하면 적용으로 표시합니다.
- 위치: GPS 이벤트가 `anchor/events`에도 복사되므로, 모니터는 저장 전에 `lat`·`lon`을 지웁니다(`redact_position`).

### 보안 정리

- 모니터가 CSE에 쓰는 것: AE `dashboard` 등록(없을 때만)과 구독 1개. 그 밖에는 읽기만 합니다.
- 모든 HTTPS는 팀 CA로 검증하고, 검증을 끄는 옵션은 없습니다. 리다이렉트도 따라가지 않습니다(팀 core와 같음).
- core에는 읽기 전용 `status-token`만 씁니다. `/status`에는 위치·사건 원문이 없습니다.
- `/api`는 기본으로 이 컴퓨터(127.0.0.1)에서만 HTTP로 열립니다. 다른 곳에 열려면 토큰과 HTTPS 인증서가 있어야 시작됩니다.
- 대시보드의 `/api` 연결은 읽기(GET)만 넘깁니다. `npm run dev`는 같은 네트워크에 열리기 때문입니다.
- 알림 받는 포트(8796)는 아무 요청이나 받지만, 확인을 한 번 앞당기거나 구독이 있는지 다시 보는 것 외에는 영향이 없습니다(기록은 CSE에서만 읽음).
- git에 안 올리는 것: `server/shore-monitor.json`, `server/secrets/`(CA·토큰), `server/data/`(SQLite, T5 백업).

## 육상 Pi4에 올리기 (최종 구성)

Shore Monitor와 대시보드 화면을 육상 Pi4에서 돌리고, 노트북이나 큰 화면의 브라우저로 `https://shore.local:8795`에 접속해서 봅니다. Pi4에는 Python만 있으면 되고(기본 설치됨), Node는 필요 없습니다. 팀 화면(8090)과 포트가 겹치지 않습니다.

준비물: Pi4 SSH 계정(`pi`, sudo 가능), 선박·연안 `status-token` 파일 2개.

**노트북에서**

```bash
npm run pack:pi                                    # 빌드 + anchor-dashboard.tgz (설정·토큰은 안 들어감)
scp anchor-dashboard.tgz pi@shore.local:~
scp ship-status-token edge-status-token pi@shore.local:~   # 시우한테 받은 파일
ssh pi@shore.local
```

**Pi4에서** (`ssh`로 들어간 상태)

```bash
sudo mkdir -p /opt/anchor-dashboard && sudo chown pi:pi /opt/anchor-dashboard
tar xzf ~/anchor-dashboard.tgz -C /opt/anchor-dashboard
cd /opt/anchor-dashboard/server
cp shore-monitor.pi.example.json shore-monitor.json          # 주소 확인 (아래)
mkdir -p secrets && chmod 700 secrets
mv ~/ship-status-token ~/edge-status-token secrets/ && chmod 600 secrets/*
python3 shore_monitor.py --config shore-monitor.json init-token   # 브라우저용 토큰 생성 (덮어쓰지 않음)
python3 shore_monitor.py --config shore-monitor.json check        # 전부 [ok]인지 확인
sudo cp ../deploy/anchor-shore-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now anchor-shore-monitor
systemctl status anchor-shore-monitor          # active (running) 이면 끝
```

`shore-monitor.pi.example.json`에는 팀 기본값이 들어 있습니다. 육상 CSE는 같은 기계라 `https://127.0.0.1:8080`, 연안·선박은 `edge.local`·`ship.local`, 인증서는 팀 core와 같은 `/opt/anchor/.runtime-shore/tls/`. 실제 주소가 다르면 이 파일만 고칩니다.

**브라우저에서 보기**

1. 처음 한 번, 팀 CA를 노트북에 신뢰 등록합니다. macOS: `ca.crt` 더블클릭 → 키체인 접근에서 "ANCHOR Local Root CA" → 신뢰 → "항상 신뢰". (팀 화면 8090을 볼 때도 필요)
2. `https://shore.local:8795` 접속 → 토큰 입력. 토큰은 Pi에서 `cat /opt/anchor-dashboard/server/secrets/api-token`으로 확인합니다. 토큰은 그 탭에만 남고, 새로고침하면 다시 입력합니다.

**시험 구분** (Pi4에서, 토큰 필요 없음)

```bash
cd /opt/anchor-dashboard/server
python3 shore_monitor.py --config shore-monitor.json run-start --id pool-anchor-01 --mode anchor
python3 shore_monitor.py --config shore-monitor.json run-close
python3 shore_monitor.py --config shore-monitor.json runs        # 목록
```

**문제가 생기면**

- 로그: `journalctl -u anchor-shore-monitor -f`
- 다시 시작: `sudo systemctl restart anchor-shore-monitor`
- 새 버전 올리기: 노트북에서 `npm run pack:pi` → `scp` → Pi에서 `tar xzf ~/anchor-dashboard.tgz -C /opt/anchor-dashboard` → 다시 시작. 설정·토큰·기록(`server/shore-monitor.json`, `secrets/`, `data/`)은 묶음에 없어서 그대로 남습니다.
- 거부 테스트는 노트북에서 돌립니다(CA만 있으면 됨). 결과 파일이 노트북의 `src/security/`에 저장돼야 화면에 반영되기 때문입니다.

## 구조 전제 (팀 core)

```
선박 Pi 5 (ship CSE + core)  ←A→  연안 (edge CSE + Edge 앱)  ←B→  육상 Pi 4 (shore CSE + core)
```

- A(선박↔연안)와 B(연안↔육상)는 따로 연결되고, 동시에 연결돼 있을 필요가 없습니다.
- 데이터는 각 CSE의 `anchor/events` 저널에 같은 이벤트 ID로 저장되고, `anchor/receipts`에 연안 보관 확인(custody)과 육상 최종 확인(final)이 따로 남습니다.
- 대시보드는 이벤트마다 **연안 사본의 저장 시각(coastAt)** 과 **육상 사본의 저장 시각(shoreAt)** 을 씁니다(각 CSE의 CIN `ct`).

## 화면

**항상 보이는 영역** (탭과 상관없이 위에 고정)

| 영역 | 내용 |
| --- | --- |
| 상태 바 | 링크 A·B 상태, 육상 마지막 수신 후 경과, 선박 보관 건수, 연안 대기 건수, ANCHOR / Baseline 전환 |
| 재생 바 | 재생·일시정지, 속도(1·10·30·60배), 시점 이동 |
| 지표 카드 | 육상 최종 확인 비율, 손실, 실시간 지연(A·B 구간), 보관 데이터 지연(긴급 따로), 밀린 데이터 처리 시간, 육상 명령 상태 |

**탭**

| 탭 | 내용 |
| --- | --- |
| Overview | 데이터 연속성(10초당 육상 도착 건수, 선박·연안 대기 건수), 선박·연안·육상에 데이터가 몇 건씩 있는지, 이벤트(링크·기울기 사건·명령·재부팅) |
| Sensors | BRIDGE(roll·pitch), ENGINE(진동 RMS, MQ-2 전압 V), CARGO(온도, 해치) 그래프와 센서 품질(bootId, seq 공백, GPS fix, DHT11, MQ-2 예열). x축 = 노드 생성 시각, 기다렸다 도착한 데이터는 보라색 |
| Measurement | ANCHOR vs Baseline 비교표, 육상 도착 지연 분포, 실행별 CSV 내보내기 |
| Security | bootstrap이 만드는 ACP 목록(자원 이름 = ACP 이름), 거부 테스트 T1~T8, 전송 구간·프라이버시 |

## 측정 지표

| 지표 | 정의 |
| --- | --- |
| 손실 | 끝까지 전달되지 않은 이벤트 수. `(deviceId, bootId)`별 seq 공백으로 확인 (재부팅하면 새 bootId라 공백으로 세지 않음) |
| 실시간 지연 | `shoreAt − generatedAt`, 10초 넘게 기다리지 않은 이벤트. A 구간 = `coastAt − generatedAt`, B 구간 = `shoreAt − coastAt` |
| 보관 데이터 지연 | 같은 계산, 선박이나 연안에서 기다렸던 이벤트 (긴급 = 사건·명령 결과·현장 확인) |
| 밀린 데이터 처리 시간 | 링크가 다시 연결된 시각부터, 그 전에 대기하던 마지막 이벤트가 건너편에 저장될 때까지 |
| 육상 명령 | 육상 발행 → 연안 보관 → 선박 수신 → ESP32 적용 결과(`applied`), 유효시간(TTL) 안에 적용됐는지 |

Baseline은 보관 없이 실시간으로만 전달하는 방식입니다. A나 B가 끊긴 동안 생긴 데이터는 전달되지 않습니다.

## 구조

```
src/
  types.ts                 이벤트(schemaVersion 2) + coastAt/shoreAt, 링크 변화, 명령
  mock/scenario.ts         더미 데이터: README 촬영 순서(A·B를 따로 끊음), 20분, 약 4,000건
  lib/metrics.ts           측정 지표 정의 (실데이터에도 그대로 사용)
  lib/csv.ts               결과 파일 내보내기 (events.csv, links.csv)
  lib/live.ts              ?live: /api/run을 2초마다 읽어 더미와 같은 Run 형태로 바꿈
  security/acp.ts          bootstrap ACP 목록, 거부 테스트, 전송 구간 보호
  components/              패널·탭별 컴포넌트
  App.tsx                  화면 배치, 탭, 재생 상태
server/
  shore_monitor.py         Shore Monitor AE (구독, 저장, /api)
  deny_tests.py            거부 테스트 T1~T10 → src/security/deny-results.json
  shore-monitor.pi.example.json  육상 Pi4용 설정 예시
deploy/anchor-shore-monitor.service  Pi4 자동 실행 (systemd)
scripts/pack-pi.sh       Pi4로 옮길 묶음 만들기 (npm run pack:pi)
  shore-monitor.example.json  설정 예시 (주소는 비어 있음)
```

## 다음 단계

- [x] Shore Monitor AE: 육상(·연안) `anchor/events`를 구독하고 CIN `ct`를 `shoreAt`(`coastAt`)으로 저장 → `?live`로 연결 (로컬 3사이트 테스트 통과)
- [ ] 육상·연안 ACP에 `CAnchorDashboard` 권한 추가 (위 표, 시우와 협의)
- [ ] 실제 육상 CSE 주소로 `check` → `serve`
- [ ] 수조 시험 ANCHOR / Baseline 실측 CSV
- [ ] 거부 테스트 T1~T8 실행 후 Security 탭 결과 반영
