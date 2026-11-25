"""Mass in g, density in g/mL, volume in microlitres."""
import itertools
import math
from statistics import fmean

LIQUIDS = ("水", "甘油溶液", "蔗糖溶液")
BLINDS = ("U1", "U2", "U3")
VOLUMES = (30, 70, 150, 300, 500, 800)


def volume(mass, density):
    return mass * 1000.0 / density


def fit_curve(pairs):
    xs, ys = zip(*pairs)
    xm, ym = fmean(xs), fmean(ys)
    xx = sum((x - xm) ** 2 for x in xs)
    yy = sum((y - ym) ** 2 for y in ys)
    if xx <= 0 or yy <= 0:
        raise ValueError("标定数据无有效变化范围")
    a = sum((x - xm) * (y - ym) for x, y in pairs) / xx
    if a <= 0 or not math.isfinite(a):
        raise ValueError("标定线斜率无效，请检查原始记录")
    b = ym - a * xm
    error2 = sum((y - a * x - b) ** 2 for x, y in pairs)
    return dict(a=a, b=b, r2=1-error2/yy, n=len(xs), points=pairs,
                rms_set=math.sqrt(error2/len(xs)))


def summary(session, records):
    good = [r for r in records if r["status"] == "done"]
    curves, issues = {}, {}
    for name in LIQUIDS:
        rows = [r for r in good if r["phase"] == "cal" and r["sample"] == name]
        if not all(sum(r["command"] == v for r in rows) >= session["repeats"] for v in VOLUMES):
            continue
        try:
            curves[name] = fit_curve([(volume(r["net"], session["densities"][name]), r["command"]) for r in rows])
        except ValueError as exc:
            issues[name] = str(exc)
    scores, assignments = {}, []
    if len(curves) == 3:
        for blind in BLINDS:
            rows = [r for r in good if r["phase"] == "blind" and r["sample"] == blind]
            if not rows:
                continue
            scores[blind] = {}
            for name, model in curves.items():
                density = session["densities"][name]
                residuals = [1000*r["net"] - density*(r["command"]-model["b"])/model["a"] for r in rows]
                scores[blind][name] = math.sqrt(fmean(d*d for d in residuals))
        if len(scores) == 3:
            for order in itertools.permutations(LIQUIDS):
                pair = dict(zip(BLINDS, order))
                rms = math.sqrt(fmean(scores[k][v]**2 for k, v in pair.items()))
                assignments.append(dict(pair=pair, rms=rms))
            assignments.sort(key=lambda item: item["rms"])
    # One scale display division and calibration residuals provide a teaching cue.
    noise = max([1.0] + [m["rms_set"] * session["densities"][k] / m["a"] for k,m in curves.items()])
    close = bool(assignments and assignments[1]["rms"] - assignments[0]["rms"] <= noise)
    return dict(curves=curves, issues=issues, scores=scores, assignments=assignments, close=close,
                noise_mg=noise)


def correction(curve):
    estimate = 200.0*curve["a"] + curve["b"]
    command = math.floor(estimate + .5)
    if not 10 <= command <= 1000:
        raise ValueError("200 μL 对应的设定量超出移液器量程")
    return command
