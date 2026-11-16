# ANCHOR

oneM2M Hackathon 2026 · 팀 Ctrl+Sea

## oneM2M Core

해상 통신 단절 중 선박 데이터를 ACME CSE에 보관하고, 복구 후 육상 CSE로 전달하는 core다. 육상에서 보낸 명령을 선박이 가져와 처리하고 실행 결과를 육상으로 회신한다. 센서 수집, 구독, 데이터 대조, pollingChannel 명령 수신, group 제어, 역할별 ACP를 실제 oneM2M API로 구현한다.

**ACME 원본은 변경하지 않는다. 사용자 플러그인, 내부 API 호출, monkey patch를 사용하지 않는다.** Python core의 MEC·MCP·Unity·실물 ESP32 직접 연동은 아직 구현하지 않았다. 실물 장치 코드는 별도 `hardware/`에 있다.

## 구성

| 파트 | 폴더 | 설명 |
|---|---|---|
| Core | [`src/anchor/`](src/anchor/) | oneM2M API 기반 수집·복구·양방향 명령 처리 |
| 하드웨어 | [`hardware/`](hardware/) | ESP32 펌웨어, 선박 게이트웨이(Pi 5), 육상 서버(Pi 4), 대시보드 |

아래 실행 안내는 Python core 기준이다. 하드웨어 구성과 실행은 [하드웨어 README](hardware/README.md)를 따른다.

## Pi 두 대에서는 명령어로 역할 선택

코드를 두 벌로 나누지 않는다. 같은 소스를 두 Pi에 설치하고 각자 필요한 프로세스만 실행한다.

```bash
# 선박 Pi 5 8GB
anchor --role ship up
anchor --role ship bootstrap

# 육상 Pi 5 16GB
anchor --role shore up
anchor --role shore bootstrap
```

선박은 수집·복구·로컬 제어, 육상은 수신 CSE·명령 보관을 담당한다. 설정·데이터·자격 증명은 `.runtime-ship`과 `.runtime-shore`로 분리한다. 분리 배포에는 SSH 키 인증과 육상 공개 인증서 등록이 필요하다. 선박에서 `anchor --role ship trust-shore <인증서 경로>`로 인증서를 등록하고 `anchor --role ship connect <user@host>`로 터널을 연다. ACME 원본은 두 역할 모두 동일하다. `--role` 생략 시 아래의 기존 Mac 통합 개발 모드다.

## 설치와 실행

필요 환경: Python 3.11 이상, Git, OpenSSL, PostgreSQL 실행 파일(`postgres`, `initdb`, `pg_isready`). 실제 개발 검증 환경은 macOS ARM64 / Python 3.14.5 / PostgreSQL 14.20이다. PostgreSQL은 ACME의 기본 저장 드라이버로만 사용한다.

ACME와 이 디렉터리를 같은 상위 폴더에 둔다. 검증한 ACME 커밋은 `283bd7d696090e983c07c149511102caab99642e`다. 실행 도구는 다른 커밋을 자동으로 checkout하지 않는다.

```bash
cd /Users/parksiwoo/hackathon
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ./ACME-oneM2M-CSE -e './anchor[test,dev-broker]'
cd anchor
anchor up
anchor bootstrap
anchor scenario coastal
anchor emit --count 10 --binding mqtt
anchor status
```

PostgreSQL이 PATH에 없으면 설치된 버전의 bin 디렉터리를 추가한다. macOS Homebrew에서는 `brew --prefix postgresql@14` 아래의 `bin`, Debian 계열에서는 `/usr/lib/postgresql/<설치버전>/bin`에 있다. 기존 PostgreSQL 서비스는 변경하지 않으며, `.runtime/postgres`에 별도 개발 클러스터를 생성한다. root 계정으로 실행하지 않는다.

기본 포트는 shore 8700, edge 8701, ship 8702, core API 8790, MQTT 18883, PostgreSQL 15432다. 모두 loopback 개발 환경이다. 시스템 Mosquitto가 정상 동작하면 사용하고, 그렇지 않으면 개발용 amqtt 프로세스를 실행한다.

