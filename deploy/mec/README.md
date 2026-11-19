# ANCHOR core · MEC 핸드오버

`ship-001`의 선박 릴레이가 부산에서는 `mep1`, 규슈에서는 `mep2`로 센서 이벤트를 보낸다. 두 엣지 릴레이는 각자의 CSE에 보관된 원본 CIN의 이름과 본문을 유지한 채 실제 육상 Pi의 `cse-in/anchor/events`로 전송한다. 선박 큐에서 한 건을 지우는 시점은 해당 엣지가 저장에 성공했거나, 같은 이름과 본문의 CIN이 이미 있음을 확인한 직후다. 육상 도착은 육상 CSE에서 확인한다.

| 프로세스 | 배치 | CSE | 릴레이 상태 포트 |
|---|---|---|---|
| `anchor-ship-app` | UE `ship-001` | 센서 미러 `cse-ship` | 30792 |
| `anchor-edge-app` | `mep1` | `cse-edge` · 30080 | 30790 |
| `anchor-edge-b-app` | `mep2` | `cse-edge-b` · 30084 | 30794 |

## 샌드박스 배치

1. 팀의 배포용 `anchor-edge-mec` 시나리오 JSON을 준비한다. 11월 19일 활성 시나리오에는 위 세 앱과 양쪽 CSE가 이미 배치돼 있었다. 생성기는 해당 배치와 포트를 유지하고 앱 이미지를 교체한다. 시나리오 원본과 생성본은 TLS 개인키나 토큰을 포함할 수 있으므로 저장소 밖에 보관한다.
2. 저장소 루트에서 릴레이 이미지를 빌드해 샌드박스가 읽을 수 있는 레지스트리에 올린다. 아래 태그의 `$REGISTRY`는 해당 레지스트리 주소다. 노드가 ARM이면 `MEC_PLATFORM=linux/arm64`를 지정한다. 커밋 전 시험용 이미지는 고유한 테스트 태그를 사용한다.

   ```sh
   IMAGE="$REGISTRY/anchor-core-relay:$(git rev-parse --short HEAD)"
   docker buildx build --platform "${MEC_PLATFORM:-linux/amd64}" \
     -f deploy/mec/Dockerfile.relay -t "$IMAGE" --push .
   ```

3. 센서 미러의 입력을 유지한다. 별도 서비스가 선박 미러의 여섯 센서 컨테이너에 CIN을 계속 생성하는 배치에서는 다음과 같이 시나리오 사본을 만든다.

   ```sh
   python3 deploy/mec/make_scenario.py /private/path/anchor-edge-mec.json \
     /private/path/anchor-core-handover.json --image "$IMAGE" --external-mirror
   ```

   기존 선박 앱이 미러 복사기까지 함께 실행한다면 `--external-mirror` 대신 `--mirror-command` 뒤에 **기존 이미지에서 미러만 실행하는 명령과 인수**를 준다. 생성기는 그 이미지를 UE에 별도 프로세스로 배치한다. 기존 앱의 전체 실행 명령을 지정하면 두 전송기가 함께 동작하게 된다. 배포 전에 실제 이미지에서 복사기의 진입점과 인증서 초기화 방식을 확인한다.

4. 샌드박스 배포 설정에서 선박 큐와 각 엣지 커서용으로 앱마다 별도의 영속 볼륨을 `/var/lib/anchor`에 연결한다. 이 경로는 이미지의 `anchor` 사용자가 쓸 수 있어야 한다. Kubernetes에서 Pod가 교체될 때 같은 PVC를 다시 연결한다. 양쪽 CSE의 저널도 각자의 영속 볼륨을 쓰고, 전송 대기 중인 CIN을 자동 삭제하지 않도록 보관 용량을 잡는다. 엣지 커서를 잃으면 보관된 CIN을 재전송해 복구할 수 있다. 선박 큐를 잃으면 아직 엣지에 보내지 않은 이벤트가 사라진다.
5. 시나리오 사본을 AdvantEDGE에 가져온 뒤 활성화한다. 같은 클러스터에서 운영본과 동시에 시험하려면 사본의 외부 NodePort를 별도로 할당한다. 활성화 후 각 앱의 `/health`와 선박 미러의 새 센서 CIN을 확인한다. 앱은 자신이 배치된 로컬 CSE에만 `core`의 ACP·리소스를 초기화한다. 육상 Pi에는 이미 `CAnchorEdge`의 이벤트 생성과 해당 CIN 읽기 권한이 있어야 한다.

