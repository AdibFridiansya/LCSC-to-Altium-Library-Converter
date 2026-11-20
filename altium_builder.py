"""Build native Altium .SchLib, .PcbLib (with clean embedded STEP), and compiled .IntLib."""

from __future__ import annotations

import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from altium_monkey import (
    AltiumIntLib,
    AltiumPcbLib,
    AltiumSchLib,
    ColorValue,
    IeeeSymbol,
    LineWidth,
    MechanicalLayerKind,
    PadShape,
    PcbBodyProjection,
    PcbLayer,
    PcbRegionKind,
    PinElectrical,
    Rotation90,
    SchPointMils,
    TextOrientation,
    compute_step_model_bounds_mils,
    make_sch_pin,
)

from easyeda_parser import (
    ParsedFootprint,
    ParsedSymbol,
    parse_easyeda_footprint,
    parse_easyeda_symbol,
)
from lcsc_api import ComponentBundle


@dataclass
class Model3DMetrics:
    width_mm: float
    depth_mm: float
    height_mm: float
    min_z_mm: float
    max_z_mm: float
    file_size_kb: float


@dataclass
class BuildResult:
    intlib_path: Path
    symbol_name: str
    footprint_name: str
    pin_count: int
    pad_count: int
    has_3d_step: bool
    model_3d_metrics: Model3DMetrics | None = None
    extra_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_LAYER_MAP: dict[int, PcbLayer] = {
    1: PcbLayer.TOP,
    2: PcbLayer.BOTTOM,
    3: PcbLayer.TOP_OVERLAY,
    4: PcbLayer.BOTTOM_OVERLAY,
    5: PcbLayer.TOP_PASTE,
    6: PcbLayer.BOTTOM_PASTE,
    7: PcbLayer.TOP_SOLDER,
    8: PcbLayer.BOTTOM_SOLDER,
    10: PcbLayer.MECHANICAL_1,
    11: PcbLayer.MULTI_LAYER,
    12: PcbLayer.MECHANICAL_1,
    13: PcbLayer.MECHANICAL_13,
    14: PcbLayer.MECHANICAL_14,
    15: PcbLayer.MECHANICAL_15,
}

_ELECTRICAL_MAP: dict[int, PinElectrical] = {
    0: PinElectrical.PASSIVE,
    1: PinElectrical.INPUT,
    2: PinElectrical.OUTPUT,
    3: PinElectrical.IO,
    4: PinElectrical.POWER,
}

_ROTATION_MAP: dict[int, Rotation90] = {
    0: Rotation90.DEG_0,
    90: Rotation90.DEG_90,
    180: Rotation90.DEG_180,
    270: Rotation90.DEG_270,
}

_TEXT_ROTATION_MAP: dict[int, TextOrientation] = {
    0: TextOrientation.DEGREES_0,
    90: TextOrientation.DEGREES_90,
    180: TextOrientation.DEGREES_180,
    270: TextOrientation.DEGREES_270,
}

_RESERVED_OLE_NAMES = frozenset(
    {"fileheader", "fileversioninfo", "library", "sectionkeys", "libadditional", "storage"}
)


def _sanitize_ascii_name(raw: str, fallback: str, max_len: int = 120) -> str:
    """Sanitize a string into clean printable ASCII suitable for Altium library names."""
    text = (raw or "").strip()
    # Replace non-ASCII or OLE-illegal chars with underscore
    cleaned = []
    for ch in text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E and ch not in '\\/:*?"<>|!':
            cleaned.append(ch)
        else:
            cleaned.append("_")
    s = re.sub(r"_+", "_", "".join(cleaned)).strip(" _.")
    if not s:
        s = fallback
    s = s[:max_len].strip(" _.")
    if not s or s.casefold() in _RESERVED_OLE_NAMES:
        s = f"{fallback}_{s}" if s else fallback
        s = s[:max_len]
    return s


def _sanitize_filename(raw: str, fallback: str) -> str:
    """Create a clean Windows filename preserving spaces (e.g. 'HI-LINK HLK-20M05')."""
    text = _sanitize_cp1252(raw, max_len=100)
    # Remove only Windows-illegal filename characters while keeping clean spaces & hyphens
    text = re.sub(r'[\\/:*?"<>|!]+', " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ._-")
    if not text:
        return fallback
    return text[:90].strip(" .")


def suggested_intlib_filename(title: str, fallback_lcsc: str) -> str:
    return f"{_sanitize_filename(title, fallback_lcsc)}.IntLib"


