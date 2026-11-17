# 육상 Pi 4

배포 설정과 서비스 정의는 `deploy/shore/` 및 `deploy/systemd/`로 통합했다.
설치·인증서·DB 이관·실행 명령은 [루트 README](../../README.md#역할별-배포와-실행)를 따른다.

이 폴더에는 실제 하드웨어 화면 소스를 유지한다. 기존 CSE DB와 `check_db.py`는 자동 실행하거나 초기화하지 않는다.
새 실행 진입점은 `./deploy/shore/run.sh cse`, `core`, `dashboard`다.
