# ANCHOR FL 실행 (MEC 서버, anchor@100.96.155.4)

전용 fl-cse(ACME + anchor-fl.fcp 커스텀 타입 5종) 위에서
코디네이터 1 + 참가자 3(ship-001 포함)이 Federated Isolation Forest 3라운드를 돈다.
트리(가중치)만 oneM2M CIN으로 오가고 원본 데이터는 각 참가자 로컬에 남는다.

## 1) 설치 (1회)
    cd ~/anchor-fl
    pip3 install numpy pandas "scikit-learn==1.7.2" joblib requests acmecse

## 2) 커스텀 타입 설치 + CSE 시작 (포트 18080)
    cp anchor-fl.fcp $(python3 -c "import acmecse,pathlib;print(pathlib.Path(acmecse.__file__).parent/'init')")/
    nohup python3 -m acmecse --config acme.ini --headless > acme.log 2>&1 &
    sleep 12 && curl -s http://localhost:18080/cse-fl -H 'X-M2M-Origin: CAdmin' -H 'X-M2M-RI: 1' -H 'X-M2M-RVI: 4' | head -c 120
    # {"m2m:cb": ... 나오면 OK

## 3) 참가자 로컬 데이터 생성 (동시 실행 충돌 방지용 선생성)
    export FL_CSE=http://localhost:18080/cse-fl
    python3 -c "import fed_iforest as F; [F._gen_client(n,s,2.0) for n,s in [('ship-001',11),('ferry-hakata',12),('container-01',13)]]"

## 4) 실행 (코디네이터 1 + 참가자 3)
    nohup python3 fl_run.py coordinator --clients 3 --rounds 3 > agg.log 2>&1 &
    sleep 3
    nohup python3 fl_run.py client --name ship-001     --profile ferry     --seed 11 > c1.log 2>&1 &
    nohup python3 fl_run.py client --name ferry-hakata --profile fishing   --seed 12 > c2.log 2>&1 &
    nohup python3 fl_run.py client --name container-01 --profile container --seed 13 > c3.log 2>&1 &
    sleep 60 && tail -12 agg.log
    # "[agg] 완료: 전역 모델 ..." 나오면 성공 (약 30-60초)

## 5) 증거 (스크린샷 2장)
    # ① 라운드 로그
    tail -12 agg.log; tail -4 c1.log
    # ② oneM2M 리소스 트리 (flTask/flRound/flUpdate/flModel)
    curl -s "http://localhost:18080/cse-fl/flCoordinator?rcn=4" -H 'X-M2M-Origin: CAnchorFL' -H 'X-M2M-RI: 9' -H 'X-M2M-RVI: 4' | python3 -m json.tool | head -60

## 참고
- 검증: `python3 fed_iforest.py selftest` (sklearn 1.7.2 트리 병합 무결성)
- 로컬/FL/중앙 비교 그래프 수치: `python3 fed_iforest.py simulate --hours 4` → results/fl_simulation.json
- 포트 18080이 사용 중이면 acme.ini의 httpPort와 FL_CSE만 같이 바꾸면 됨
- ship-001은 지금 합성 데이터(ferry 프로필)로 돌며, 실측 CSV가 있으면 fl_run.py의 _gen_client 대신 연결