### 설정

이미지는 시나리오의 기존 환경변수를 읽는다. 공통으로 `ROLE`, `APP_SVC`, `TLS_CA_B64`, `TLS_CERT_B64`, `TLS_KEY_B64`를 사용한다. 상태 API 토큰은 `ANCHOR_API_TOKEN`으로 전달한다.

- **선박**에서 `SHIP_CSE_URL`, `EDGE_CSE_URL`, `EDGE_B_CSE_URL`, `LOCATION_URL`을 지정한다. `LOCATION_URL`은 내부 Location Service의 버전 경로까지다. 릴레이가 `/queries/users`를 붙인다. 기본 선박 주소는 `ship-001`이며 `LOCATION_ADDRESS`로 바꿀 수 있다.
- **각 엣지**에서 `EDGE_CSE_URL`, `EDGE_CSE_ROOT`, `EDGE_CSE_ID`, `SHORE_CSE_URL`을 지정한다. 실제 육상 Pi가 별도 CA를 쓰면 `SHORE_CA_B64`도 전달한다. 릴레이와 CSE 간에는 인증서를 검증하는 HTTPS를 사용한다.
- 위치를 2초마다 별도 스레드에서 읽는다. 위성 접속, 알 수 없는 구역, 조회 실패에서는 마지막 선택을 유지한다. 처음 시작해 아직 연안 구역을 본 적이 없으면 선박 큐에 보관한다. 재시작하면 같은 SQLite 파일에서 마지막 엣지를 복구한다.
- 엣지는 구독 알림으로 새 CIN을 식별한다. 알림이 빠졌거나 앱이 재시작하면 로컬 CSE의 `st`(상태 태그)와 SQLite 커서로 구간을 복구한다. 구간당 최대 50개 상태 태그를 읽는다. ACME 내부의 필터 조회 비용은 CSE 구현과 보관 건수에 따라 달라진다.

## 시연과 검증

선박의 토큰 인증 `GET /status`에서 `handover.activeEdge`, `changedAt`, `firstStoredAt`을 기록한다. 두 시각은 같은 선박 릴레이에서 측정한 Unix 초다. 차이는 **릴레이가 구역 변경을 관측한 뒤 새 엣지의 첫 저장 응답을 받기까지**의 시간이다. 첫 이벤트가 아직 없으면 `firstStoredAt`은 null이다.

부산 → 규슈 → 부산 순서로 선박을 이동시키고, 각 구역에서 새 센서 이벤트를 발생시킨다. 전환 직전에 A가 저장한 기록은 A가 계속 육상으로 보낸다. 응답이 유실된 기록은 선박이 B로 다시 보낼 수 있다. 시험 종료 후 선박이 받아들인 ID와 육상 CIN의 ID·본문을 대조하고, 같은 ID로 저장된 육상 CIN 수를 센다. 선박의 `delivery.edgeStored`는 현 프로세스가 확인한 엣지 저장 건수다.

로컬 통합 시험은 임시 HTTPS CSE 네 개와 테스트 인증서를 사용한다. 실제 SUB 알림, 엣지 간 전환, 응답 유실, 재시작, 육상 중복 대조를 시험한다.

```sh
ANCHOR_TLS_WIRE_INTEGRATION=1 python -m pytest -q tests/test_mec_handover.py
```
