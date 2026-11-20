# LCSC Part Importer & Integrator for Altium Designer

A lightweight Windows desktop utility that converts **LCSC / EasyEDA** components into native **Altium Designer `.IntLib`** libraries — complete with **clean 3D STEP models (no PCB slab)**, **Altium Native schematic styling**, **ActiveBOM supplier links**, an **interactive 3D STEP / 2D Footprint previewer**, and a built-in **`.IntLib` → `.LibPkg` + `.IntLib` Integrator**.

---

## Key Features

### 1. Direct LCSC / EasyEDA → Native `.IntLib`
- **Auto-Clipboard Detection:** Copy any LCSC Part # (e.g. `C465406`) or LCSC product URL in your browser and the app automatically fetches the component, datasheet, thumbnail, and 3D STEP status.
- **Keyword & MPN Search:** Search parts directly by MPN or keyword (e.g. `HLK-20M05`, `AMS1117`, `EE16`).
- **Clean 3D STEP Models (No PCB Slab):** Downloads raw STEP assets directly from EasyEDA's 3D endpoint and aligns XY/Z placement automatically using `wn-geometer` geometry bounds.
- **Altium Native Schematic Styling:** Automatically formats schematic symbols to match Altium Designer conventions — IC bodies (`#FFFFB0` fill / `#800000` border), discrete/passive components (`#0000FF`), `200–300 mil` pin lengths, `100 mil` grid snapping, and clean Designator/Comment styling (`#000080`).
- **ActiveBOM & Supplier Links:** Populates `Supplier 1` (`LCSC`), `Supplier Part Number 1`, `Manufacturer 1`, `Manufacturer Part Number 1`, `Supplier Currency 1`, `Supplier Unit Price 1`, `Supplier Stock 1`, and `ComponentLink1/2` (Datasheet PDF & LCSC product page).
- **Optional Source Export:** Optionally save separate `.SchLib`, `.PcbLib`, and `.STEP` files alongside `.IntLib`, plus quick `3D Rot Z` (`0° / 90° / 180° / 270°`) adjustment.

### 2. Interactive 3D STEP / 2D Footprint Preview
- Toggle **Show** in the **Recent Exports** panel to open a collapsible Altium-style **Footprint Preview** pane:
  - **3D mode:** Orbit, zoom, and inspect the embedded STEP model (`Left-drag` to orbit, `Scroll` to zoom, `Reset`).
  - **2D mode:** Inspect pads, silkscreen, and pin-1 markers.

### 3. Library Integrator (`Integrate` Tab)
- Select multiple single-component `.IntLib` files, choose a custom output library name and folder, and merge them into:
  - `<CustomName>.LibPkg` + `.SchLib` + `.PcbLib`
  - Compiled `<CustomName>.IntLib`
- Automatically sanitizes non-CP1252 characters and invalid Altium parameter delimiters (`|`, `¦`) during merging.

---

## Quick Start (Windows)

### Prerequisites
- **Python 3.11+** (tested on Python 3.13)

### Setup
```powershell
git clone <your-repo-url>
cd LCSC-to-IntLib
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Run
- **GUI (no console window):** Double-click `LCSC_to_IntLib.vbs` (or `LCSC Part Importer.lnk`)
- **With debug console:** Double-click `Run_Debug.bat` or run:
  ```powershell
  .venv\Scripts\python.exe app.py
  ```

---

## Project Structure

| File | Purpose |
| --- | --- |
| `app.py` | Altium Dark Gray GUI (`Search` & `Integrate` tabs, clipboard monitor, 3D/2D split preview) |
| `lcsc_api.py` | EasyEDA & LCSC API client (part metadata, search, datasheet, raw 3D STEP) |
| `easyeda_parser.py` | EasyEDA Standard symbol & footprint parser (SVG paths, pads, slots, 3D alignment) |
| `altium_builder.py` | Native `.SchLib`, `.PcbLib`, and `.IntLib` builder via `altium-monkey` |
| `viewer3d.py` | Fast `wn-geometer` + `numpy`/Pillow 3D STEP & 2D footprint renderer |
| `integrator.py` | Multi-`.IntLib` extractor, sanitizer, and `.LibPkg` + `.IntLib` compiler |
| `requirements.txt` | Python dependencies (`altium-monkey`, `requests`, `customtkinter`, `pillow`, `numpy`) |
