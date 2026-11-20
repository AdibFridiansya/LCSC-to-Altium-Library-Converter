"""Lightweight interactive 3D STEP & 2D PCB Footprint renderer for Altium-style preview."""

from __future__ import annotations

import hashlib
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geometer
import numpy as np
from PIL import Image, ImageDraw

from altium_monkey import AltiumIntLib, AltiumPcbLib, PcbLayer


@dataclass
class PreparedMesh3D:
    """Compact numpy-backed triangle mesh ready for fast interactive orbit rendering."""

    v0: np.ndarray          # (N, 3) float32 centered & normalized around origin
    v1: np.ndarray          # (N, 3) float32
    v2: np.ndarray          # (N, 3) float32
    normals: np.ndarray     # (N, 3) float32 unit face normals
    base_rgb: np.ndarray    # (N, 3) float32 colors in [0..255]
    radius: float           # bounding sphere radius (mm)
    size_mm: tuple[float, float, float]


@dataclass
class PreviewAsset:
    """Combined 3D mesh + 2D footprint primitives loaded from an .IntLib or STEP payload."""

    footprint_name: str
    library_label: str
    mesh_3d: PreparedMesh3D | None
    pads_2d: list[dict[str, Any]]
    tracks_2d: list[dict[str, Any]]
    arcs_2d: list[dict[str, Any]]


# In-memory cache so re-clicking any recent export is instant
_MESH_CACHE: dict[str, PreparedMesh3D] = {}