```bash
anchor scenario shadow
anchor emit --count 7 --sensor sensor-02
anchor command safetyGroup on --ttl 120
anchor status
anchor scenario offshore
anchor status
anchor restart service
anchor down
```

`status`의 `metrics.pending`과 `complete`는 현재 원본과 마지막 육상 조회·전송 결과를 대조한 지표다. 보관 용량 초과 시 실제 육상 상태와 수신 캐시가 달라져 완료 지표가 부정확할 수 있다. 전원 장애·무제한 보관·대회 전체 요구사항 충족을 뜻하지 않는다. 시나리오 전환은 안정화·복구에 수 초가 걸린다.

로컬 단절 연동은 `anchor scenario shadow` 후 `anchor emit --count 1 --temperature 75 --priority critical`로 확인한다. 60°C 이상인 새 측정을 받으면 60초 유효기간의 safetyGroup ON 명령을 만든다. 팬·경보 드라이버는 시뮬레이터이며, 실제 안전 제어용으로 검증된 로직은 아니다.

## 원본 유지와 API 경계

`anchor up/down/restart`는 개인 개발 환경을 띄우는 도구다. ACME 설정 파일과 프로세스를 준비하지만 원본 코드는 수정하지 않는다. `engine`, `resources`, `client`, `service`의 동작은 공개 HTTPS/MQTTS API만 사용한다. 주최 측 CSE에서는 launcher를 사용하지 않고 제공된 endpoint·CA·권한에 맞게 클라이언트 구성을 조정해야 한다. 아직 주소만 바꾸면 모든 대회 환경에서 작동한다고 검증한 상태는 아니다.

선박 CSE는 로컬 수집을 계속 담당한다. 연안 모드의 앱 요청은 edge → shore의 고정 Mcc 등록을 이용하며, 공해 모드는 앱이 shore를 직접 호출한다. **CSE registrar 핸드오버는 구현하지 않았다.** 내부 플러그인 방식의 이전 실험은 폐기했다.

검증한 의존성 버전은 `constraints-tested.txt`에 기록했다. Pi 설치는 해당 OS/Python에서 의존성을 다시 해석하며, Mac 환경의 버전 목록을 ARM64 Linux 검증 결과로 간주하지 않는다.

## 디렉터리

| 경로 | 역할 |
|---|---|
| `src/anchor/client.py` | oneM2M HTTP·MQTT 클라이언트, discovery, 중복 검증 |
| `src/anchor/resources.py` | 리소스·ACP·구독·group·PCH bootstrap |
| `src/anchor/engine.py` | 전송 경로 선택, 복구·checkpoint·명령·로컬 연동 |
| `src/anchor/service.py` | core HTTPS API와 테스트 링크 입력 |
| `src/anchor/stack.py` | 로컬 ACME·broker·DB 프로세스 실행 |
| `src/anchor/deployment.py` | 역할 정보, 육상 인증서 등록, SSH 연결 |
| `scripts/` | Pi 설치와 비밀정보를 제외한 소스 묶기 |
| `tests/` | 모델 검증 및 실제 ACME 통합 테스트 |

## 검증

```bash
python -m pytest -q
ANCHOR_INTEGRATION=1 python -m pytest tests/test_core_integration.py -v --tb=short
git -C ../ACME-oneM2M-CSE status --short
```

통합 테스트는 로컬 실행 환경에 데이터를 추가하고 시나리오·서비스·CSE를 재시작한다. 공유 대회 서버를 대상으로 실행하지 않는다. `.runtime`에는 DB, 로그, 개발 인증서·비밀번호가 있으므로 Git에 올리지 않는다. `down`은 데이터를 삭제하지 않는다.

개발 TLS·ACP는 외부망 배포 인증을 대신하지 않는다. 개발용 인증서·권한 설정을 운영 보안 검증 결과로 간주하지 않는다. oneM2M 서비스 구현은 [ACME](https://github.com/ankraft/ACME-oneM2M-CSE)를 사용한다.
