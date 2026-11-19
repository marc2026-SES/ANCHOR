#!/usr/bin/env python3
"""ANCHOR FL 러너: fed_iforest의 연합 학습을 oneM2M(fl-cse) 위에서 돌린다.

FL_PLAN.md의 축소 실행본 — 전용 fl-cse(ACME + anchor-fl.fcp)에 대해
  코디네이터 1개 + 참가자 N개가 전부 HTTP(Mca)로만 통신한다.
  모델 배포/업데이트 수집/보정/결과가 모두 oneM2M 리소스로 남는다:

  cse-fl/flCoordinator/
    task-1            anchor:flTsk   과제 정의, fst가 "r<k>:training|calibrating" / "done" 으로 진행
    prt-<pid>         anchor:flPtc   참가자 상태 (fst, hb, nsm)
    rnd-<k>           anchor:flRnd   라운드 기록 (sel, rcv, mvr)
    updates (CNT)     CIN rn=r<k>-<pid>-<SUB>  트리 본문(base64 joblib) — 원본 데이터는 안 올라감
    calib   (CNT)     CIN rn=c<k>-<pid>        전역 모델의 로컬 점수 분포 (med, span, n)
    models  (CNT)     CIN rn=v<k>              병합된 전역 포레스트 본문
    mdl-v<k>          anchor:flMdl   버전 메타 (기여 참가자, 트리 수)

실행 (같은 순서 아니어도 됨, 서로 폴링):
  python3 fl_run.py coordinator --clients 3 --rounds 3
  python3 fl_run.py client --name ship-001     --profile ferry   --seed 11
  python3 fl_run.py client --name ferry-hakata --profile fishing --seed 12
  ...
환경: FL_CSE (기본 http://localhost:8080/cse-fl)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import time
import uuid

import joblib
import numpy as np
import requests

import fed_iforest as F
from features import SUBSYSTEMS, load_csv

CSE = os.environ.get('FL_CSE', 'http://localhost:8080/cse-fl').rstrip('/')
GEO = os.environ.get('GEO_URL', 'http://localhost:30793/api/geodata')
TAG = '1'  # --tag: 같은 CSE에서 여러 번 돌릴 때 리소스 이름 충돌 방지
ORIGIN = os.environ.get('FL_ORIGIN', 'CAnchorFL')
AE = 'flCoordinator'


def hdr(ty=None):
    h = {'X-M2M-Origin': ORIGIN, 'X-M2M-RI': uuid.uuid4().hex[:10], 'X-M2M-RVI': '4',
         'Accept': 'application/json'}
    h['Content-Type'] = f'application/json{";ty=" + str(ty) if ty else ""}'
    return h


def create(path, ty, body, ok_dup=False):
    r = requests.post(f'{CSE}/{path}' if path else CSE, headers=hdr(ty), json=body, timeout=15)
    if ok_dup and r.status_code in (403, 409) and 'already' in r.text.lower():
        return None
    assert r.status_code in (200, 201), f'CREATE {path} ty={ty}: {r.status_code} {r.text[:200]}'
    return r.json()


def update(path, body):
    r = requests.put(f'{CSE}/{path}', headers=hdr(), json=body, timeout=15)
    assert r.status_code == 200, f'UPDATE {path}: {r.status_code} {r.text[:200]}'


def get(path):
    r = requests.get(f'{CSE}/{path}', headers=hdr(), timeout=15)
    return r.json() if r.status_code == 200 else None


def cin(parent_cnt, rn, value):
    create(f'{AE}/{parent_cnt}', 4, {'m2m:cin': {'rn': rn, 'con': json.dumps(value)}}, ok_dup=True)


def cins(parent_cnt):
    v = get(f'{AE}/{parent_cnt}?rcn=4') or {}
    out = {}
    for c in (v.get('m2m:cnt') or {}).get('m2m:cin') or []:
        out[c['rn']] = json.loads(c['con'])
    return out


def pack(obj) -> str:
    buf = io.BytesIO()
    joblib.dump(obj, buf, compress=3)
    return base64.b64encode(buf.getvalue()).decode()


def unpack(s: str):
    return joblib.load(io.BytesIO(base64.b64decode(s)))


# ── 참가자 ───────────────────────────────────────────────────────────────────
def client(name: str, profile: str, seed: int, hours: float):
    d = F._gen_client(name, seed, hours)
    train = F._scaled(load_csv(d / 'train.csv'), profile)
    ups = {sub: F.local_train(train, sub, seed) for sub in SUBSYSTEMS}
    n = ups[SUBSYSTEMS[0]]['n']
    create(AE, 28, {'anchor:flPtc': {'rn': f'prt-{name}', 'cnd': 'edu.anchor.fl.participant',
                                     'pid': name, 'prf': profile, 'nsm': n, 'fst': 'idle',
                                     'hb': time.strftime('%H:%M:%S')}}, ok_dup=True)
    print(f'[{name}] 등록: {n} windows (프로필 {profile}) — 데이터는 로컬에만 있음')
    done_upload = done_calib = 0
    while True:
        update(f'{AE}/prt-{name}', {'anchor:flPtc': {'hb': time.strftime('%H:%M:%S')}})
        task = (get(f'{AE}/task-{TAG}') or {}).get('anchor:flTsk') or {}
        fst = task.get('fst', '')
        if fst == 'done':
            print(f'[{name}] 과제 완료, 종료')
            return
        if ':' not in fst:
            time.sleep(1)
            continue
        rk, phase = fst.split(':')
        k = int(rk[1:])
        sel = json.loads((get(f'{AE}/rnd-{TAG}-{k}') or {}).get('anchor:flRnd', {}).get('sel') or '[]')
        if name not in sel:
            time.sleep(1)
            continue
        if phase == 'training' and done_upload < k:
            t0 = time.time()
            for sub in SUBSYSTEMS:
                u = ups[sub]
                cin('updates', f'r{TAG}.{k}-{name}-{sub}',
                    {'pid': name, 'rnd': k, 'sub': sub, 'n': u['n'],
                     'ntr': len(u['model'].estimators_),
                     'body': pack({kk: u[kk] for kk in ('model', 'n', 'cols', 'feat_mean', 'feat_std')})})
            update(f'{AE}/prt-{name}', {'anchor:flPtc': {'fst': 'uploaded'}})
            done_upload = k
            print(f'[{name}] round {k}: 로컬 학습 {time.time()-t0:.1f}s, 트리만 업로드 (3 서브시스템)')
        elif phase == 'calibrating' and done_calib < k:
            g = unpack((cins('models').get(f'v{TAG}.{k}') or {})['body'])
            rep = {}
            for sub in SUBSYSTEMS:
                raw = -g[sub].score_samples(ups[sub]['X_val'])
                med = float(np.median(raw))
                rep[sub] = {'med': med, 'span': max(float(np.quantile(raw, 0.995)) - med, 1e-9)}
            cin('calib', f'c{TAG}.{k}-{name}', {'pid': name, 'rnd': k, 'n': n, 'dist': rep})
            done_calib = k
            print(f'[{name}] round {k}: 전역 모델 보정 분포 업로드')
        time.sleep(1)


# ── 코디네이터 ───────────────────────────────────────────────────────────────
BAD_IMPACT = {'core', 'flooded', 'down', 'severe'}  # outer/degraded는 선택 유지 (사유만 기록)


def geo_view():
    """지도 서버의 재난/접속 상태. 못 읽으면 None — 선택은 전원으로 폴백한다."""
    try:
        d = requests.get(GEO, timeout=4).json()
        def level(imp):  # 지도 서버는 impact를 {'level': 'core'|'outer', 'label': ...} 딕셔너리로 준다
            return (imp.get('level') or imp.get('label')) if isinstance(imp, dict) else imp
        poa_imp = {p.get('assetName'): level(p.get('impact')) for p in d.get('poas', [])}
        ue_poa = {u.get('assetName'): (u.get('poaInRange') or [None])[0] for u in d.get('ues', [])}
        return poa_imp, ue_poa
    except Exception:
        return None


def select(pids: list[str], minc: int):
    """동적 참가자 선택: 재난 등급(core/flooded/down) 기지국에 붙은 배는 제외.
    쿼럼 미달이면 제외자를 복귀시키고 사유를 남긴다."""
    g = geo_view()
    sel, why = [], {}
    for pid in pids:
        if g is not None:
            poa = g[1].get(pid)
            imp = g[0].get(poa) if poa else None
            if imp and str(imp).lower() in BAD_IMPACT:
                why[pid] = f'excluded: poa {poa} impact={imp}'
                continue
            if imp:
                why[pid] = f'selected (poa {poa} {imp})'
    # 여기 왔으면 선택
        sel.append(pid)
    if len(sel) < minc:
        for pid in pids:
            if pid not in sel:
                sel.append(pid)
                why[pid] = why.get(pid, '') + ' -> restored for quorum'
    return sorted(sel), why, g is not None


def coordinator(n_clients: int, rounds: int, deadline: int):
    create('', 2, {'m2m:ae': {'rn': AE, 'api': 'NanchorFL', 'rr': True, 'srv': ['4']}}, ok_dup=True)
    for cnt in ('updates', 'calib', 'models'):
        create(AE, 3, {'m2m:cnt': {'rn': cnt, 'mni': 1000}}, ok_dup=True)
    create(AE, 28, {'anchor:flTsk': {'rn': f'task-{TAG}', 'cnd': 'edu.anchor.fl.task', 'tid': f'task-{TAG}',
                                     'mdl': 'iforest', 'rnds': rounds, 'minc': max(2, n_clients - 1),
                                     'maxc': n_clients, 'ddl': deadline, 'tpc': F.TREES_PER_CLIENT,
                                     'msp': F.FL_MAX_SAMPLES, 'fst': 'open'}}, ok_dup=True)
    print(f'[agg] task-{TAG} 생성 ({rounds} rounds, quorum {max(2, n_clients-1)}), 참가자 대기')
    while True:  # 참가자 등록 대기
        root = get(f'{AE}?rcn=4') or {}
        prts = [v for v in (root.get('m2m:ae') or {}).get('anchor:flPtc', [])]
        if len(prts) >= n_clients:
            break
        time.sleep(1)
    minc = max(2, n_clients - 1)
    for k in range(1, rounds + 1):
        root = get(f'{AE}?rcn=4') or {}
        prts = [v for v in (root.get('m2m:ae') or {}).get('anchor:flPtc', [])]
        sel, why, geo_ok = select([p['pid'] for p in prts], minc)
        if not geo_ok:
            print('[agg] 지도 서버 접근 불가 — 전원 선택으로 폴백')
        for pid, reason in why.items():
            print(f'[agg] round {k}: {pid}: {reason}')
        create(AE, 28, {'anchor:flRnd': {'rn': f'rnd-{TAG}-{k}', 'cnd': 'edu.anchor.fl.round',
                                         'rnd': k, 'fst': 'training', 'sel': json.dumps(sel),
                                         'why': json.dumps(why)}}, ok_dup=True)
        update(f'{AE}/task-{TAG}', {'anchor:flTsk': {'fst': f'r{k}:training'}})
        print(f'[agg] round {k}: 선택 {sel} — 학습 지시 게시')
        t0 = time.time()
        while True:  # 업데이트 수집 (quorum + 마감)
            got = cins('updates')
            have = {p for p in sel if all(f'r{TAG}.{k}-{p}-{s}' in got for s in SUBSYSTEMS)}
            if len(have) >= (len(sel) if time.time() - t0 < deadline else minc):
                break
            time.sleep(1)
        gl, meta = {}, []
        for sub in SUBSYSTEMS:
            ups = [unpack(got[f'r{TAG}.{k}-{p}-{sub}']['body']) for p in have]
            gl[sub] = F.merge(ups, F.GLOBAL_TREES)
        meta = sorted(have)
        cin('models', f'v{TAG}.{k}', {'round': k, 'body': pack(gl)})
        update(f'{AE}/rnd-{TAG}-{k}', {'anchor:flRnd': {'fst': 'calibrating', 'rcv': json.dumps(meta)}})
        update(f'{AE}/task-{TAG}', {'anchor:flTsk': {'fst': f'r{k}:calibrating'}})
        print(f'[agg] round {k}: {len(have)}명 트리 병합 → 전역 v{k} 게시, 보정 대기')
        t0 = time.time()
        while True:
            cal = {rn: v for rn, v in cins('calib').items() if v.get('rnd') == k}
            if len(cal) >= (len(have) if time.time() - t0 < deadline else minc):
                break
            time.sleep(1)
        norms = {}
        w = np.array([v['n'] for v in cal.values()], dtype=float)
        w /= w.sum()
        for sub in SUBSYSTEMS:
            meds = [v['dist'][sub]['med'] for v in cal.values()]
            spans = [v['dist'][sub]['span'] for v in cal.values()]
            norms[sub] = {'norm_median': float(np.dot(w, meds)), 'norm_span': float(np.dot(w, spans))}
        cin('models', f'v{TAG}.{k}-norm', {'round': k, 'norms': norms})
        ntr = sum(len(gl[s].estimators_) for s in SUBSYSTEMS)
        create(AE, 28, {'anchor:flMdl': {'rn': f'mdl-{TAG}-v{k}', 'cnd': 'edu.anchor.fl.model',
                                         'ver': f'v{TAG}.{k}', 'rnd': k, 'ntr': ntr,
                                         'pts': json.dumps(meta)}}, ok_dup=True)
        update(f'{AE}/rnd-{TAG}-{k}', {'anchor:flRnd': {'fst': 'done', 'mvr': f'v{TAG}.{k}'}})
        print(f'[agg] round {k}: 보정 완료 (가중 평균) → flModel mdl-v{k} 기록')
    update(f'{AE}/task-{TAG}', {'anchor:flTsk': {'fst': 'done'}})
    # 전역 번들 저장 (analytics_ae 호환 형식)
    out = F.ROOT / 'models' / 'fl'
    out.mkdir(parents=True, exist_ok=True)
    for sub in SUBSYSTEMS:
        b = F.to_bundle(sub, gl[sub], [], np.zeros(1), np.ones(1), **norms[sub])
        joblib.dump(b, out / f'iforest_{sub}.joblib')
    print(f'[agg] 완료: 전역 모델 {out}/iforest_*.joblib 저장, task fst=done')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('role', choices=['coordinator', 'client'])
    ap.add_argument('--name', default='client-1')
    ap.add_argument('--profile', default='ferry', choices=list(F.PROFILE_SCALE))
    ap.add_argument('--seed', type=int, default=11)
    ap.add_argument('--hours', type=float, default=2.0)
    ap.add_argument('--clients', type=int, default=3)
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--deadline', type=int, default=120)
    ap.add_argument('--tag', default='1')
    a = ap.parse_args()
    TAG = a.tag
    if a.role == 'coordinator':
        coordinator(a.clients, a.rounds, a.deadline)
    else:
        client(a.name, a.profile, a.seed, a.hours)
