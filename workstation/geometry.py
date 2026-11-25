"""Workstation millimetres from the three physically acquired negative limits; Z+ is down."""
import json
import math
from pathlib import Path

LAYOUT = json.loads(Path(__file__).with_name("layout.json").read_text(encoding="utf-8"))
POINTS = ("safe", "tip_a", "tip_col", "tip_row", "tube_a", "tube_col", "tube_row", "receiver", "waste")
TUBES = tuple(f"{row}{col}" for row in "AB" for col in range(1, 7))
TIPS = tuple(f"{row}{col:02d}" for row in "ABCDEFGH" for col in range(1, 13))


def bounds(axis, value):
    low, high = LAYOUT["limits"][axis]
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{axis.upper()} 目标 {value:g} mm 超出工位行程 [{low}, {high}]")
    return value


def grid_point(points, kind, well):
    names, cols, rows = (TIPS, 12, 8) if kind == "tip" else (TUBES, 6, 2)
    row, col = divmod(names.index(well), cols)
    first, last_col, last_row = (points[kind + s] for s in ("_a", "_col", "_row"))
    return [bounds(axis, first[i] + col * (last_col[i] - first[i]) / (cols - 1)
                   + row * (last_row[i] - first[i]) / (rows - 1))
            for i, axis in enumerate("xy")] + [first[2]]


def validate_rack(points, kind):
    keys = [kind + suffix for suffix in ("_a", "_col", "_row")]
    if any(k not in points for k in keys):
        raise ValueError("请先记录该架的三个定位点")
    cols, rows = (12, 8) if kind == "tip" else (6, 2)
    a, b, c = [points[k] for k in keys]
    u = [(b[i] - a[i]) / (cols - 1) for i in range(2)]
    v = [(c[i] - a[i]) / (rows - 1) for i in range(2)]
    pitch = LAYOUT["pitch"][kind]
    if any(abs(math.hypot(*edge) - pitch) > pitch * .08 for edge in (u, v)):
        raise ValueError(f"角点间距与 {pitch:g} mm 网格不符，请复核行列和孔号")
    if abs(sum(x * y for x, y in zip(u, v))) > .12 * pitch ** 2:
        raise ValueError("两条网格边不垂直，请复核三个角点")
    grid_point(points, kind, (TIPS if kind == "tip" else TUBES)[-1])


def validate_profile(points):
    if "receiver" not in points:
        raise ValueError("请在工位设置中记录天平接收瓶排液位")
    if not set(POINTS) <= points.keys():
        raise ValueError("工位的架位或横移高度缺失，请检查保存的工位")
    for xyz in points.values():
        for axis, value in zip("xyz", xyz):
            bounds(axis, value)
    for kind in ("tip", "tube"):
        validate_rack(points, kind)
    for key in ("tip_a", "tube_a", "receiver", "waste"):
        if points[key][2] <= points["safe"][2]:
            raise ValueError("请复核横移高度，四个操作点都应在横移面下方")