def tessellate_step_to_mesh(
    step_bytes: bytes,
    *,
    rot_x_deg: float = 0.0,
    rot_y_deg: float = 0.0,
    rot_z_deg: float = 0.0,
    max_triangles: int = 8500,
) -> PreparedMesh3D:
    """Tessellate raw STEP bytes into a compact colored numpy triangle mesh."""
    key = hashlib.sha256(step_bytes).hexdigest() + f":{rot_x_deg:.1f}:{rot_y_deg:.1f}:{rot_z_deg:.1f}"
    cached = _MESH_CACHE.get(key)
    if cached is not None:
        return cached

    # Adaptive deflection: start crisp; if too dense, geometer respects max_triangles or we retry coarser
    tess = None
    for deflection in (0.28, 0.55, 1.0):
        req = geometer.ModelTessellationRequestA0(
            schema="geometry.model_tessellation.request.a0",
            linear_deflection_mm=deflection,
            max_triangles=max_triangles,
        )
        try:
            tess = geometer.model_tessellation(step_bytes, options=req, timeout=20.0)
            break
        except Exception:
            continue

    if tess is None:
        # Final fallback with higher triangle cap
        req = geometer.ModelTessellationRequestA0(
            schema="geometry.model_tessellation.request.a0",
            linear_deflection_mm=1.2,
            max_triangles=35000,
        )
        tess = geometer.model_tessellation(step_bytes, options=req, timeout=25.0)

    v0_list: list[np.ndarray] = []
    v1_list: list[np.ndarray] = []
    v2_list: list[np.ndarray] = []
    rgb_list: list[np.ndarray] = []

    default_rgb = np.array([150.0, 155.0, 162.0], dtype=np.float32)

    for mesh in tess.mesh_collection.meshes:
        if not mesh.positions or not mesh.indices:
            continue
        pos = np.asarray(mesh.positions, dtype=np.float32).reshape(-1, 3)
        idx = np.asarray(mesh.indices, dtype=np.int32).reshape(-1, 3)
        tri_count = idx.shape[0]
        if tri_count == 0:
            continue

        t0 = pos[idx[:, 0]]
        t1 = pos[idx[:, 1]]
        t2 = pos[idx[:, 2]]
        v0_list.append(t0)
        v1_list.append(t1)
        v2_list.append(t2)

        # Resolve per-triangle RGB material colors
        mats = mesh.materials or ()
        if mats:
            mat_colors = np.array(
                [
                    [
                        max(0.0, min(1.0, float(m.color[0]))) * 255.0,
                        max(0.0, min(1.0, float(m.color[1]))) * 255.0,
                        max(0.0, min(1.0, float(m.color[2]))) * 255.0,
                    ]
                    if getattr(m, "color", None) is not None
                    else default_rgb
                    for m in mats
                ],
                dtype=np.float32,
            )
            if mesh.triangle_material_indices is not None and len(mesh.triangle_material_indices) == tri_count:
                m_idx = np.asarray(mesh.triangle_material_indices, dtype=np.int32)
                colors = mat_colors[m_idx]
            else:
                colors = np.broadcast_to(mat_colors[0], (tri_count, 3)).copy()
        else:
            colors = np.broadcast_to(default_rgb, (tri_count, 3)).copy()

        # Lift pure-black bodies slightly so shaded facets remain readable (just like Altium 3D)
        luma = colors[:, 0] * 0.299 + colors[:, 1] * 0.587 + colors[:, 2] * 0.114
        too_dark = luma < 28.0
        if np.any(too_dark):
            colors[too_dark] = np.clip(colors[too_dark] + 42.0, 0.0, 255.0)
        rgb_list.append(colors)

    if not v0_list:
        raise ValueError("STEP model contains no renderable triangles.")

    v0 = np.concatenate(v0_list, axis=0)
    v1 = np.concatenate(v1_list, axis=0)
    v2 = np.concatenate(v2_list, axis=0)
    base_rgb = np.concatenate(rgb_list, axis=0)

    # Decimate uniformly only if extremely dense (keeps mouse orbit ultra responsive)
    if v0.shape[0] > 9500:
        step = int(math.ceil(v0.shape[0] / 9000))
        v0 = v0[::step]
        v1 = v1[::step]
        v2 = v2[::step]
        base_rgb = base_rgb[::step]

    # Apply component model placement rotation (Altium order: X, then Y, then Z)
    if any(abs(a) > 1e-3 for a in (rot_x_deg, rot_y_deg, rot_z_deg)):
        rot_m = _euler_xyz_matrix(rot_x_deg, rot_y_deg, rot_z_deg)
        v0 = v0 @ rot_m.T
        v1 = v1 @ rot_m.T
        v2 = v2 @ rot_m.T

    all_v = np.concatenate([v0, v1, v2], axis=0)
    vmin = all_v.min(axis=0)
    vmax = all_v.max(axis=0)
    center = (vmin + vmax) * 0.5
    size = vmax - vmin

    v0 = v0 - center
    v1 = v1 - center
    v2 = v2 - center

    # Face normals
    e1 = v1 - v0
    e2 = v2 - v0
    normals = np.cross(e1, e2)
    lens = np.linalg.norm(normals, axis=1, keepdims=True)
    valid = (lens[:, 0] > 1e-8)
    v0 = v0[valid]
    v1 = v1[valid]
    v2 = v2[valid]
    base_rgb = base_rgb[valid]
    normals = normals[valid] / lens[valid]

    radius = float(np.max(np.linalg.norm(all_v - center, axis=1)))
    if radius < 1e-4:
        radius = 1.0

    prepared = PreparedMesh3D(
        v0=v0,
        v1=v1,
        v2=v2,
        normals=normals,
        base_rgb=base_rgb,
        radius=radius,
        size_mm=(float(size[0]), float(size[1]), float(size[2])),
    )
    if len(_MESH_CACHE) > 24:
        _MESH_CACHE.pop(next(iter(_MESH_CACHE)))
    _MESH_CACHE[key] = prepared
    return prepared


def load_preview_from_intlib(intlib_path: Path | str) -> PreviewAsset:
    """Extract footprint 2D primitives and embedded 3D STEP mesh from a compiled .IntLib."""
    p = Path(intlib_path)
    il = AltiumIntLib.from_file(p)
    try:
        with tempfile.TemporaryDirectory(prefix="lcsc_prev_") as td:
            ex = il.extract_sources(td, overwrite=True, use_original_filenames=False, write_libpkg=False)
            pcb_files = [s.output_path for s in ex.sources if s.kind == "PCBLib" and s.output_path]
            if not pcb_files:
                return PreviewAsset(
                    footprint_name=p.stem,
                    library_label=str(p),
                    mesh_3d=None,
                    pads_2d=[],
                    tracks_2d=[],
                    arcs_2d=[],
                )
            pcb = AltiumPcbLib(pcb_files[0])
            if not pcb.footprints:
                return PreviewAsset(
                    footprint_name=p.stem,
                    library_label=str(p),
                    mesh_3d=None,
                    pads_2d=[],
                    tracks_2d=[],
                    arcs_2d=[],
                )
            fp = pcb.footprints[0]
            pads_2d, tracks_2d, arcs_2d = _extract_2d_primitives(fp)

            mesh_3d: PreparedMesh3D | None = None
            models = pcb.embedded_model_summaries()
            if models:
                step_bytes = pcb.get_embedded_model_payload(0)
                rx, ry, rz = 0.0, 0.0, 0.0
                if fp.component_bodies:
                    b0 = fp.component_bodies[0]
                    rx = float(getattr(b0, "model_3d_rotx", 0.0) or 0.0)
                    ry = float(getattr(b0, "model_3d_roty", 0.0) or 0.0)
                    rz = float(getattr(b0, "model_3d_rotz", 0.0) or 0.0)
                try:
                    mesh_3d = tessellate_step_to_mesh(step_bytes, rot_x_deg=rx, rot_y_deg=ry, rot_z_deg=rz)
                except Exception:
                    mesh_3d = None

            return PreviewAsset(
                footprint_name=fp.name,
                library_label=str(p),
                mesh_3d=mesh_3d,
                pads_2d=pads_2d,
                tracks_2d=tracks_2d,
                arcs_2d=arcs_2d,
            )
    finally:
        il.close()


