"""Merge multiple Altium .IntLib files into a single custom .LibPkg + .IntLib."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from altium_monkey import AltiumIntLib, AltiumPcbLib, AltiumSchLib


@dataclass
class IntLibFileInfo:
    path: Path
    size_kb: float
    component_names: list[str]
    error: str | None = None

    @property
    def component_count(self) -> int:
        return len(self.component_names)


@dataclass
class IntegrateResult:
    libpkg_path: Path
    intlib_path: Path
    schlib_path: Path
    pcblib_path: Path
    component_count: int
    footprint_count: int
    model_3d_count: int
    source_file_count: int
    warnings: list[str] = field(default_factory=list)


ProgressCallback = Callable[[float, str], None]

_RESERVED_PARAM_KEYS = frozenset(
    {
        "component kind",
        "component type",
        "description",
        "designator",
        "footprint",
        "footprint doc",
        "library reference",
        "pad count",
        "pin count",
    }
)


def sanitize_library_name(name: str, fallback: str = "Integrated_Library") -> str:
    """Clean custom output library name preserving clean spaces and hyphens."""
    cleaned = re.sub(r'[\\/:*?"<>|!]+', " ", (name or "").strip())
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:90].strip(" .") or fallback


def _clean_cp1252(text: str) -> str:
    """Make text safe for Altium IntLib metadata (CP1252, no '|' or Altium escaped '¦', no newlines)."""
    s = (
        str(text or "")
        .replace("|", "/")
        .replace("\u00a6", "/")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\t", " ")
    )
    out = []
    for ch in s:
        if ord(ch) < 0x20:
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            pass
    cleaned = "".join(out)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def inspect_intlib(path: Path | str) -> IntLibFileInfo:
    """Inspect an .IntLib file to preview its component count and names."""
    p = Path(path)
    size_kb = p.stat().st_size / 1024.0 if p.exists() else 0.0
    try:
        il = AltiumIntLib.from_file(p)
        names = [c.name for c in il.components]
        il.close()
        return IntLibFileInfo(path=p, size_kb=size_kb, component_names=names)
    except Exception as exc:
        return IntLibFileInfo(path=p, size_kb=size_kb, component_names=[], error=str(exc))


def _write_libpkg(
    libpkg_path: Path,
    schlib_filename: str,
    pcblib_filename: str,
    output_subpath: str = "Project Outputs",
) -> Path:
    """Write a native Altium Designer .LibPkg project file."""
    lines = [
        "[Design]",
        "Version=1.0",
        "HierarchyMode=0",
        f"OutputPath={output_subpath}",
        "",
        "[Document1]",
        f"DocumentPath={schlib_filename}",
        "DocumentUniqueId=",
        "",
        "[Document2]",
        f"DocumentPath={pcblib_filename}",
        "DocumentUniqueId=",
        "",
    ]
    with open(libpkg_path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return libpkg_path


def _sanitize_raw_record_value(rec: dict, key: str) -> None:
    """Clean a string field inside an Altium raw record dict (case-insensitive)."""
    for k in list(rec.keys()):
        if k.casefold() == key.casefold() or k.casefold() == f"%utf8%{key.casefold()}":
            if isinstance(rec[k], str):
                rec[k] = _clean_cp1252(rec[k])


def _sanitize_schlib_for_intlib(schlib: AltiumSchLib) -> None:
    """Ensure merged third-party symbols comply with Altium IntLib metadata constraints."""
    from altium_monkey import AltiumSchDesignator

    # Clean FileHeader component descriptions
    if isinstance(getattr(schlib, "file_header", None), dict):
        for k in list(schlib.file_header.keys()):
            if "compdescr" in k.casefold() and isinstance(schlib.file_header[k], str):
                schlib.file_header[k] = _clean_cp1252(schlib.file_header[k])

    for sym in schlib.symbols:
        clean_desc = _clean_cp1252(sym.description)
        sym.description = clean_desc

        # 1. Clean OOP component_record if present
        if isinstance(sym.component_record, dict):
            _sanitize_raw_record_value(sym.component_record, "ComponentDescription")

        # 2. Clean raw_records directly (AltiumSchLib.save serializes raw_records when populated!)
        if sym.raw_records:
            seen_param_names: set[str] = set()
            has_value_rec = any(
                str(r.get("RECORD", "")) == "41"
                and str(r.get("Name", "")).casefold() == "value"
                for r in sym.raw_records
                if isinstance(r, dict)
            )
            cleaned_records: list[dict] = []
            extra_value_records: list[dict] = []

            for rec in sym.raw_records:
                if not isinstance(rec, dict):
                    cleaned_records.append(rec)
                    continue
                rec_type = str(rec.get("RECORD", ""))
                if rec_type == "1":
                    _sanitize_raw_record_value(rec, "ComponentDescription")
                    cleaned_records.append(rec)
                    continue
                if rec_type == "41":
                    pname = _clean_cp1252(str(rec.get("Name", "")))
                    folded = pname.casefold()
                    ptext = _clean_cp1252(str(rec.get("Text", "")))
                    if not pname or folded in _RESERVED_PARAM_KEYS or folded in seen_param_names:
                        continue
                    if folded == "comment":
                        if ptext != "=Value":
                            if not has_value_rec and ptext:
                                val_rec = dict(rec)
                                val_rec["Name"] = "Value"
                                val_rec["Text"] = ptext
                                val_rec["IsHidden"] = "T"
                                val_rec.pop("%UTF8%Text", None)
                                extra_value_records.append(val_rec)
                                has_value_rec = True
                                seen_param_names.add("value")
                            rec["Text"] = "=Value"
                            rec.pop("%UTF8%Text", None)
                        seen_param_names.add(folded)
                        cleaned_records.append(rec)
                        continue
                    rec["Name"] = pname
                    rec["Text"] = ptext
                    rec.pop("%UTF8%Name", None)
                    rec.pop("%UTF8%Text", None)
                    seen_param_names.add(folded)
                    cleaned_records.append(rec)
                    continue
                cleaned_records.append(rec)

            if extra_value_records:
                # Insert before ImplementationList (RECORD=44) if present
                insert_idx = next(
                    (i for i, r in enumerate(cleaned_records) if isinstance(r, dict) and str(r.get("RECORD", "")) == "44"),
                    len(cleaned_records),
                )
                for vr in extra_value_records:
                    cleaned_records.insert(insert_idx, vr)
                    insert_idx += 1

            sym.raw_records = cleaned_records

        # 3. Also sync OOP parameters list so in-memory inspection matches
        seen_oop: set[str] = set()
        for param in list(sym.parameters):
            if isinstance(param, AltiumSchDesignator):
                continue
            pname = _clean_cp1252(str(param.name or ""))
            folded = pname.casefold()
            if not pname or folded in _RESERVED_PARAM_KEYS or folded in seen_oop:
                try:
                    sym.objects.remove(param)
                except Exception:
                    pass
                continue
            if folded == "comment":
                param.text = "=Value"
            else:
                param.name = pname
                param.text = _clean_cp1252(param.text)
            seen_oop.add(folded)


def _sanitize_pcblib_for_intlib(pcblib: AltiumPcbLib) -> None:
    """Clean footprint descriptions in merged PcbLib so no '|' or non-CP1252 breaks IntLib."""
    for fp in pcblib.footprints:
        if isinstance(fp.parameters, dict) and "DESCRIPTION" in fp.parameters:
            fp.parameters["DESCRIPTION"] = _clean_cp1252(fp.parameters["DESCRIPTION"])


def integrate_intlibs(
    intlib_paths: Sequence[Path | str],
    output_dir: Path | str,
    custom_name: str,
    *,
    progress: ProgressCallback | None = None,
) -> IntegrateResult:
    """Extract, merge, package (.LibPkg), and compile (.IntLib) multiple .IntLib files."""
    def _emit(frac: float, msg: str) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, frac)), msg)

    paths = [Path(p) for p in intlib_paths if Path(p).is_file()]
    if not paths:
        raise ValueError("Select at least one valid .IntLib file.")

    lib_name = sanitize_library_name(custom_name)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    schlib_out = out_dir / f"{lib_name}.SchLib"
    pcblib_out = out_dir / f"{lib_name}.PcbLib"
    libpkg_out = out_dir / f"{lib_name}.LibPkg"
    intlib_out = out_dir / f"{lib_name}.IntLib"

    warnings: list[str] = []

    with tempfile.TemporaryDirectory(prefix="lcsc_integrate_") as tmp:
        tmp_root = Path(tmp)
        extracted_schlibs: list[Path] = []
        extracted_pcblibs: list[Path] = []

        total = len(paths)
        for idx, src_path in enumerate(paths):
            frac = 0.05 + 0.45 * (idx / max(1, total))
            _emit(frac, f"[1/4] Extracting ({idx + 1}/{total}): {src_path.name}")
            sub_dir = tmp_root / f"src_{idx:03d}"
            sub_dir.mkdir(parents=True, exist_ok=True)
            il = AltiumIntLib.from_file(src_path)
            try:
                res = il.extract_sources(
                    sub_dir,
                    overwrite=True,
                    use_original_filenames=False,
                    write_libpkg=False,
                )
            finally:
                il.close()

            src_schs = [s.output_path for s in res.sources if s.kind == "SchLib" and s.output_path]
            src_pcbs = [s.output_path for s in res.sources if s.kind == "PCBLib" and s.output_path]
            if not src_schs:
                warnings.append(f"{src_path.name}: no SchLib stream found, skipped.")
                continue
            extracted_schlibs.extend(src_schs)
            extracted_pcblibs.extend(src_pcbs)

        if not extracted_schlibs:
            raise RuntimeError("No schematic libraries could be extracted from the selected .IntLib files.")

        # 1. Merge all SchLib files into one unified <CustomName>.SchLib
        _emit(0.58, f"[2/4] Merging {len(extracted_schlibs)} SchLib source(s) -> {schlib_out.name}")
        if len(extracted_schlibs) == 1:
            merged_sch = AltiumSchLib(extracted_schlibs[0])
            _sanitize_schlib_for_intlib(merged_sch)
            merged_sch.save(schlib_out)
        else:
            merged_sch = AltiumSchLib.merge(
                extracted_schlibs,
                schlib_out,
                handle_conflicts="rename",
                verbose=False,
            )
            _sanitize_schlib_for_intlib(merged_sch)
            merged_sch.save(schlib_out)

        # 2. Combine all PcbLib files (including embedded 3D STEP models!) into <CustomName>.PcbLib
        _emit(0.75, f"[3/4] Combining {len(extracted_pcblibs)} PcbLib + 3D STEP -> {pcblib_out.name}")
        if extracted_pcblibs:
            merged_pcb = AltiumPcbLib.combine(extracted_pcblibs, verbose=False)
            _sanitize_pcblib_for_intlib(merged_pcb)
            merged_pcb.save(pcblib_out)
            fp_count = len(merged_pcb.footprints)
            model_count = len(merged_pcb.embedded_model_summaries())
        else:
            merged_pcb = AltiumPcbLib()
            merged_pcb.save(pcblib_out)
            fp_count = 0
            model_count = 0

        # 3. Write <CustomName>.LibPkg referencing the merged SchLib & PcbLib
        _emit(0.86, f"[4/4] Writing {libpkg_out.name} & compiling {intlib_out.name}...")
        _write_libpkg(libpkg_out, schlib_out.name, pcblib_out.name)

        # 4. Compile unified <CustomName>.IntLib
        build_res = AltiumIntLib.create_from_libraries(
            intlib_out,
            schlib_paths=[schlib_out],
            pcblib_paths=[pcblib_out] if fp_count > 0 else (),
            overwrite=True,
        )
        for w in build_res.warnings:
            warnings.append(f"{w.subject}: {w.message}")

    _emit(
        1.0,
        f"Done! {intlib_out.name} + {libpkg_out.name} "
        f"({build_res.component_count} components, {build_res.footprint_count} footprints, {model_count} 3D models)",
    )

    return IntegrateResult(
        libpkg_path=libpkg_out,
        intlib_path=intlib_out,
        schlib_path=schlib_out,
        pcblib_path=pcblib_out,
        component_count=build_res.component_count,
        footprint_count=build_res.footprint_count,
        model_3d_count=model_count,
        source_file_count=len(paths),
        warnings=warnings,
    )