def _sanitize_cp1252(raw: str, max_len: int = 240) -> str:
    """Keep only CP1252-encodable printable text without Altium '|' record delimiter."""
    if not raw:
        return ""
    # Replace pipe '|' (Altium field delimiter) and control newlines
    src = str(raw).replace("|", "/").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    kept: list[str] = []
    for ch in src:
        if ord(ch) < 0x20:
            continue
        try:
            ch.encode("cp1252")
            kept.append(ch)
        except UnicodeEncodeError:
            pass
    text = "".join(kept)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;-")
    return text[:max_len]


def inspect_step_metrics(step_bytes: bytes, filename_hint: str = "model.step") -> Model3DMetrics | None:
    """Compute clean STEP 3D bounding dimensions in mm using wn-geometer."""
    try:
        b = compute_step_model_bounds_mils(step_bytes, filename_hint=filename_hint)
        w_mm = (b.bounds_mils[2] - b.bounds_mils[0]) * 0.0254
        d_mm = (b.bounds_mils[3] - b.bounds_mils[1]) * 0.0254
        min_z_mm = b.min_z_mils * 0.0254
        max_z_mm = b.max_z_mils * 0.0254
        h_mm = max(0.0, max_z_mm - min_z_mm)
        return Model3DMetrics(
            width_mm=w_mm,
            depth_mm=d_mm,
            height_mm=h_mm,
            min_z_mm=min_z_mm,
            max_z_mm=max_z_mm,
            file_size_kb=len(step_bytes) / 1024.0,
        )
    except Exception:
        return None


# Classic Altium Designer Win32 colors (0x00BBGGRR)
ALTIUM_IC_FILL = 0xB0FFFF      # #FFFFB0 iconic Altium cream-yellow IC body
ALTIUM_MAROON = 0x000080       # #800000 iconic Altium dark maroon border
ALTIUM_BLUE = 0xFF0000         # #0000FF classic Altium royal blue (passives/discretes)
ALTIUM_NAVY = 0x800000         # #000080 classic Altium dark navy (Designator/Comment)

_DISCRETE_PREFIXES = frozenset(
    {
        "R", "C", "L", "T", "D", "Q", "LED", "SW", "F", "FB",
        "Y", "X", "Z", "VR", "RV", "TH", "B", "K", "S", "BT",
        "LS", "BZ", "TVS", "ESD", "TR",
    }
)

_POWER_PIN_RE = re.compile(
    r"^(?:GND|AGND|DGND|PGND|SGND|VSS|AVSS|DVSS|VSSA|"
    r"VDD|AVDD|DVDD|IOVDD|VDDIO|VDDA|VDD33|VCC|AVCC|DVCC|"
    r"VBUS|VBAT|VREF|VREG|VIN|VOUT|VEE|VS|VP|VN|"
    r"3V3|5V|12V|24V|1V8|1V2|1V1|PWR|POWER)(?:[0-9_+\-].*)?$",
    re.IGNORECASE,
)


def _compute_grid_snap_shift(parsed: ParsedSymbol) -> tuple[float, float]:
    """Compute smallest (dx, dy) in [-50, +50] mils to snap pins onto Altium's 100-mil grid."""
    if not parsed.pins:
        return (0.0, 0.0)
    rem_x_counts: dict[int, int] = {}
    rem_y_counts: dict[int, int] = {}
    for pin in parsed.pins:
        rx = int(round(pin.body_x_mils)) % 100
        ry = int(round(pin.body_y_mils)) % 100
        rem_x_counts[rx] = rem_x_counts.get(rx, 0) + 1
        rem_y_counts[ry] = rem_y_counts.get(ry, 0) + 1
    best_rx = max(rem_x_counts, key=lambda k: rem_x_counts[k])
    best_ry = max(rem_y_counts, key=lambda k: rem_y_counts[k])
    dx = float(-best_rx if best_rx <= 50 else 100 - best_rx)
    dy = float(-best_ry if best_ry <= 50 else 100 - best_ry)
    return (dx, dy)


def _shift_symbol(parsed: ParsedSymbol, dx: float, dy: float) -> None:
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return
    for r in parsed.rects:
        r.x1_mils += dx
        r.x2_mils += dx
        r.y1_mils += dy
        r.y2_mils += dy
    for p in parsed.polys:
        p.points_mils = [(x + dx, y + dy) for x, y in p.points_mils]
    for e in parsed.ellipses:
        e.cx_mils += dx
        e.cy_mils += dy
    for a in parsed.arcs:
        a.cx_mils += dx
        a.cy_mils += dy
    for t in parsed.texts:
        t.x_mils += dx
        t.y_mils += dy
    for pin in parsed.pins:
        pin.body_x_mils += dx
        pin.body_y_mils += dy


