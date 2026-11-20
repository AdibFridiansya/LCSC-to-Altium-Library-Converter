"""End-to-end verification test for LCSC -> Altium .IntLib pipeline."""

from pathlib import Path
import tempfile
from altium_monkey import AltiumIntLib, AltiumPcbLib, AltiumSchLib

from lcsc_api import fetch_component, search_lcsc_keyword
from altium_builder import build_intlib

OUT_DIR = Path(r"E:\LCSC-to-IntLib\output")


def verify_part(lcsc_id: str):
    print("=" * 64)
    print(f"Fetching {lcsc_id}...")
    bundle = fetch_component(lcsc_id, fetch_step=True, fetch_thumb=False)
    print(f"  Title: {bundle.meta.title} | MPN: {bundle.meta.mpn} | Pkg: {bundle.meta.package_name}")
    print(f"  3D UUID: {bundle.meta.has_3d_uuid} | STEP bytes: {len(bundle.step_bytes) if bundle.step_bytes else 0}")

    res = build_intlib(bundle, OUT_DIR, save_sources=False)
    print(f"  BUILT: {res.intlib_path} ({res.intlib_path.stat().st_size / 1024:.1f} KB)")
    print(f"  Symbol: {res.symbol_name} ({res.pin_count} pins) -> Footprint: {res.footprint_name} ({res.pad_count} pads)")
    if res.model_3d_metrics:
        m = res.model_3d_metrics
        print(f"  3D STEP Clean Bounds: {m.width_mm:.2f} x {m.depth_mm:.2f} x {m.height_mm:.2f} mm (Z: {m.min_z_mm:.2f}..{m.max_z_mm:.2f} mm)")
    if res.warnings:
        print(f"  Warnings: {res.warnings}")

    # Re-open and validate the compiled .IntLib!
    intlib = AltiumIntLib.from_file(res.intlib_path)
    comps = intlib.components
    assert len(comps) == 1, f"Expected 1 component in IntLib, got {len(comps)}"
    comp = comps[0]
    print(f"  VERIFIED IntLib component: {comp.name} | models={[m.name for m in comp.models]}")

    with tempfile.TemporaryDirectory() as td:
        extracted = intlib.extract_sources(td)
        sch_files = [s.output_path for s in extracted.sources if s.kind == "SchLib" and s.output_path]
        pcb_files = [s.output_path for s in extracted.sources if s.kind == "PCBLib" and s.output_path]
        assert sch_files and pcb_files
        sch = AltiumSchLib(sch_files[0])
        pcb = AltiumPcbLib(pcb_files[0])
        assert len(sch.symbols) == 1
        assert len(pcb.footprints) == 1
        fp = pcb.footprints[0]
        models = pcb.embedded_model_summaries()
        print(
            f"  VERIFIED extracted SchLib ({len(sch.symbols[0].pins)} pins) & "
            f"PcbLib ({len(fp.pads)} pads, {len(fp.component_bodies)} 3D bodies, {len(models)} embedded STEP)"
        )
        assert len(fp.component_bodies) == (1 if res.has_3d_step else 0)
        assert len(models) == (1 if res.has_3d_step else 0)


if __name__ == "__main__":
    # Test keyword search too (like a transformer!)
    print("Testing keyword search for 'EE16'...")
    hits = search_lcsc_keyword("EE16", limit=5)
    for h in hits:
        print(f"  HIT: {h.lcsc_id} | {h.title} | {h.package}")

    test_ids = ["C2040", "C165948", "C20917", "C49451311"]

    for cid in test_ids:
        verify_part(cid)
    print("\nALL PIPELINE TESTS PASSED!")
