"""Land/sea test for the Korea Strait (Natural Earth 10m land, clipped to land.json's bbox)."""
import json, math
from pathlib import Path

_d = json.loads((Path(__file__).resolve().parent / 'land.json').read_text())
BBOX = tuple(_d['bbox'])
RINGS = [(r, (min(p[0] for p in r), min(p[1] for p in r), max(p[0] for p in r), max(p[1] for p in r))) for r in _d['rings']]


def on_land(lon, lat):
    for ring, (x0, y0, x1, y1) in RINGS:
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            inside = False
            j = len(ring) - 1
            for i in range(len(ring)):
                xi, yi = ring[i]; xj, yj = ring[j]
                if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                    inside = not inside
                j = i
            if inside:
                return True
    return False


def _km(a, b):
    dx = (b[0] - a[0]) * 111.32 * math.cos(math.radians((a[1] + b[1]) / 2)); dy = (b[1] - a[1]) * 110.57
    return math.hypot(dx, dy)


def land_crossings(path, step_km=0.5, port_km=3.0):
    """Points where a path goes over land, sampled every step_km.
    The first/last port_km of the whole path are ignored (ships start and end at a quay)."""
    total = sum(_km(path[i], path[i + 1]) for i in range(len(path) - 1))
    hits, done = [], 0.0
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]; seg = _km(a, b); n = max(1, int(seg / step_km))
        for k in range(n + 1):
            t = k / n; d = done + seg * t
            if d < port_km or d > total - port_km:
                continue
            p = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
            if on_land(*p):
                hits.append({'segment': i, 'at': [round(p[0], 4), round(p[1], 4)]})
                break  # one hit per segment is enough
        done += seg
    return hits