def _all_pins_touch_inner_graphics(parsed: ParsedSymbol) -> bool:
    """Return True if every pin body endpoint touches a polyline/arc/ellipse (not just an outer box)."""
    if len(parsed.pins) < 2 or not (parsed.polys or parsed.arcs or parsed.ellipses):
        return False
    # Collect all non-rectangle graphic anchor points
    gfx_pts: list[tuple[float, float]] = []
    for p in parsed.polys:
        gfx_pts.extend(p.points_mils)
    for e in parsed.ellipses:
        gfx_pts.append((e.cx_mils, e.cy_mils))
    for a in parsed.arcs:
        gfx_pts.append((a.cx_mils, a.cy_mils))
    if not gfx_pts:
        return False
    for pin in parsed.pins:
        px, py = pin.body_x_mils, pin.body_y_mils
        if not any(math.hypot(gx - px, gy - py) <= 15.0 for gx, gy in gfx_pts):
            return False
    return True


def _apply_altium_native_style(parsed: ParsedSymbol, meta) -> None:
    """Restyle EasyEDA symbol primitives to match native Altium Designer aesthetics."""
    # 1. Snap entire symbol cleanly to Altium 100-mil grid
    dx, dy = _compute_grid_snap_shift(parsed)
    _shift_symbol(parsed, dx, dy)

    prefix_root = re.sub(r"[^A-Z]", "", (meta.prefix or "").upper())
    pins_touch_inner = _all_pins_touch_inner_graphics(parsed)
    is_discrete = (prefix_root in _DISCRETE_PREFIXES) and (
        not parsed.rects or pins_touch_inner
    )

    # 2. Apply native Altium color scheme
    if is_discrete:
        # Remove redundant EasyEDA decorative outer rectangle when windings already connect to pins
        if parsed.rects and pins_touch_inner:
            parsed.rects = []
        for p in parsed.polys:
            p.stroke_color = ALTIUM_BLUE
            if p.is_solid:
                p.fill_color = ALTIUM_BLUE
        for e in parsed.ellipses:
            e.stroke_color = ALTIUM_BLUE
            if e.is_solid:
                e.fill_color = ALTIUM_BLUE
        for a in parsed.arcs:
            a.stroke_color = ALTIUM_BLUE
        for t in parsed.texts:
            t.color = ALTIUM_NAVY
    else:
        # Boxed IC / Connector / Module: Iconic Altium Cream-Yellow fill + Maroon border
        for r in parsed.rects:
            r.stroke_color = ALTIUM_MAROON
            r.fill_color = ALTIUM_IC_FILL
            r.is_solid = True

        # If no rectangles exist (e.g. Op-Amp / Logic Gate drawn with a closed polygon), fill that polygon
        has_rects = bool(parsed.rects)
        for p in parsed.polys:
            p.stroke_color = ALTIUM_MAROON
            if p.is_closed and (p.is_solid or not has_rects):
                p.fill_color = ALTIUM_IC_FILL
                p.is_solid = True
        for e in parsed.ellipses:
            e.stroke_color = ALTIUM_MAROON
            if e.is_solid:
                e.fill_color = ALTIUM_MAROON
        for a in parsed.arcs:
            a.stroke_color = ALTIUM_MAROON

        # Filter out redundant decorative title/manufacturer text dumped inside IC boxes
        redundant_tokens = {
            _sanitize_cp1252(v).strip().casefold()
            for v in (meta.title, meta.mpn, meta.manufacturer, meta.lcsc_id)
            if v and _sanitize_cp1252(v).strip()
        }
        cleaned_texts = []
        for t in parsed.texts:
            txt_norm = _sanitize_cp1252(t.text).strip().casefold()
            if txt_norm in redundant_tokens:
                continue
            t.color = ALTIUM_MAROON
            cleaned_texts.append(t)
        parsed.texts = cleaned_texts

    # 3. Extend short pins to Altium standard length (200 mil, or 300 mil for >=8-pin ICs)
    target_pin_len = 300.0 if (not is_discrete and len(parsed.pins) >= 8) else 200.0
    for pin in parsed.pins:
        if pin.length_mils < target_pin_len:
            pin.length_mils = target_pin_len
        else:
            # Snap existing longer pins cleanly to 100-mil multiples
            pin.length_mils = max(target_pin_len, round(pin.length_mils / 100.0) * 100.0)