def _extract_2d_primitives(fp: Any) -> tuple[list[dict], list[dict], list[dict]]:
    """Extract simple mil-space 2D primitives from an AltiumPcbFootprint."""
    pads: list[dict] = []
    for pad in getattr(fp, "pads", []) or []:
        x = float(getattr(pad, "x", 0)) / 10000.0
        y = float(getattr(pad, "y", 0)) / 10000.0
        w = float(getattr(pad, "size_x", getattr(pad, "x_size_top", 200000))) / 10000.0
        h = float(getattr(pad, "size_y", getattr(pad, "y_size_top", 200000))) / 10000.0
        hole = float(getattr(pad, "hole_size", 0)) / 10000.0
        rot = float(getattr(pad, "rotation", 0.0) or 0.0)
        layer = int(getattr(pad, "layer", 1) or 1)
        shape = int(getattr(pad, "shape", getattr(pad, "shape_top", 2)) or 2)
        pads.append({"x": x, "y": y, "w": max(w, 4.0), "h": max(h, 4.0), "hole": hole, "rot": rot, "layer": layer, "shape": shape})

    tracks: list[dict] = []
    for tr in getattr(fp, "tracks", []) or []:
        x1 = float(getattr(tr, "x1", 0)) / 10000.0
        y1 = float(getattr(tr, "y1", 0)) / 10000.0
        x2 = float(getattr(tr, "x2", 0)) / 10000.0
        y2 = float(getattr(tr, "y2", 0)) / 10000.0
        w = max(float(getattr(tr, "width", 50000)) / 10000.0, 2.0)
        layer = int(getattr(tr, "layer", int(PcbLayer.TOP_OVERLAY)) or int(PcbLayer.TOP_OVERLAY))
        tracks.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "w": w, "layer": layer})

    arcs: list[dict] = []
    for arc in getattr(fp, "arcs", []) or []:
        cx = float(getattr(arc, "x", getattr(arc, "center_x", 0))) / 10000.0
        cy = float(getattr(arc, "y", getattr(arc, "center_y", 0))) / 10000.0
        r = float(getattr(arc, "radius", 0)) / 10000.0
        sa = float(getattr(arc, "start_angle", 0.0) or 0.0)
        ea = float(getattr(arc, "end_angle", 360.0) or 360.0)
        layer = int(getattr(arc, "layer", int(PcbLayer.TOP_OVERLAY)) or int(PcbLayer.TOP_OVERLAY))
        if r > 0.5:
            arcs.append({"cx": cx, "cy": cy, "r": r, "sa": sa, "ea": ea, "layer": layer})

    return pads, tracks, arcs


def _euler_xyz_matrix(rx_deg: float, ry_deg: float, rz_deg: float) -> np.ndarray:
    rx, ry, rz = math.radians(rx_deg), math.radians(ry_deg), math.radians(rz_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    mx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float32)
    my = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float32)
    mz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float32)
    return (mz @ my @ mx).astype(np.float32)


