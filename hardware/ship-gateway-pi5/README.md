# 선박 Pi 5

배포 설정과 서비스 정의는 `deploy/ship/` 및 `deploy/systemd/`로 통합했다.
설치·인증서·DB 이관·실행 명령은 [루트 README](../../README.md#역할별-배포와-실행)를 따른다.

이 폴더에는 실제 하드웨어 화면 소스를 유지한다. 기존 CSE DB와 `check_db.py`는 자동 실행하거나 초기화하지 않는다.
새 실행 진입점은 `./deploy/ship/run.sh cse`, `core`, `dashboard`다.

## 현장 장비 기준 (2026-11-17)

- 무선 분리: 내장 `wlan0` = 선박 AP `ANCHOR-SHIP`(NetworkManager AP, `ipv4.method shared`), USB 동글 `wlan1`(MT7610U) = 육상 링크.
  ESP32는 `WIFI_SSID`(선박 AP)를 우선 쓰고, `WIFI_SSID2`(육상 핫스팟)는 예비로만 쓴다.
- `uplink-watch/`: `wlan1`이 끊겨 있으면 6초마다 재스캔·재접속한다(NetworkManager 백오프 때문에 범위 복귀 후 늦게 붙는 문제 방지).
  설치: 저장소가 `/opt/anchor`에 있다고 보고 `anchor-uplink-watch.service`를 복사해 enable(root로 실행, nmcli 필요). 육상 링크 프로필은 `connection.autoconnect-retries 0`.
- `sync/anchor_sync_legacy.py`: core 이관 전까지 실제 Pi 5에서 돌던 동기화 에이전트(SUB → SQLite 큐 → 육상 `ship-gw`, `sync/link` 링크 감시).
  `ANCHOR_SHORE_HOST`, `ANCHOR_UPLINK_IF`(기본 `wlan1`) 환경변수로 설정한다.
- `dashboard/`: 그래프 5분 표시, `sync/link` 기록으로 육상 두절 구간 음영 표시, 원격 보기 끊김 문구, 복귀 시 카메라 자동 재연결.