def _compute_symbol_bounds(
    parsed: ParsedSymbol, *, include_pin_tips: bool = False
) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for r in parsed.rects:
        xs.extend([r.x1_mils, r.x2_mils])
        ys.extend([r.y1_mils, r.y2_mils])
    for p in parsed.polys:
        for px, py in p.points_mils:
            xs.append(px)
            ys.append(py)
    for e in parsed.ellipses:
        xs.extend([e.cx_mils - e.rx_mils, e.cx_mils + e.rx_mils])
        ys.extend([e.cy_mils - e.ry_mils, e.cy_mils + e.ry_mils])
    for pin in parsed.pins:
        xs.append(pin.body_x_mils)
        ys.append(pin.body_y_mils)
        if include_pin_tips:
            rad = math.radians(pin.orientation_deg)
            xs.append(pin.body_x_mils + pin.length_mils * math.cos(rad))
            ys.append(pin.body_y_mils + pin.length_mils * math.sin(rad))
    if not xs or not ys:
        return (-200.0, -200.0, 200.0, 200.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _build_schlib(
    bundle: ComponentBundle,
    parsed: ParsedSymbol,
    symbol_storage_name: str,
    symbol_display_name: str,
    footprint_name: str,
    *,
    altium_native_style: bool = True,
) -> AltiumSchLib:
    meta = bundle.meta
    if altium_native_style:
        _apply_altium_native_style(parsed, meta)

    schlib = AltiumSchLib(show_comments_designators=True)
    desc = _sanitize_cp1252(meta.description) or _sanitize_cp1252(
        f"{meta.mpn} ({meta.package_name}) - LCSC {meta.lcsc_id}"
    )
    symbol = schlib.add_symbol(
        symbol_storage_name,
        description=desc[:240],
        original_name=symbol_display_name,
    )
    if parsed.part_count > 1:
        symbol.part_count = parsed.part_count

    # 1. Rectangles (drawn first as background)
    for r in parsed.rects:
        x1, y1 = int(round(r.x1_mils)), int(round(r.y1_mils))
        x2, y2 = int(round(r.x2_mils)), int(round(r.y2_mils))
        if x1 == x2 or y1 == y2:
            continue
        if r.rx_mils >= 10.0 or r.ry_mils >= 10.0:
            symbol.add_rounded_rectangle(
                x1,
                y1,
                x2,
                y2,
                corner_x_radius=max(10, int(round(r.rx_mils))),
                corner_y_radius=max(10, int(round(r.ry_mils))),
                color=r.stroke_color,
                area_color=r.fill_color,
                line_width=LineWidth.SMALL,
                is_solid=r.is_solid,
                owner_part_id=r.part_id,
            )
        else:
            symbol.add_rectangle(
                x1,
                y1,
                x2,
                y2,
                color=r.stroke_color,
                area_color=r.fill_color,
                line_width=LineWidth.SMALL,
                is_solid=r.is_solid,
                owner_part_id=r.part_id,
            )

    # 2. Polylines & Polygons
    for p in parsed.polys:
        verts = [(int(round(x)), int(round(y))) for x, y in p.points_mils]
        if len(verts) < 2:
            continue
        if p.is_closed and len(verts) >= 3:
            symbol.add_polygon(
                verts,
                color=p.stroke_color,
                area_color=p.fill_color,
                line_width=LineWidth.SMALL,
                is_solid=p.is_solid,
                owner_part_id=p.part_id,
            )
        else:
            symbol.add_polyline(
                verts,
                color=p.stroke_color,
                line_width=LineWidth.SMALL,
                owner_part_id=p.part_id,
            )

    # 3. Ellipses / Circles
    for e in parsed.ellipses:
        rx = max(10, int(round(e.rx_mils)))
        ry = max(10, int(round(e.ry_mils)))
        symbol.add_ellipse(
            int(round(e.cx_mils)),
            int(round(e.cy_mils)),
            rx,
            ry,
            color=e.stroke_color,
            area_color=e.fill_color,
            line_width=LineWidth.SMALL,
            is_solid=e.is_solid,
            owner_part_id=e.part_id,
        )

    # 4. Arcs
    for a in parsed.arcs:
        r_val = max(10, int(round(a.radius_mils)))
        symbol.add_arc(
            int(round(a.cx_mils)),
            int(round(a.cy_mils)),
            r_val,
            start_angle=a.start_angle_deg,
            end_angle=a.end_angle_deg,
            color=a.stroke_color,
            line_width=LineWidth.SMALL,
            owner_part_id=a.part_id,
        )

    # 5. Text labels
    for t in parsed.texts:
        clean_t = _sanitize_cp1252(t.text, max_len=120)
        if not clean_t:
            continue
        rot_snapped = (round(t.rotation_deg / 90.0) * 90) % 360
        symbol.add_label(
            clean_t,
            int(round(t.x_mils)),
            int(round(t.y_mils)),
            color=t.color,
            orientation=_TEXT_ROTATION_MAP.get(rot_snapped, TextOrientation.DEGREES_0),
            owner_part_id=t.part_id,
        )

    # 6. Pins
    for pin in parsed.pins:
        orient = _ROTATION_MAP.get(pin.orientation_deg, Rotation90.DEG_0)
        p_desig = _sanitize_cp1252(pin.number, max_len=32) or "1"
        p_name = _sanitize_cp1252(pin.name, max_len=64) or p_desig

        if altium_native_style:
            # Clean native Altium pins (POWER for power rails, PASSIVE for signal pins -> no <> clutter)
            if _POWER_PIN_RE.match(p_name.strip()):
                elec = PinElectrical.POWER
            else:
                elec = PinElectrical.PASSIVE
        else:
            elec = _ELECTRICAL_MAP.get(pin.electrical_type, PinElectrical.PASSIVE)

        sch_pin = make_sch_pin(
            designator=p_desig,
            name=p_name,
            location_mils=SchPointMils(
                int(round(pin.body_x_mils)),
                int(round(pin.body_y_mils)),
            ),
            orientation=orient,
            length_mils=max(10.0, float(round(pin.length_mils))),
            electrical_type=elec,
            pin_color=ColorValue.from_hex("#000000"),
            hidden=pin.hidden,
            name_visible=pin.name_visible,
            designator_visible=pin.number_visible,
            owner_part_id=pin.part_id,
            symbol_outer=IeeeSymbol.DOT if pin.has_dot else IeeeSymbol.NONE,
            symbol_inner_edge=IeeeSymbol.CLOCK if pin.has_clock else IeeeSymbol.NONE,
        )
        symbol.add_pin(sch_pin)

    # 7. Designator & Comment + BOM Parameters (Altium Dark Navy #000080)
    body_min_x, body_min_y, body_max_x, body_max_y = _compute_symbol_bounds(
        parsed, include_pin_tips=False
    )
    _, full_min_y, _, full_max_y = _compute_symbol_bounds(
        parsed, include_pin_tips=True
    )
    has_top_pins = any(pin.orientation_deg == 90 for pin in parsed.pins)
    has_bottom_pins = any(pin.orientation_deg == 270 for pin in parsed.pins)

    label_x = int(round(body_min_x / 100.0) * 100)
    desig_y = int(round(((full_max_y if has_top_pins else body_max_y) + 80.0) / 100.0) * 100)
    comment_y = int(round(((full_min_y if has_bottom_pins else body_min_y) - 160.0) / 100.0) * 100)

    prefix_clean = _sanitize_cp1252(meta.prefix, max_len=16) or "U?"
    symbol.add_designator(
        prefix_clean,
        x=label_x,
        y=desig_y,
        color=ALTIUM_NAVY if altium_native_style else 0x000000,
    )
    comment_param = symbol.add_parameter(
        "Comment",
        "=Value",
        x=label_x,
        y=comment_y,
        is_hidden=False,
    )
    if altium_native_style:
        comment_param.color = ALTIUM_NAVY

    clean_mfr = _sanitize_cp1252(meta.manufacturer)
    clean_mpn = _sanitize_cp1252(meta.mpn)
    clean_lcsc_url = _sanitize_cp1252(meta.lcsc_url, max_len=500)
    clean_ds_url = _sanitize_cp1252(meta.datasheet_url, max_len=500)

    bom_params: list[tuple[str, str]] = [
        ("Value", _sanitize_cp1252(symbol_display_name)),
        # Standard BOM / JLCPCB columns
        ("LCSC Part #", meta.lcsc_id),
        ("Manufacturer", clean_mfr),
        ("Manufacturer Part Number", clean_mpn),
        # Altium ActiveBOM & Native Supplier Link parameters
        ("Manufacturer 1", clean_mfr),
        ("Manufacturer Part Number 1", clean_mpn),
        ("Supplier 1", "LCSC"),
        ("Supplier Part Number 1", meta.lcsc_id),
    ]
    if meta.unit_price_usd is not None:
        bom_params.append(("Supplier Currency 1", "USD"))
        bom_params.append(("Supplier Unit Price 1", f"{meta.unit_price_usd:.4f}"))
    if meta.stock_qty is not None:
        bom_params.append(("Supplier Stock 1", str(meta.stock_qty)))

    bom_params.extend(
        [
            ("Package", _sanitize_cp1252(meta.packaging or meta.package_name)),
            ("Category", _sanitize_cp1252(meta.category)),
            ("JLCPCB Part Class", _sanitize_cp1252(meta.jlcpcb_class)),
            ("Datasheet", clean_ds_url),
            ("LCSC URL", clean_lcsc_url),
        ]
    )

    # Native Altium ComponentLinks (Right-click -> References & F1 Datasheet shortcut!)
    link_idx = 1
    if clean_ds_url:
        bom_params.append((f"ComponentLink{link_idx}Description", "Datasheet"))
        bom_params.append((f"ComponentLink{link_idx}URL", clean_ds_url))
        link_idx += 1
    if clean_lcsc_url:
        bom_params.append((f"ComponentLink{link_idx}Description", f"LCSC Product ({meta.lcsc_id})"))
        bom_params.append((f"ComponentLink{link_idx}URL", clean_lcsc_url))
        link_idx += 1
    for pname, pval in bom_params:
        if pval:
            symbol.add_parameter(pname, pval, x=label_x, y=comment_y, is_hidden=True)

    # 8. Footprint Implementation Link
    symbol.add_footprint(
        footprint_name,
        description=_sanitize_cp1252(meta.package_name, max_len=200),
        is_current=True,
    )

    return schlib


def _build_pcblib(
    bundle: ComponentBundle,
    parsed: ParsedFootprint,
    footprint_name: str,
    *,
    extra_rot_z_deg: float = 0.0,
    extra_z_offset_mm: float = 0.0,
    warnings_out: list[str],
) -> tuple[AltiumPcbLib, Model3DMetrics | None]:
    meta = bundle.meta
    pcblib = AltiumPcbLib()

    # Standard Altium Designer mechanical layer semantics
    try:
        pcblib.set_mechanical_layer_kind(PcbLayer.MECHANICAL_13, MechanicalLayerKind.BODY_3D_TOP)
        pcblib.set_mechanical_layer_kind(PcbLayer.MECHANICAL_15, MechanicalLayerKind.COURTYARD_TOP)
    except Exception:
        pass

    fp = pcblib.add_footprint(
        footprint_name,
        description=_sanitize_cp1252(meta.description or meta.package_name, max_len=200),
    )

    # 1. Pads (SMT, THT, Slotted, Custom Polygon, NPTH Holes)
    for pad in parsed.pads:
        layer = _LAYER_MAP.get(pad.layer_id, PcbLayer.TOP)
        if pad.hole_diameter_mils > 0.1:
            layer = PcbLayer.MULTI_LAYER

        if pad.shape == "POLYGON" and len(pad.polygon_points_mils) >= 3 and pad.hole_diameter_mils <= 0.1:
            # Clean duplicate vertices
            clean_pts: list[tuple[float, float]] = []
            for pt in pad.polygon_points_mils:
                if not clean_pts or math.hypot(pt[0] - clean_pts[-1][0], pt[1] - clean_pts[-1][1]) > 0.05:
                    clean_pts.append(pt)
            if len(clean_pts) >= 3 and math.hypot(clean_pts[0][0] - clean_pts[-1][0], clean_pts[0][1] - clean_pts[-1][1]) < 0.05:
                clean_pts.pop()
            if len(clean_pts) >= 3:
                try:
                    anchor_d = max(1.0, min(pad.width_mils, pad.height_mils) * 0.4)
                    fp.add_custom_pad(
                        designator=pad.designator,
                        position_mils=(pad.x_mils, pad.y_mils),
                        outline_points_mils=clean_pts,
                        layer=layer,
                        anchor_diameter_mils=anchor_d,
                        outline_points_are_local=True,
                    )
                    continue
                except Exception:
                    pass  # Fall back to standard rectangular pad below

        shape_enum = {
            "RECT": PadShape.RECTANGLE,
            "RECTANGLE": PadShape.RECTANGLE,
            "ELLIPSE": PadShape.CIRCLE,
            "OVAL": PadShape.CIRCLE,
            "POLYGON": PadShape.RECTANGLE,
        }.get(pad.shape, PadShape.RECTANGLE)

        pad_kwargs: dict = dict(
            designator=pad.designator,
            position_mils=(pad.x_mils, pad.y_mils),
            width_mils=pad.width_mils,
            height_mils=pad.height_mils,
            layer=layer,
            shape=shape_enum,
            rotation_degrees=pad.rotation_deg,
            hole_size_mils=pad.hole_diameter_mils,
            plated=pad.plated,
        )
        if pad.hole_diameter_mils > 0.1 and pad.slot_length_mils > pad.hole_diameter_mils + 0.5:
            pad_kwargs["hole_shape"] = "slot"
            pad_kwargs["slot_length_mils"] = pad.slot_length_mils
            pad_kwargs["slot_rotation_degrees"] = pad.rotation_deg

        fp.add_pad(**pad_kwargs)

    # 2. Tracks
    for tr in parsed.tracks:
        layer = _LAYER_MAP.get(tr.layer_id)
        if layer is None:
            continue
        fp.add_track(
            (tr.x1_mils, tr.y1_mils),
            (tr.x2_mils, tr.y2_mils),
            width_mils=tr.width_mils,
            layer=layer,
        )

    # 3. Arcs & Circles
    for arc in parsed.arcs:
        layer = _LAYER_MAP.get(arc.layer_id)
        if layer is None:
            continue
        fp.add_arc(
            center_mils=(arc.cx_mils, arc.cy_mils),
            radius_mils=arc.radius_mils,
            start_angle_degrees=arc.start_angle_deg,
            end_angle_degrees=arc.end_angle_deg,
            width_mils=arc.width_mils,
            layer=layer,
        )

    # 4. Solid Regions (Copper fills, solder/paste mask openings, board cutouts)
    for reg in parsed.regions:
        layer = _LAYER_MAP.get(reg.layer_id)
        if layer is None:
            continue
        try:
            fp.add_region(
                outline_points_mils=reg.points_mils,
                layer=PcbLayer.MULTI_LAYER if reg.is_cutout else layer,
                kind=PcbRegionKind.BOARD_CUTOUT if reg.is_cutout else PcbRegionKind.COPPER,
                is_board_cutout=reg.is_cutout,
            )
        except Exception:
            pass

    # 5. Silkscreen / Assembly Text
    for txt in parsed.texts:
        layer = _LAYER_MAP.get(txt.layer_id)
        if layer is None:
            continue
        try:
            fp.add_text(
                text=txt.text,
                position_mils=(txt.x_mils, txt.y_mils),
                height_mils=txt.height_mils,
                stroke_width_mils=txt.stroke_width_mils,
                rotation_degrees=txt.rotation_deg,
                layer=layer,
            )
        except Exception:
            pass

    # 6. Clean Embedded 3D STEP Model (PURE component geometry, ZERO PCB board slab!)
    metrics: Model3DMetrics | None = None
    if bundle.step_bytes:
        step_name = bundle.step_filename or f"{meta.lcsc_id}.step"
        m3d = parsed.model_3d

        rx = (m3d.rot_x_deg % 360.0) if m3d else 0.0
        ry = (m3d.rot_y_deg % 360.0) if m3d else 0.0
        # EasyEDA c_rotation Z vs Altium CCW Z + optional user override
        rz_base = (m3d.rot_z_deg % 360.0) if m3d else 0.0
        rz = (rz_base + extra_rot_z_deg) % 360.0

        loc_x, loc_y = 0.0, 0.0
        dz_mils = extra_z_offset_mm / 0.0254

        try:
            # Compute rotated STEP bounding box using wn-geometer for pixel-perfect alignment
            step_bounds = compute_step_model_bounds_mils(
                bundle.step_bytes,
                filename_hint=step_name,
                rotation_x_degrees=rx,
                rotation_y_degrees=ry,
                rotation_z_degrees=rz,
            )

            # Auto-check 90/270 Z-sign against EasyEDA 3D outline aspect ratio if no manual override
            if m3d and m3d.outline_bounds_mils and extra_rot_z_deg == 0.0 and rz_base in (90.0, 270.0):
                ol = m3d.outline_bounds_mils
                ol_w = ol[2] - ol[0]
                ol_h = ol[3] - ol[1]
                st_w = step_bounds.bounds_mils[2] - step_bounds.bounds_mils[0]
                st_h = step_bounds.bounds_mils[3] - step_bounds.bounds_mils[1]
                # Compare width/height match; if inverted, try negated Z rotation
                err_pos = abs(st_w - ol_w) + abs(st_h - ol_h)
                rz_alt = (-rz_base) % 360.0
                alt_bounds = compute_step_model_bounds_mils(
                    bundle.step_bytes,
                    filename_hint=step_name,
                    rotation_x_degrees=rx,
                    rotation_y_degrees=ry,
                    rotation_z_degrees=rz_alt,
                )
                err_alt = abs((alt_bounds.bounds_mils[2] - alt_bounds.bounds_mils[0]) - ol_w) + abs(
                    (alt_bounds.bounds_mils[3] - alt_bounds.bounds_mils[1]) - ol_h
                )
                if err_alt + 1.0 < err_pos:
                    rz = rz_alt
                    step_bounds = alt_bounds

            step_cx = (step_bounds.bounds_mils[0] + step_bounds.bounds_mils[2]) / 2.0
            step_cy = (step_bounds.bounds_mils[1] + step_bounds.bounds_mils[3]) / 2.0
            step_zmin = step_bounds.min_z_mils

            target_cx = m3d.origin_x_mils if m3d else 0.0
            target_cy = m3d.origin_y_mils if m3d else 0.0
            target_zmin = m3d.z_bottom_mils if m3d else 0.0

            loc_x = target_cx - step_cx
            loc_y = target_cy - step_cy
            dz_mils = (target_zmin - step_zmin) + (extra_z_offset_mm / 0.0254)

            final_min_z_mm = (step_zmin + dz_mils) * 0.0254
            final_max_z_mm = (step_bounds.max_z_mils + dz_mils) * 0.0254
            metrics = Model3DMetrics(
                width_mm=(step_bounds.bounds_mils[2] - step_bounds.bounds_mils[0]) * 0.0254,
                depth_mm=(step_bounds.bounds_mils[3] - step_bounds.bounds_mils[1]) * 0.0254,
                height_mm=max(0.0, final_max_z_mm - final_min_z_mm),
                min_z_mm=final_min_z_mm,
                max_z_mm=final_max_z_mm,
                file_size_kb=len(bundle.step_bytes) / 1024.0,
            )
            fp.parameters["HEIGHT"] = f"{max(0.0, step_bounds.max_z_mils + dz_mils):.2f}mil"
        except Exception as exc:
            warnings_out.append(f"Auto-align 3D STEP memakai fallback origin (detail: {exc})")

        embedded_model = pcblib.add_embedded_model(
            name=step_name,
            model_data=bundle.step_bytes,
            rotation_x_degrees=rx,
            rotation_y_degrees=ry,
            rotation_z_degrees=rz,
            z_offset_mils=dz_mils,
        )
        fp.add_embedded_3d_model(
            embedded_model,
            layer=PcbLayer.MECHANICAL_13,
            side=PcbBodyProjection.TOP,
            location_mils=(loc_x, loc_y),
            rotation_x_degrees=rx,
            rotation_y_degrees=ry,
            rotation_z_degrees=rz,
            standoff_height_mils=dz_mils,
            name=step_name,
        )

    return pcblib, metrics


def build_intlib(
    bundle: ComponentBundle,
    output_dir: Path | str,
    *,
    save_sources: bool = False,
    altium_native_style: bool = True,
    extra_rot_z_deg: float = 0.0,
    extra_z_offset_mm: float = 0.0,
) -> BuildResult:
    """Convert a fetched ComponentBundle directly into a standalone Altium .IntLib."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = bundle.meta
    warnings_list = list(bundle.warnings)

    parsed_sym = parse_easyeda_symbol(bundle.raw_result)
    parsed_fp = parse_easyeda_footprint(bundle.raw_result, fallback_name=f"{meta.lcsc_id}_FP")

    symbol_display_name = _sanitize_ascii_name(meta.title or meta.mpn, meta.lcsc_id, max_len=80)
    symbol_storage_name = _sanitize_ascii_name(meta.title or meta.mpn, meta.lcsc_id, max_len=31)
    footprint_name = _sanitize_ascii_name(parsed_fp.name or meta.package_name, f"{meta.lcsc_id}_FP", max_len=100)

    file_stem = _sanitize_filename(meta.title or meta.mpn, meta.lcsc_id)
    intlib_path = out_dir / f"{file_stem}.IntLib"

    schlib = _build_schlib(
        bundle,
        parsed_sym,
        symbol_storage_name=symbol_storage_name,
        symbol_display_name=symbol_display_name,
        footprint_name=footprint_name,
        altium_native_style=altium_native_style,
    )
    pcblib, metrics = _build_pcblib(
        bundle,
        parsed_fp,
        footprint_name=footprint_name,
        extra_rot_z_deg=extra_rot_z_deg,
        extra_z_offset_mm=extra_z_offset_mm,
        warnings_out=warnings_list,
    )

    extra_files: list[Path] = []
    if save_sources:
        src_dir = out_dir / f"{file_stem}_sources"
        src_dir.mkdir(parents=True, exist_ok=True)
        schlib_path = src_dir / f"{file_stem}.SchLib"
        pcblib_path = src_dir / f"{file_stem}.PcbLib"
        schlib.save(schlib_path, sync_pin_text_data=True)
        pcblib.save(pcblib_path)
        extra_files.extend([schlib_path, pcblib_path])
        if bundle.step_bytes:
            step_path = src_dir / (bundle.step_filename or f"{file_stem}.step")
            step_path.write_bytes(bundle.step_bytes)
            extra_files.append(step_path)
        AltiumIntLib.create_from_libraries(
            intlib_path,
            schlib_paths=[schlib_path],
            pcblib_paths=[pcblib_path],
            overwrite=True,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="lcsc_intlib_") as tmp:
            tmp_dir = Path(tmp)
            schlib_path = tmp_dir / f"{file_stem}.SchLib"
            pcblib_path = tmp_dir / f"{file_stem}.PcbLib"
            schlib.save(schlib_path, sync_pin_text_data=True)
            pcblib.save(pcblib_path)
            AltiumIntLib.create_from_libraries(
                intlib_path,
                schlib_paths=[schlib_path],
                pcblib_paths=[pcblib_path],
                overwrite=True,
            )

    return BuildResult(
        intlib_path=intlib_path,
        symbol_name=symbol_display_name,
        footprint_name=footprint_name,
        pin_count=len(parsed_sym.pins),
        pad_count=len(parsed_fp.pads),
        has_3d_step=bundle.step_bytes is not None,
        model_3d_metrics=metrics,
        extra_files=extra_files,
        warnings=warnings_list,
    )
