# 연안 MEC AI(Fleet AI) 데모 순서

지도: http://100.96.155.4:30793 · MEC 서버: `anchor@100.96.155.4` (`~/anchor-ai`, 로그 `fleet.log`)

## 데모 전 준비 (30분 전)

```bash
# 1) Fleet AI가 돌고 있는지 (숫자 1이 나와야 함)
ssh -i ~/.ssh/anchor_pi anchor@100.96.155.4 'pgrep -cf "^.venv/bin/python -u tools/fleet_scan.py"; tail -2 ~/anchor-ai/fleet.log'
```

```bash
# 2) 항로 모델을 최근 3시간 항적으로 다시 학습 (스캐너가 30초 안에 자동 반영)
ssh -i ~/.ssh/anchor_pi anchor@100.96.155.4 'cd ~/anchor-ai && .venv/bin/python fleet_route_model.py train --since-min 180'
```

9척이 모두 "표본 … warn … danger"로 나오면 준비 끝. "한 바퀴 미완"인 배가 있으면 그 배는 항로 이탈
판정에서 빠진다(직전에 태풍·표류 시연을 했다면 30~40분 뒤 다시 학습).

## 시연 1 — 태풍 회피 (약 5분)

1. 지도 오른쪽 **Trigger a disaster** → typhoon, 중심을 배들이 지나는 곳(예: 경도 129.3, 위도 34.9),
   지속 3~4분. 통신에 영향을 주지 않으려면 affectNetwork를 끈다.
2. 볼 것
   - 경고 목록에 `배 ↔ 태풍 이름` (구역 안·30분 전 danger, 2시간 전 warn — 시나리오 시간)
   - 해당 배의 경로가 태풍 진로 옆으로 바뀜 (주변이 육지뿐이면 경고만 뜬다)
3. 태풍이 끝나면 배가 가던 방향으로 원래 항로에 합류해 끝점까지 간 뒤 원래 경로로 돌아온다(수 분).

## 시연 2 — 표류선 충돌 회피 (약 3분 + 대기)

"통신이 끊겨 항로 위에서 표류하는 배"를 만들고, 그 항로를 지나는 배를 AI가 비키게 한다.

```bash
cd ~/Desktop/anchor-ai && .venv/bin/python tools/demo_rogue.py start
```

- 조건이 맞는 순간(긴 직선 구간을 가는 배 + 먼저 닿을 수 있는 배)을 기다린다. 바로 될 때도 있고 몇 분
  걸릴 때도 있으니 **발표 순서보다 몇 분 먼저 실행**한다. `start --dry-run`은 아무것도 바꾸지 않고
  지금 가능한지만 알려준다.
- "표류 시작"이 뜬 뒤 약 1~2분 동안 볼 것
  1. 표류선 `항로 이탈` warn → danger
  2. 두 배 충돌 warn → danger (최근접 0.0nm)
  3. **대상선 경로가 옆으로 꺾임** (로그: `AVOID <대상선> 경로 변경`)
  4. 지나간 뒤 대상선이 원래 항로로 복귀
- 끝나면 반드시:

```bash
cd ~/Desktop/anchor-ai && .venv/bin/python tools/demo_rogue.py restore
```

2026-11-19 실측(2회): 예측 최근접 0.0nm → 경로 변경 → 실제 최소 거리 1.7nm → 원래 경로 복귀 완료.

## 설명할 때

- 충돌 경고·재해 경고·경로 변경은 **계산**, 항로 이탈 탐지는 **학습**(배마다 과거 항적을 기억).
- 평소에도 danger는 자주 뜨지만 경로는 안 바꾼다. 직선으로 계속 간다는 가정이 대부분 빗나가기
  때문(45분 동안 danger 7번, 실제 0.25nm 이내 접근 0번). **상대가 학습된 항로를 벗어났을 때만** 바꾼다.
- 해일은 경고만 한다. 지도는 가장 최근 경고 한 건만 보여준다.

## 문제가 생기면

```bash
ssh -i ~/.ssh/anchor_pi anchor@100.96.155.4 'bash ~/anchor-ai/tools/restart_fleet.sh'   # Fleet AI 재시작
ssh -i ~/.ssh/anchor_pi anchor@100.96.155.4 'grep -a -E "AVOID|복귀|항로 이탈|warn\]" ~/anchor-ai/fleet.log | tail -20'
```