def _camera_rotation(yaw_deg: float, pitch_deg: float) -> np.ndarray:
    """View rotation bringing PCB Z-up model into camera space (X right, Y up, Z toward viewer)."""
    # First rotate around Z (yaw/turntable), then tilt around X so Z-up tilts into view
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)

    # World Z-up -> camera: start looking from -Y toward +Y with Z up:
    # cam_x = x, cam_y = z, cam_z = -y (before pitch)
    rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)
    base = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]], dtype=np.float32)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cp, -sp], [0.0, sp, cp]], dtype=np.float32)
    return (rx @ base @ rz).astype(np.float32)


def render_3d_preview(
    mesh: PreparedMesh3D,
    width: int,
    height: int,
    *,
    yaw_deg: float = -38.0,
    pitch_deg: float = 32.0,
    zoom: float = 1.0,
    bg_rgb: tuple[int, int, int] = (247, 247, 247),
    supersample: int = 1,
) -> Image.Image:
    """Render a shaded 3D preview image using vectorized numpy projection + Pillow."""
    w = max(64, int(width))
    h = max(64, int(height))
    rw = w * supersample
    rh = h * supersample

    rot = _camera_rotation(yaw_deg, pitch_deg)
    rt = rot.T

    p0 = mesh.v0 @ rt
    p1 = mesh.v1 @ rt
    p2 = mesh.v2 @ rt
    n_cam = mesh.normals @ rt

    # Two-sided shading (STEP shells occasionally have mixed winding) + slight backface culling when safe
    # Using abs(n_cam[:, 2]) ensures zero missing faces even on open STEP shells!
    key_light = np.array([0.40, 0.65, 0.64], dtype=np.float32)
    fill_light = np.array([-0.55, -0.20, 0.81], dtype=np.float32)

    # Flip normal toward camera so every face shades cleanly
    flip = np.where(n_cam[:, 2:3] < 0.0, -1.0, 1.0).astype(np.float32)
    n_vis = n_cam * flip

    d_key = np.clip(n_vis @ key_light, 0.0, 1.0)
    d_fill = np.clip(n_vis @ fill_light, 0.0, 1.0)
    intensity = 0.28 + 0.58 * d_key + 0.22 * d_fill
    # Subtle specular highlight for metallic pins/plastic sheen
    spec = np.power(np.clip(n_vis[:, 2], 0.0, 1.0), 16.0) * 0.14

    shaded = np.clip(mesh.base_rgb * intensity[:, None] + spec[:, None] * 255.0, 0.0, 255.0).astype(np.uint8)

    # Orthographic-perspective blend (gentle perspective looks natural like Altium 3D)
    cam_dist = mesh.radius * 4.2
    scale = (min(rw, rh) * 0.42 * zoom) / mesh.radius

    def _proj(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        persp = cam_dist / np.maximum(cam_dist - pts[:, 2], mesh.radius * 0.5)
        sx = rw * 0.5 + pts[:, 0] * scale * persp
        sy = rh * 0.5 - pts[:, 1] * scale * persp
        return sx, sy

    x0, y0 = _proj(p0)
    x1, y1 = _proj(p1)
    x2, y2 = _proj(p2)

    # Painter's algorithm sort: back-to-front by average Z
    z_avg = (p0[:, 2] + p1[:, 2] + p2[:, 2]) * (1.0 / 3.0)
    order = np.argsort(z_avg)

    x0_o = x0[order].tolist()
    y0_o = y0[order].tolist()
    x1_o = x1[order].tolist()
    y1_o = y1[order].tolist()
    x2_o = x2[order].tolist()
    y2_o = y2[order].tolist()
    r_o = shaded[order, 0].tolist()
    g_o = shaded[order, 1].tolist()
    b_o = shaded[order, 2].tolist()

    img = Image.new("RGB", (rw, rh), bg_rgb)
    draw = ImageDraw.Draw(img)
    poly = draw.polygon
    for i in range(len(order)):
        poly(
            (x0_o[i], y0_o[i], x1_o[i], y1_o[i], x2_o[i], y2_o[i]),
            fill=(r_o[i], g_o[i], b_o[i]),
        )

    if supersample > 1:
        img = img.resize((w, h), Image.Resampling.LANCZOS)
    return img


def render_2d_footprint_preview(
    asset: PreviewAsset,
    width: int,
    height: int,
    *,
    zoom: float = 1.0,
) -> Image.Image:
    """Render Altium-style 2D PCB Footprint preview (dark canvas, red pads, yellow silkscreen)."""
    w = max(64, int(width))
    h = max(64, int(height))
    img = Image.new("RGB", (w, h), (28, 28, 30))
    draw = ImageDraw.Draw(img)

    xs: list[float] = []
    ys: list[float] = []
    for p in asset.pads_2d:
        r = max(p["w"], p["h"]) * 0.5
        xs.extend([p["x"] - r, p["x"] + r])
        ys.extend([p["y"] - r, p["y"] + r])
    for tr in asset.tracks_2d:
        xs.extend([tr["x1"], tr["x2"]])
        ys.extend([tr["y1"], tr["y2"]])
    for a in asset.arcs_2d:
        xs.extend([a["cx"] - a["r"], a["cx"] + a["r"]])
        ys.extend([a["cy"] - a["r"], a["cy"] + a["r"]])

    if not xs or not ys:
        draw.text((w // 2 - 42, h // 2 - 6), "No 2D Footprint", fill=(140, 140, 145))
        return img

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 20.0)
    span_y = max(max_y - min_y, 20.0)
    cx = (min_x + max_x) * 0.5
    cy = (min_y + max_y) * 0.5
    scale = min((w * 0.76) / span_x, (h * 0.76) / span_y) * zoom

    def to_px(mx: float, my: float) -> tuple[float, float]:
        return (w * 0.5 + (mx - cx) * scale, h * 0.5 - (my - cy) * scale)

    # Subtle crosshair origin
    ox, oy = to_px(0.0, 0.0)
    draw.line((ox - 8, oy, ox + 8, oy), fill=(70, 70, 76), width=1)
    draw.line((ox, oy - 8, ox, oy + 8), fill=(70, 70, 76), width=1)

    # Layer colors (Altium standard)
    layer_colors = {
        int(PcbLayer.TOP): (210, 55, 55),
        int(PcbLayer.BOTTOM): (55, 115, 220),
        int(PcbLayer.MULTI_LAYER): (185, 155, 70),
        int(PcbLayer.TOP_OVERLAY): (230, 205, 65),
        int(PcbLayer.BOTTOM_OVERLAY): (110, 190, 90),
        int(PcbLayer.MECHANICAL_13): (180, 95, 190),
        int(PcbLayer.MECHANICAL_15): (180, 95, 190),
        int(PcbLayer.MECHANICAL_1): (160, 95, 180),
    }

    # Draw mechanical/silkscreen tracks
    for tr in asset.tracks_2d:
        col = layer_colors.get(tr["layer"], (165, 165, 170))
        pw = max(1, int(round(tr["w"] * scale)))
        p1 = to_px(tr["x1"], tr["y1"])
        p2 = to_px(tr["x2"], tr["y2"])
        draw.line((p1[0], p1[1], p2[0], p2[1]), fill=col, width=pw)

    # Draw arcs/circles
    for a in asset.arcs_2d:
        col = layer_colors.get(a["layer"], (230, 205, 65))
        r_px = a["r"] * scale
        px, py = to_px(a["cx"], a["cy"])
        bbox = [px - r_px, py - r_px, px + r_px, py + r_px]
        if abs(a["ea"] - a["sa"]) >= 359.0 or abs(a["ea"] - a["sa"]) < 0.1:
            draw.ellipse(bbox, outline=col, width=1)
        else:
            # PIL angles are CW from +X; Altium angles are CCW from +X
            draw.arc(bbox, start=-a["ea"], end=-a["sa"], fill=col, width=1)

    # Draw pads on top
    for p in asset.pads_2d:
        col = layer_colors.get(p["layer"], (210, 55, 55))
        if p["hole"] > 0.1:
            col = (190, 160, 75)
        px, py = to_px(p["x"], p["y"])
        hw = p["w"] * 0.5 * scale
        hh = p["h"] * 0.5 * scale
        if p["shape"] == 1:  # Circle / Oval
            draw.ellipse([px - hw, py - hh, px + hw, py + hh], fill=col)
        else:
            rad = math.radians(p["rot"])
            cr, sr = math.cos(rad), math.sin(rad)
            corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
            pts = [(px + dx * cr + dy * sr, py - dx * sr + dy * cr) for dx, dy in corners]
            draw.polygon(pts, fill=col)

        if p["hole"] > 0.1:
            hr = p["hole"] * 0.5 * scale
            draw.ellipse([px - hr, py - hr, px + hr, py + hr], fill=(28, 28, 30), outline=(110, 110, 115))

    return img
