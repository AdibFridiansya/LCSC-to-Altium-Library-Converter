"""Parsers for EasyEDA Standard schematic symbols and PCB footprints."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

# 1 EasyEDA canvas unit = 10 mil = 0.254 mm
EASYEDA_TO_MILS = 10.0

_NUM_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_SVG_TOKEN_RE = re.compile(
    r"[MmLlHhVvCcQqAaZz]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
)


def _to_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def _to_int(val: Any, default: int = 0) -> int:
    try:
        if val is None or val == "":
            return default
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _hex_to_win32_color(color_str: str, default: int = 0x000080) -> int:
    """Convert '#RRGGBB' to Altium Win32 0x00BBGGRR integer."""
    if not color_str or color_str.lower() in ("none", "transparent"):
        return default
    s = color_str.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return default
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (b << 16) | (g << 8) | r
    except ValueError:
        return default


def _e2a_pt(x: float, y: float, ox: float, oy: float) -> tuple[float, float]:
    """Convert EasyEDA (Y-down, 10-mil units) to Altium (Y-up, mils)."""
    return ((x - ox) * EASYEDA_TO_MILS, -(y - oy) * EASYEDA_TO_MILS)


def _sample_cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 12,
) -> list[tuple[float, float]]:
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1.0 - t
        x = (
            mt**3 * p0[0]
            + 3 * mt**2 * t * p1[0]
            + 3 * mt * t**2 * p2[0]
            + t**3 * p3[0]
        )
        y = (
            mt**3 * p0[1]
            + 3 * mt**2 * t * p1[1]
            + 3 * mt * t**2 * p2[1]
            + t**3 * p3[1]
        )
        pts.append((x, y))
    return pts


def _sample_quad_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    steps: int = 10,
) -> list[tuple[float, float]]:
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1.0 - t
        x = mt**2 * p0[0] + 2 * mt * t * p1[0] + t**2 * p2[0]
        y = mt**2 * p0[1] + 2 * mt * t * p1[1] + t**2 * p2[1]
        pts.append((x, y))
    return pts


def svg_arc_endpoint_to_center(
    x1: float,
    y1: float,
    rx: float,
    ry: float,
    phi_deg: float,
    large_arc: int,
    sweep: int,
    x2: float,
    y2: float,
) -> tuple[float, float, float, float, float, float] | None:
    """W3C SVG 1.1 F.6.5 endpoint-to-center arc parameterization.

    Returns (cx, cy, rx, ry, theta1_deg, dtheta_deg) in the input coordinate frame.
    """
    if math.hypot(x2 - x1, y2 - y1) < 1e-9:
        return None
    rx = abs(rx)
    ry = abs(ry)
    if rx < 1e-9 or ry < 1e-9:
        return None

    phi = math.radians(phi_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)

    dx2 = (x1 - x2) / 2.0
    dy2 = (y1 - y2) / 2.0
    x1p = cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2

    lam = (x1p**2) / (rx**2) + (y1p**2) / (ry**2)
    if lam > 1.0:
        s = math.sqrt(lam)
        rx *= s
        ry *= s

    num = max(0.0, (rx**2) * (ry**2) - (rx**2) * (y1p**2) - (ry**2) * (x1p**2))
    den = (rx**2) * (y1p**2) + (ry**2) * (x1p**2)
    coef = math.sqrt(num / den) if den > 1e-12 else 0.0
    if bool(large_arc) == bool(sweep):
        coef = -coef

    cxp = coef * (rx * y1p / ry)
    cyp = coef * (-ry * x1p / rx)

    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2.0

    def _angle(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        n = math.hypot(ux, uy) * math.hypot(vx, vy)
        if n < 1e-12:
            return 0.0
        val = max(-1.0, min(1.0, dot / n))
        ang = math.acos(val)
        if ux * vy - uy * vx < 0:
            ang = -ang
        return ang

    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry

    theta1 = _angle(1.0, 0.0, ux, uy)
    dtheta = _angle(ux, uy, vx, vy)
    if not sweep and dtheta > 0:
        dtheta -= 2.0 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2.0 * math.pi

    return (cx, cy, rx, ry, math.degrees(theta1), math.degrees(dtheta))


def _sample_svg_arc(
    x1: float,
    y1: float,
    rx: float,
    ry: float,
    phi_deg: float,
    large_arc: int,
    sweep: int,
    x2: float,
    y2: float,
    steps: int = 16,
) -> list[tuple[float, float]]:
    res = svg_arc_endpoint_to_center(x1, y1, rx, ry, phi_deg, large_arc, sweep, x2, y2)
    if res is None:
        return [(x2, y2)]
    cx, cy, rx, ry, theta1_deg, dtheta_deg = res
    phi = math.radians(phi_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    pts = []
    for i in range(1, steps + 1):
        t = math.radians(theta1_deg + dtheta_deg * (i / steps))
        ex = rx * math.cos(t)
        ey = ry * math.sin(t)
        px = cx + cos_p * ex - sin_p * ey
        py = cy + sin_p * ex + cos_p * ey
        pts.append((px, py))
    return pts


def parse_svg_path_subpaths(path_str: str) -> list[tuple[list[tuple[float, float]], bool]]:
    """Parse an SVG path string into list of (points, is_closed) subpaths in raw coordinates."""
    tokens = _SVG_TOKEN_RE.findall(path_str or "")
    subpaths: list[tuple[list[tuple[float, float]], bool]] = []
    cur_pts: list[tuple[float, float]] = []
    cx, cy = 0.0, 0.0
    sx, sy = 0.0, 0.0
    cmd = ""
    i = 0
    n = len(tokens)

    def _is_cmd(tok: str) -> bool:
        return len(tok) == 1 and tok.isalpha()

    while i < n:
        if _is_cmd(tokens[i]):
            cmd = tokens[i]
            i += 1
        elif not cmd:
            i += 1
            continue

        if cmd in ("M", "m"):
            if cur_pts:
                subpaths.append((cur_pts, False))
                cur_pts = []
            if i + 1 >= n:
                break
            x, y = float(tokens[i]), float(tokens[i + 1])
            i += 2
            if cmd == "m":
                cx += x
                cy += y
            else:
                cx, cy = x, y
            sx, sy = cx, cy
            cur_pts.append((cx, cy))
            cmd = "l" if cmd == "m" else "L"

        elif cmd in ("L", "l"):
            if i + 1 >= n:
                break
            x, y = float(tokens[i]), float(tokens[i + 1])
            i += 2
            if cmd == "l":
                cx += x
                cy += y
            else:
                cx, cy = x, y
            cur_pts.append((cx, cy))

        elif cmd in ("H", "h"):
            x = float(tokens[i])
            i += 1
            cx = cx + x if cmd == "h" else x
            cur_pts.append((cx, cy))

        elif cmd in ("V", "v"):
            y = float(tokens[i])
            i += 1
            cy = cy + y if cmd == "v" else y
            cur_pts.append((cx, cy))

        elif cmd in ("C", "c"):
            if i + 5 >= n:
                break
            x1, y1, x2, y2, x, y = (float(tokens[i + k]) for k in range(6))
            i += 6
            if cmd == "c":
                x1 += cx; y1 += cy; x2 += cx; y2 += cy; x += cx; y += cy
            cur_pts.extend(_sample_cubic_bezier((cx, cy), (x1, y1), (x2, y2), (x, y)))
            cx, cy = x, y

        elif cmd in ("Q", "q"):
            if i + 3 >= n:
                break
            x1, y1, x, y = (float(tokens[i + k]) for k in range(4))
            i += 4
            if cmd == "q":
                x1 += cx; y1 += cy; x += cx; y += cy
            cur_pts.extend(_sample_quad_bezier((cx, cy), (x1, y1), (x, y)))
            cx, cy = x, y

        elif cmd in ("A", "a"):
            if i + 6 >= n:
                break
            rx, ry, rot = float(tokens[i]), float(tokens[i + 1]), float(tokens[i + 2])
            large_arc = int(float(tokens[i + 3]))
            sweep = int(float(tokens[i + 4]))
            x, y = float(tokens[i + 5]), float(tokens[i + 6])
            i += 7
            if cmd == "a":
                x += cx
                y += cy
            cur_pts.extend(_sample_svg_arc(cx, cy, rx, ry, rot, large_arc, sweep, x, y))
            cx, cy = x, y

        elif cmd in ("Z", "z"):
            if cur_pts:
                subpaths.append((cur_pts, True))
                cur_pts = []
            cx, cy = sx, sy
            cmd = ""
        else:
            i += 1

    if cur_pts:
        subpaths.append((cur_pts, False))
    return subpaths


# ============================================================================
# Schematic Symbol Models & Parser
# ============================================================================

@dataclass
class SchPinModel:
    number: str
    name: str
    body_x_mils: float
    body_y_mils: float
    length_mils: float
    orientation_deg: int  # 0=right, 90=up, 180=left, 270=down (from body toward hot end)
    electrical_type: int  # EasyEDA: 0=unspec, 1=in, 2=out, 3=io, 4=power
    hidden: bool = False
    name_visible: bool = True
    number_visible: bool = True
    has_dot: bool = False
    has_clock: bool = False
    part_id: int = 1


@dataclass
class SchRectModel:
    x1_mils: float
    y1_mils: float
    x2_mils: float
    y2_mils: float
    stroke_color: int
    fill_color: int
    is_solid: bool
    rx_mils: float = 0.0
    ry_mils: float = 0.0
    part_id: int = 1


@dataclass
class SchPolyModel:
    points_mils: list[tuple[float, float]]
    stroke_color: int
    fill_color: int
    is_closed: bool
    is_solid: bool
    part_id: int = 1


@dataclass
class SchEllipseModel:
    cx_mils: float
    cy_mils: float
    rx_mils: float
    ry_mils: float
    stroke_color: int
    fill_color: int
    is_solid: bool
    part_id: int = 1


@dataclass
class SchArcModel:
    cx_mils: float
    cy_mils: float
    radius_mils: float
    start_angle_deg: float
    end_angle_deg: float
    stroke_color: int
    part_id: int = 1


@dataclass
class SchTextModel:
    text: str
    x_mils: float
    y_mils: float
    rotation_deg: int
    color: int
    part_id: int = 1


@dataclass
class ParsedSymbol:
    part_count: int = 1
    pins: list[SchPinModel] = field(default_factory=list)
    rects: list[SchRectModel] = field(default_factory=list)
    polys: list[SchPolyModel] = field(default_factory=list)
    ellipses: list[SchEllipseModel] = field(default_factory=list)
    arcs: list[SchArcModel] = field(default_factory=list)
    texts: list[SchTextModel] = field(default_factory=list)


def _parse_pin_shape(shape_str: str, ox: float, oy: float, part_id: int) -> SchPinModel | None:
    sections = shape_str.split("^^")
    if not sections:
        return None
    s0 = sections[0].split("~")
    if len(s0) < 7:
        return None

    hidden = s0[1].strip().lower() != "show"
    electrical = _to_int(s0[2], 0)
    pin_num = s0[3].strip() or "1"
    hot_x = _to_float(s0[4], ox)
    hot_y = _to_float(s0[5], oy)
    rot_easy = _to_int(s0[6], 0) % 360

    # Parse pin line from section 2 (e.g., "M 525 230 h -10~#880000")
    body_x, body_y = hot_x, hot_y
    if len(sections) > 2:
        path_raw = sections[2].split("~")[0]
        subpaths = parse_svg_path_subpaths(path_raw)
        if subpaths and len(subpaths[0][0]) >= 2:
            p_start = subpaths[0][0][0]
            p_end = subpaths[0][0][-1]
            # Hot end is (hot_x, hot_y); the opposite end of the path is the body end
            d_start = math.hypot(p_start[0] - hot_x, p_start[1] - hot_y)
            d_end = math.hypot(p_end[0] - hot_x, p_end[1] - hot_y)
            if d_start <= d_end:
                body_x, body_y = p_end
            else:
                body_x, body_y = p_start
        else:
            # Fallback using rotation (default length 10 easyeda units = 100 mil)
            rad = math.radians(rot_easy)
            body_x = hot_x - 10.0 * math.cos(rad)
            body_y = hot_y + 10.0 * math.sin(rad)

    ax_hot, ay_hot = _e2a_pt(hot_x, hot_y, ox, oy)
    ax_body, ay_body = _e2a_pt(body_x, body_y, ox, oy)

    dx = ax_hot - ax_body
    dy = ay_hot - ay_body
    length_mils = math.hypot(dx, dy)
    if length_mils < 1.0:
        length_mils = 100.0
        # Derive direction from EasyEDA pin rotation
        # In EasyEDA: 0 -> hot on right, 180 -> hot on left, 90 -> hot on top, 270 -> hot on bottom
        orientation_deg = {0: 0, 90: 90, 180: 180, 270: 270}.get(rot_easy, 0)
        rad = math.radians(orientation_deg)
        ax_body = ax_hot - length_mils * math.cos(rad)
        ay_body = ay_hot - length_mils * math.sin(rad)
    else:
        if abs(dx) >= abs(dy):
            orientation_deg = 0 if dx > 0 else 180
        else:
            orientation_deg = 90 if dy > 0 else 270

    pin_name = pin_num
    name_visible = True
    if len(sections) > 3:
        s3 = sections[3].split("~")
        if len(s3) >= 5:
            name_visible = s3[0].strip() != "0"
            if s3[4].strip():
                pin_name = s3[4].strip()

    number_visible = True
    if len(sections) > 4:
        s4 = sections[4].split("~")
        if len(s4) >= 1:
            number_visible = s4[0].strip() != "0"
        if len(s4) >= 5 and s4[4].strip():
            pin_num = s4[4].strip()

    has_dot = False
    if len(sections) > 5:
        s5 = sections[5].split("~")
        if s5 and s5[0].strip() == "1":
            has_dot = True

    has_clock = False
    if len(sections) > 6:
        s6 = sections[6].split("~")
        if s6 and s6[0].strip() == "1":
            has_clock = True

    return SchPinModel(
        number=pin_num,
        name=pin_name,
        body_x_mils=ax_body,
        body_y_mils=ay_body,
        length_mils=length_mils,
        orientation_deg=orientation_deg,
        electrical_type=electrical,
        hidden=hidden,
        name_visible=name_visible,
        number_visible=number_visible,
        has_dot=has_dot,
        has_clock=has_clock,
        part_id=part_id,
    )


def _parse_symbol_data_str(
    data_str: dict[str, Any], out: ParsedSymbol, part_id: int
) -> None:
    head = data_str.get("head") or {}
    ox = _to_float(head.get("x"), 0.0)
    oy = _to_float(head.get("y"), 0.0)
    shapes = data_str.get("shape") or []

    default_stroke = 0x000080  # Classic Altium dark red/maroon
    default_fill = 0xB0FFFF    # Classic Altium pale yellow

    for raw in shapes:
        if not isinstance(raw, str) or "~" not in raw:
            continue
        kind = raw.split("~", 1)[0]
        parts = raw.split("~")

        if kind == "P":
            pin = _parse_pin_shape(raw, ox, oy, part_id)
            if pin:
                out.pins.append(pin)

        elif kind == "R" and len(parts) >= 7:
            # R~x~y~rx~ry~w~h~stroke~stroke_w~style~fill~gId~locked
            x = _to_float(parts[1])
            y = _to_float(parts[2])
            rx = _to_float(parts[3]) * EASYEDA_TO_MILS
            ry = _to_float(parts[4]) * EASYEDA_TO_MILS
            w = _to_float(parts[5])
            h = _to_float(parts[6])
            stroke_hex = parts[7] if len(parts) > 7 else ""
            fill_hex = parts[10] if len(parts) > 10 else "none"
            is_solid = fill_hex.strip().lower() not in ("", "none", "transparent")
            ax1, ay1 = _e2a_pt(x, y, ox, oy)
            ax2, ay2 = _e2a_pt(x + w, y + h, ox, oy)
            out.rects.append(
                SchRectModel(
                    x1_mils=min(ax1, ax2),
                    y1_mils=min(ay1, ay2),
                    x2_mils=max(ax1, ax2),
                    y2_mils=max(ay1, ay2),
                    stroke_color=_hex_to_win32_color(stroke_hex, default_stroke),
                    fill_color=_hex_to_win32_color(fill_hex, default_fill) if is_solid else default_fill,
                    is_solid=is_solid,
                    rx_mils=rx,
                    ry_mils=ry,
                    part_id=part_id,
                )
            )

        elif kind in ("PL", "PG") and len(parts) >= 2:
            # PL~x1 y1 x2 y2 ...~stroke~stroke_w~style~fill~gId~locked
            nums = [float(m) for m in _NUM_RE.findall(parts[1])]
            pts = [
                _e2a_pt(nums[i], nums[i + 1], ox, oy)
                for i in range(0, len(nums) - 1, 2)
            ]
            if len(pts) >= 2:
                stroke_hex = parts[2] if len(parts) > 2 else ""
                fill_hex = parts[5] if len(parts) > 5 else "none"
                is_solid = fill_hex.strip().lower() not in ("", "none", "transparent")
                is_closed = kind == "PG"
                out.polys.append(
                    SchPolyModel(
                        points_mils=pts,
                        stroke_color=_hex_to_win32_color(stroke_hex, default_stroke),
                        fill_color=_hex_to_win32_color(fill_hex, default_fill),
                        is_closed=is_closed,
                        is_solid=is_solid,
                        part_id=part_id,
                    )
                )

        elif kind == "E" and len(parts) >= 5:
            # E~cx~cy~rx~ry~stroke~stroke_w~style~fill~gId~locked
            cx, cy = _e2a_pt(_to_float(parts[1]), _to_float(parts[2]), ox, oy)
            rx = abs(_to_float(parts[3])) * EASYEDA_TO_MILS
            ry = abs(_to_float(parts[4])) * EASYEDA_TO_MILS
            stroke_hex = parts[5] if len(parts) > 5 else ""
            fill_hex = parts[8] if len(parts) > 8 else "none"
            is_solid = fill_hex.strip().lower() not in ("", "none", "transparent")
            if rx > 0.1 and ry > 0.1:
                out.ellipses.append(
                    SchEllipseModel(
                        cx_mils=cx,
                        cy_mils=cy,
                        rx_mils=rx,
                        ry_mils=ry,
                        stroke_color=_hex_to_win32_color(stroke_hex, default_stroke),
                        fill_color=_hex_to_win32_color(fill_hex, default_fill),
                        is_solid=is_solid,
                        part_id=part_id,
                    )
                )

        elif kind == "C" and len(parts) >= 4:
            # C~cx~cy~r~stroke~stroke_w~style~fill~gId~locked
            cx, cy = _e2a_pt(_to_float(parts[1]), _to_float(parts[2]), ox, oy)
            r = abs(_to_float(parts[3])) * EASYEDA_TO_MILS
            stroke_hex = parts[4] if len(parts) > 4 else ""
            fill_hex = parts[7] if len(parts) > 7 else "none"
            is_solid = fill_hex.strip().lower() not in ("", "none", "transparent")
            if r > 0.1:
                out.ellipses.append(
                    SchEllipseModel(
                        cx_mils=cx,
                        cy_mils=cy,
                        rx_mils=r,
                        ry_mils=r,
                        stroke_color=_hex_to_win32_color(stroke_hex, default_stroke),
                        fill_color=_hex_to_win32_color(fill_hex, default_fill),
                        is_solid=is_solid,
                        part_id=part_id,
                    )
                )

        elif kind in ("A", "PT") and len(parts) >= 2:
            # A~svg_path~...~stroke~stroke_w~style~fill~gId
            # PT~svg_path~stroke~stroke_w~style~fill~gId
            path_str = parts[1]
            if kind == "A":
                stroke_hex = parts[3] if len(parts) > 3 else ""
                fill_hex = parts[6] if len(parts) > 6 else "none"
            else:
                stroke_hex = parts[2] if len(parts) > 2 else ""
                fill_hex = parts[5] if len(parts) > 5 else "none"
            is_solid = fill_hex.strip().lower() not in ("", "none", "transparent")
            for pts_raw, closed in parse_svg_path_subpaths(path_str):
                if len(pts_raw) < 2:
                    continue
                pts_mils = [_e2a_pt(px, py, ox, oy) for px, py in pts_raw]
                out.polys.append(
                    SchPolyModel(
                        points_mils=pts_mils,
                        stroke_color=_hex_to_win32_color(stroke_hex, default_stroke),
                        fill_color=_hex_to_win32_color(fill_hex, default_fill),
                        is_closed=closed or is_solid,
                        is_solid=is_solid,
                        part_id=part_id,
                    )
                )

        elif kind == "T" and len(parts) >= 12:
            # T~type~x~y~rot~color~font~size~bold~italic~baseline~align~text~...
            t_type = parts[1].strip().upper()
            if t_type in ("P", "N"):  # Skip auto prefix/name text (Altium adds Designator/Comment)
                continue
            tx, ty = _e2a_pt(_to_float(parts[2]), _to_float(parts[3]), ox, oy)
            rot = _to_int(parts[4], 0) % 360
            color = _hex_to_win32_color(parts[5], 0x000000)
            text_val = parts[12].strip() if len(parts) > 12 else ""
            if text_val:
                out.texts.append(
                    SchTextModel(
                        text=text_val,
                        x_mils=tx,
                        y_mils=ty,
                        rotation_deg=rot,
                        color=color,
                        part_id=part_id,
                    )
                )


def parse_easyeda_symbol(raw_result: dict[str, Any]) -> ParsedSymbol:
    """Parse EasyEDA component JSON (including subparts) into a ParsedSymbol."""
    out = ParsedSymbol()
    subparts = raw_result.get("subparts")
    if isinstance(subparts, list) and len(subparts) > 1:
        out.part_count = len(subparts)
        for idx, sub in enumerate(subparts, start=1):
            ds = sub.get("dataStr") if isinstance(sub, dict) else None
            if isinstance(ds, str):
                ds = json.loads(ds)
            if isinstance(ds, dict):
                _parse_symbol_data_str(ds, out, part_id=idx)
    else:
        ds = raw_result.get("dataStr")
        if isinstance(ds, str):
            ds = json.loads(ds)
        if isinstance(ds, dict):
            _parse_symbol_data_str(ds, out, part_id=1)

    # Ensure single-part symbols that somehow only have a bounding box look clean
    return out


# ============================================================================
# PCB Footprint Models & Parser
# ============================================================================

@dataclass
class FpPadModel:
    designator: str
    x_mils: float
    y_mils: float
    width_mils: float
    height_mils: float
    shape: str  # "RECT", "ELLIPSE", "OVAL", "POLYGON"
    layer_id: int  # 1=Top, 2=Bottom, 11=Multi-Layer
    rotation_deg: float = 0.0
    hole_diameter_mils: float = 0.0
    slot_length_mils: float = 0.0
    plated: bool = True
    polygon_points_mils: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class FpTrackModel:
    x1_mils: float
    y1_mils: float
    x2_mils: float
    y2_mils: float
    width_mils: float
    layer_id: int


@dataclass
class FpArcModel:
    cx_mils: float
    cy_mils: float
    radius_mils: float
    start_angle_deg: float
    end_angle_deg: float
    width_mils: float
    layer_id: int


@dataclass
class FpRegionModel:
    points_mils: list[tuple[float, float]]
    layer_id: int
    is_cutout: bool = False


@dataclass
class FpTextModel:
    text: str
    x_mils: float
    y_mils: float
    height_mils: float
    stroke_width_mils: float
    rotation_deg: float
    layer_id: int


@dataclass
class FpModel3DInfo:
    uuid: str
    title: str
    origin_x_mils: float
    origin_y_mils: float
    z_bottom_mils: float
    rot_x_deg: float
    rot_y_deg: float
    rot_z_deg: float
    width_mils: float
    height_mils: float
    outline_bounds_mils: tuple[float, float, float, float] | None = None


@dataclass
class ParsedFootprint:
    name: str
    pads: list[FpPadModel] = field(default_factory=list)
    tracks: list[FpTrackModel] = field(default_factory=list)
    arcs: list[FpArcModel] = field(default_factory=list)
    regions: list[FpRegionModel] = field(default_factory=list)
    texts: list[FpTextModel] = field(default_factory=list)
    model_3d: FpModel3DInfo | None = None


# EasyEDA layers to skip (EasyEDA's fake internal 2D-to-3D preview layers & ratlines)
_IGNORED_FP_LAYERS = frozenset({9, 19, 99, 100, 101})


def _parse_svgnode_3d(raw: str, ox: float, oy: float) -> FpModel3DInfo | None:
    try:
        payload = json.loads(raw.split("~", 1)[1])
    except Exception:
        return None
    attrs = payload.get("attrs") or {}
    uuid_val = str(attrs.get("uuid") or attrs.get("c_uuid") or "").strip()
    if not uuid_val:
        return None

    title = str(attrs.get("title") or "").strip()
    c_origin_str = str(attrs.get("c_origin") or f"{ox},{oy}")
    c_rot_str = str(attrs.get("c_rotation") or "0,0,0")

    orig_nums = [float(m) for m in _NUM_RE.findall(c_origin_str)]
    cx_raw = orig_nums[0] if len(orig_nums) >= 1 else ox
    cy_raw = orig_nums[1] if len(orig_nums) >= 2 else oy

    rot_nums = [float(m) for m in _NUM_RE.findall(c_rot_str)]
    rx = rot_nums[0] if len(rot_nums) >= 1 else 0.0
    ry = rot_nums[1] if len(rot_nums) >= 2 else 0.0
    rz = rot_nums[2] if len(rot_nums) >= 3 else 0.0

    z_bottom_mils = _to_float(attrs.get("z"), 0.0) * EASYEDA_TO_MILS
    width_mils = _to_float(attrs.get("c_width"), 0.0) * EASYEDA_TO_MILS
    height_mils = _to_float(attrs.get("c_height"), 0.0) * EASYEDA_TO_MILS

    # Compute exact 3D outline bounding box from childNodes polylines (immune to stale c_origin!)
    xs_alt: list[float] = []
    ys_alt: list[float] = []
    for child in payload.get("childNodes") or []:
        c_attrs = child.get("attrs") or {}
        pts_str = c_attrs.get("points") or ""
        nums = [float(m) for m in _NUM_RE.findall(pts_str)]
        for i in range(0, len(nums) - 1, 2):
            ax, ay = _e2a_pt(nums[i], nums[i + 1], ox, oy)
            xs_alt.append(ax)
            ys_alt.append(ay)

    outline_bounds: tuple[float, float, float, float] | None = None
    if xs_alt and ys_alt:
        outline_bounds = (min(xs_alt), min(ys_alt), max(xs_alt), max(ys_alt))
        origin_x_mils = (outline_bounds[0] + outline_bounds[2]) / 2.0
        origin_y_mils = (outline_bounds[1] + outline_bounds[3]) / 2.0
    else:
        origin_x_mils, origin_y_mils = _e2a_pt(cx_raw, cy_raw, ox, oy)

    return FpModel3DInfo(
        uuid=uuid_val,
        title=title,
        origin_x_mils=origin_x_mils,
        origin_y_mils=origin_y_mils,
        z_bottom_mils=z_bottom_mils,
        rot_x_deg=rx,
        rot_y_deg=ry,
        rot_z_deg=rz,
        width_mils=width_mils,
        height_mils=height_mils,
        outline_bounds_mils=outline_bounds,
    )


def parse_easyeda_footprint(raw_result: dict[str, Any], fallback_name: str) -> ParsedFootprint:
    """Parse EasyEDA packageDetail JSON into a clean ParsedFootprint."""
    pkg = raw_result.get("packageDetail") or {}
    if not isinstance(pkg, dict):
        return ParsedFootprint(name=fallback_name)

    fp_name = str(pkg.get("title") or fallback_name).strip() or fallback_name
    pds = pkg.get("dataStr")
    if isinstance(pds, str):
        pds = json.loads(pds)
    if not isinstance(pds, dict):
        return ParsedFootprint(name=fp_name)

    head = pds.get("head") or {}
    ox = _to_float(head.get("x"), 0.0)
    oy = _to_float(head.get("y"), 0.0)
    shapes = pds.get("shape") or []

    out = ParsedFootprint(name=fp_name)

    for raw in shapes:
        if not isinstance(raw, str) or "~" not in raw:
            continue
        kind = raw.split("~", 1)[0]
        parts = raw.split("~")

        if kind == "PAD" and len(parts) >= 12:
            # PAD~shape~x~y~w~h~layer~net~num~hole_r~points~rot~gId~hole_len~hole_pts~plated~locked...
            shape_str = parts[1].strip().upper()
            px, py = _e2a_pt(_to_float(parts[2]), _to_float(parts[3]), ox, oy)
            w_mils = abs(_to_float(parts[4])) * EASYEDA_TO_MILS
            h_mils = abs(_to_float(parts[5])) * EASYEDA_TO_MILS
            layer_id = _to_int(parts[6], 1)
            designator = parts[8].strip()
            hole_r_mils = abs(_to_float(parts[9])) * EASYEDA_TO_MILS
            hole_d_mils = hole_r_mils * 2.0
            pts_str = parts[10] if len(parts) > 10 else ""
            rot_easy = _to_float(parts[11], 0.0)
            hole_len_mils = abs(_to_float(parts[13], 0.0)) * EASYEDA_TO_MILS if len(parts) > 13 else 0.0
            plated = (parts[15].strip().upper() != "N") if len(parts) > 15 and parts[15].strip() else True

            poly_pts: list[tuple[float, float]] = []
            if shape_str == "POLYGON" and pts_str.strip():
                nums = [float(m) for m in _NUM_RE.findall(pts_str)]
                for i in range(0, len(nums) - 1, 2):
                    ax, ay = _e2a_pt(nums[i], nums[i + 1], ox, oy)
                    # Local coordinates relative to pad center
                    poly_pts.append((ax - px, ay - py))

            # EasyEDA rotation is CCW in Y-down (wait: EasyEDA 2D pads rotate CW visually in Y-down,
            # which equals CCW in Y-up OR negated depending on sign; let's preserve exact (-rot) % 360)
            alt_rot = (-rot_easy) % 360.0 if rot_easy != 0.0 else 0.0

            # If pad has a hole, ensure Multi-Layer
            if hole_d_mils > 0.1 and layer_id in (1, 2):
                layer_id = 11

            out.pads.append(
                FpPadModel(
                    designator=designator,
                    x_mils=px,
                    y_mils=py,
                    width_mils=max(w_mils, 1.0),
                    height_mils=max(h_mils, 1.0),
                    shape=shape_str,
                    layer_id=layer_id,
                    rotation_deg=alt_rot,
                    hole_diameter_mils=hole_d_mils,
                    slot_length_mils=hole_len_mils,
                    plated=plated,
                    polygon_points_mils=poly_pts,
                )
            )

        elif kind == "HOLE" and len(parts) >= 4:
            # HOLE~x~y~radius~gId~locked
            hx, hy = _e2a_pt(_to_float(parts[1]), _to_float(parts[2]), ox, oy)
            diam_mils = abs(_to_float(parts[3])) * 2.0 * EASYEDA_TO_MILS
            if diam_mils > 0.1:
                out.pads.append(
                    FpPadModel(
                        designator="",
                        x_mils=hx,
                        y_mils=hy,
                        width_mils=diam_mils,
                        height_mils=diam_mils,
                        shape="ELLIPSE",
                        layer_id=11,
                        rotation_deg=0.0,
                        hole_diameter_mils=diam_mils,
                        plated=False,
                    )
                )

        elif kind == "TRACK" and len(parts) >= 5:
            # TRACK~width~layer~net~x1 y1 x2 y2 ...~gId~locked
            width_mils = max(abs(_to_float(parts[1], 0.5)) * EASYEDA_TO_MILS, 1.0)
            layer_id = _to_int(parts[2], 3)
            if layer_id in _IGNORED_FP_LAYERS:
                continue
            nums = [float(m) for m in _NUM_RE.findall(parts[4])]
            pts = [
                _e2a_pt(nums[i], nums[i + 1], ox, oy)
                for i in range(0, len(nums) - 1, 2)
            ]
            for i in range(len(pts) - 1):
                if math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) > 1e-4:
                    out.tracks.append(
                        FpTrackModel(
                            x1_mils=pts[i][0],
                            y1_mils=pts[i][1],
                            x2_mils=pts[i + 1][0],
                            y2_mils=pts[i + 1][1],
                            width_mils=width_mils,
                            layer_id=layer_id,
                        )
                    )

        elif kind == "CIRCLE" and len(parts) >= 6:
            # CIRCLE~cx~cy~r~stroke_w~layer~gId~locked
            layer_id = _to_int(parts[5], 3)
            if layer_id in _IGNORED_FP_LAYERS:
                continue
            cx, cy = _e2a_pt(_to_float(parts[1]), _to_float(parts[2]), ox, oy)
            r_mils = abs(_to_float(parts[3])) * EASYEDA_TO_MILS
            w_mils = max(abs(_to_float(parts[4], 0.5)) * EASYEDA_TO_MILS, 1.0)
            if r_mils > 0.1:
                out.arcs.append(
                    FpArcModel(
                        cx_mils=cx,
                        cy_mils=cy,
                        radius_mils=r_mils,
                        start_angle_deg=0.0,
                        end_angle_deg=360.0,
                        width_mils=w_mils,
                        layer_id=layer_id,
                    )
                )

        elif kind == "ARC" and len(parts) >= 5:
            # ARC~stroke_w~layer~net~M x1 y1 A rx ry rot large sweep x2 y2~helper~gId~locked
            w_mils = max(abs(_to_float(parts[1], 0.5)) * EASYEDA_TO_MILS, 1.0)
            layer_id = _to_int(parts[2], 3)
            if layer_id in _IGNORED_FP_LAYERS:
                continue
            path_str = parts[4]
            m = re.search(
                r"M\s*([-+0-9.eE]+)[\s,]+([-+0-9.eE]+)\s*A\s*"
                r"([-+0-9.eE]+)[\s,]+([-+0-9.eE]+)[\s,]+([-+0-9.eE]+)[\s,]+"
                r"([01])[\s,]+([01])[\s,]+([-+0-9.eE]+)[\s,]+([-+0-9.eE]+)",
                path_str,
                re.IGNORECASE,
            )
            if m:
                x1, y1 = _e2a_pt(float(m.group(1)), float(m.group(2)), ox, oy)
                rx = abs(float(m.group(3))) * EASYEDA_TO_MILS
                ry = abs(float(m.group(4))) * EASYEDA_TO_MILS
                rot = -float(m.group(5))
                large_arc = int(m.group(6))
                # Flipping Y reverses sweep direction!
                sweep_alt = 1 - int(m.group(7))
                x2, y2 = _e2a_pt(float(m.group(8)), float(m.group(9)), ox, oy)

                # If circular arc, emit native Altium arc; if elliptical, sample tracks
                if abs(rx - ry) <= max(rx, ry) * 0.05 + 0.1:
                    res = svg_arc_endpoint_to_center(
                        x1, y1, rx, ry, rot, large_arc, sweep_alt, x2, y2
                    )
                    if res is not None:
                        cx, cy, r_fit, _, theta1_deg, dtheta_deg = res
                        if dtheta_deg >= 0:
                            start_deg = theta1_deg % 360.0
                            end_deg = (theta1_deg + dtheta_deg) % 360.0
                        else:
                            start_deg = (theta1_deg + dtheta_deg) % 360.0
                            end_deg = theta1_deg % 360.0
                        out.arcs.append(
                            FpArcModel(
                                cx_mils=cx,
                                cy_mils=cy,
                                radius_mils=r_fit,
                                start_angle_deg=start_deg,
                                end_angle_deg=end_deg,
                                width_mils=w_mils,
                                layer_id=layer_id,
                            )
                        )
                else:
                    pts = [(x1, y1)] + _sample_svg_arc(
                        x1, y1, rx, ry, rot, large_arc, sweep_alt, x2, y2
                    )
                    for i in range(len(pts) - 1):
                        out.tracks.append(
                            FpTrackModel(
                                x1_mils=pts[i][0],
                                y1_mils=pts[i][1],
                                x2_mils=pts[i + 1][0],
                                y2_mils=pts[i + 1][1],
                                width_mils=w_mils,
                                layer_id=layer_id,
                            )
                        )

        elif kind == "SOLIDREGION" and len(parts) >= 5:
            # SOLIDREGION~layer~net~svg_path~fill_type~gId~...
            layer_id = _to_int(parts[1], 1)
            if layer_id in _IGNORED_FP_LAYERS:
                continue
            path_str = parts[3]
            fill_type = parts[4].strip().lower()
            is_cutout = fill_type in ("npth", "cutout")
            # Skip non-copper mechanical fills on BoardOutLine unless it's a cutout
            if layer_id == 10 and not is_cutout:
                continue
            for pts_raw, _ in parse_svg_path_subpaths(path_str):
                if len(pts_raw) < 3:
                    continue
                pts_mils = [_e2a_pt(px, py, ox, oy) for px, py in pts_raw]
                # Deduplicate consecutive identical points
                dedup: list[tuple[float, float]] = []
                for pt in pts_mils:
                    if not dedup or math.hypot(pt[0] - dedup[-1][0], pt[1] - dedup[-1][1]) > 1e-3:
                        dedup.append(pt)
                if len(dedup) >= 3 and math.hypot(dedup[0][0] - dedup[-1][0], dedup[0][1] - dedup[-1][1]) < 1e-3:
                    dedup.pop()
                if len(dedup) >= 3:
                    out.regions.append(
                        FpRegionModel(
                            points_mils=dedup,
                            layer_id=layer_id,
                            is_cutout=is_cutout,
                        )
                    )

        elif kind == "RECT" and len(parts) >= 6:
            # RECT~x~y~w~h~layer~gId~locked~stroke_w~fill
            x = _to_float(parts[1])
            y = _to_float(parts[2])
            w = _to_float(parts[3])
            h = _to_float(parts[4])
            layer_id = _to_int(parts[5], 3)
            if layer_id in _IGNORED_FP_LAYERS:
                continue
            ax1, ay1 = _e2a_pt(x, y, ox, oy)
            ax2, ay2 = _e2a_pt(x + w, y + h, ox, oy)
            sw = max(_to_float(parts[8], 0.5) * EASYEDA_TO_MILS, 1.0) if len(parts) > 8 else 5.0
            corners = [(ax1, ay1), (ax2, ay1), (ax2, ay2), (ax1, ay2)]
            for i in range(4):
                p1, p2 = corners[i], corners[(i + 1) % 4]
                out.tracks.append(
                    FpTrackModel(
                        x1_mils=p1[0],
                        y1_mils=p1[1],
                        x2_mils=p2[0],
                        y2_mils=p2[1],
                        width_mils=sw,
                        layer_id=layer_id,
                    )
                )

        elif kind == "TEXT" and len(parts) >= 11:
            # TEXT~type~x~y~stroke_w~rot~mirror~layer~net~font_size~text~...
            t_type = parts[1].strip().upper()
            if t_type in ("P", "N"):  # Designator/Comment handled automatically by Altium
                continue
            layer_id = _to_int(parts[7], 3)
            if layer_id in _IGNORED_FP_LAYERS:
                continue
            tx, ty = _e2a_pt(_to_float(parts[2]), _to_float(parts[3]), ox, oy)
            sw = max(_to_float(parts[4], 0.6) * EASYEDA_TO_MILS, 1.0)
            rot = (-_to_float(parts[5], 0.0)) % 360.0
            height = max(_to_float(parts[9], 4.0) * EASYEDA_TO_MILS, 10.0)
            text_val = parts[10].strip()
            if text_val:
                out.texts.append(
                    FpTextModel(
                        text=text_val,
                        x_mils=tx,
                        y_mils=ty,
                        height_mils=height,
                        stroke_width_mils=sw,
                        rotation_deg=rot,
                        layer_id=layer_id,
                    )
                )

        elif kind == "SVGNODE":
            model_info = _parse_svgnode_3d(raw, ox, oy)
            if model_info is not None:
                out.model_3d = model_info

    return out
