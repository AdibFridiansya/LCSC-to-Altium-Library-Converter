"""Client for fetching LCSC / EasyEDA component data and clean raw 3D STEP models."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://easyeda.com/",
}

LCSC_ID_REGEX = re.compile(r"(?:^|[^A-Za-z0-9])(C\d{4,10})(?:$|[^A-Za-z0-9])", re.IGNORECASE)
STEP_DOWNLOAD_BASE = "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y"


@dataclass
class ComponentMetadata:
    lcsc_id: str
    title: str               # Clean component display name, e.g. "HI-LINK HLK-20M05"
    mpn: str                 # e.g. "HLK-20M05"
    manufacturer: str        # e.g. "HI-LINK"
    package_name: str        # Footprint / package name
    description: str         # Detailed English description + LCSC Part #
    prefix: str
    datasheet_url: str
    lcsc_url: str
    thumb_url: str
    category: str = ""
    packaging: str = ""
    jlcpcb_class: str = ""
    unit_price_usd: float | None = None
    stock_qty: int | None = None
    has_3d_uuid: str | None = None
    model_3d_title: str = ""


@dataclass
class ComponentBundle:
    meta: ComponentMetadata
    raw_result: dict[str, Any]
    step_bytes: bytes | None = None
    step_filename: str = ""
    thumb_bytes: bytes | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SearchHit:
    lcsc_id: str
    title: str
    package: str
    manufacturer: str
    description: str


def extract_lcsc_id(text: str) -> str | None:
    """Extract an LCSC part code (e.g. C2040) from raw text or a product URL."""
    if not text:
        return None
    cleaned = text.strip()
    if re.fullmatch(r"C\d{4,10}", cleaned, re.IGNORECASE):
        return cleaned.upper()
    # Handle URLs like ..._C2040.html or ...-C2040
    m = re.search(r"[_\-/=](C\d{4,10})(?:\.html|[?&#/\s]|$)", cleaned, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = LCSC_ID_REGEX.search(cleaned)
    if m:
        return m.group(1).upper()
    return None


def _extract_3d_uuid_from_package(pkg_detail: dict[str, Any] | None) -> tuple[str | None, str]:
    """Find the raw 3D model UUID and title inside packageDetail SVGNODE shapes."""
    if not isinstance(pkg_detail, dict):
        return None, ""
    data_str = pkg_detail.get("dataStr")
    if isinstance(data_str, str):
        try:
            data_str = json.loads(data_str)
        except Exception:
            return None, ""
    if not isinstance(data_str, dict):
        return None, ""
    for shape in data_str.get("shape", []) or []:
        if not isinstance(shape, str) or not shape.startswith("SVGNODE~"):
            continue
        try:
            payload = json.loads(shape.split("~", 1)[1])
            attrs = payload.get("attrs", {})
            uuid_val = attrs.get("uuid") or attrs.get("c_uuid")
            if uuid_val:
                return str(uuid_val).strip(), str(attrs.get("title", "")).strip()
        except Exception:
            continue
    return None, ""


def _clean_text(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def _strip_cjk_parenthetical(text: str) -> str:
    """Remove CJK characters and any leftover empty parentheses, e.g. 'HI-LINK(海凌科)' -> 'HI-LINK'."""
    kept = []
    for ch in text:
        if ord(ch) < 0x20:
            continue
        try:
            ch.encode("cp1252")
            kept.append(ch)
        except UnicodeEncodeError:
            pass
    cleaned = "".join(kept)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\[\s*\]", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,;-")
    return cleaned


def _compose_display_name(lcsc_title: str, manufacturer: str, mpn: str, fallback: str) -> str:
    """Build clean component & filename title like 'HI-LINK HLK-20M05'."""
    t = _strip_cjk_parenthetical(lcsc_title)
    if t:
        return t
    mfr = _strip_cjk_parenthetical(manufacturer)
    part = _strip_cjk_parenthetical(mpn)
    if mfr and part:
        if part.casefold().startswith(mfr.casefold()):
            return part
        return f"{mfr} {part}"
    return part or mfr or fallback


def _fetch_lcsc_product_detail(session: requests.Session, lcsc_id: str) -> dict[str, Any]:
    """Query LCSC's public product detail API for rich English title, attributes, and datasheet."""
    url = f"https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={lcsc_id}"
    try:
        r = session.get(url, timeout=12)
        if r.status_code == 200:
            body = r.json()
            res = body.get("result")
            if isinstance(res, dict):
                return res
    except Exception:
        pass
    return {}


def fetch_component(lcsc_id: str, *, fetch_step: bool = True, fetch_thumb: bool = True) -> ComponentBundle:
    """Fetch component symbol/footprint JSON and clean raw 3D STEP model from EasyEDA/LCSC."""
    cid = extract_lcsc_id(lcsc_id)
    if not cid:
        raise ValueError(f"Kode LCSC tidak valid: '{lcsc_id}' (contoh: C2040)")

    session = requests.Session()
    session.headers.update(HEADERS)

    url = f"https://easyeda.com/api/products/{cid}/components?version=6.4.19.5"
    resp = session.get(url, timeout=25)
    resp.raise_for_status()
    data = resp.json()

    result = data.get("result")
    if not result or not isinstance(result, dict):
        raise LookupError(f"Komponen '{cid}' tidak ditemukan di database LCSC / EasyEDA.")

    # Also query LCSC official detail API for clean English Manufacturer, Title, Description & Photo
    lcsc_detail = _fetch_lcsc_product_detail(session, cid)

    sym_ds = result.get("dataStr")
    if isinstance(sym_ds, str):
        sym_ds = json.loads(sym_ds)
        result["dataStr"] = sym_ds
    sym_head = (sym_ds or {}).get("head", {}) if isinstance(sym_ds, dict) else {}
    c_para = sym_head.get("c_para", {}) if isinstance(sym_head, dict) else {}

    pkg_detail = result.get("packageDetail") or {}
    if isinstance(pkg_detail, dict) and isinstance(pkg_detail.get("dataStr"), str):
        try:
            pkg_detail["dataStr"] = json.loads(pkg_detail["dataStr"])
        except Exception:
            pass
    pkg_head = (
        (pkg_detail.get("dataStr") or {}).get("head", {})
        if isinstance(pkg_detail, dict) and isinstance(pkg_detail.get("dataStr"), dict)
        else {}
    )
    pkg_para = pkg_head.get("c_para", {}) if isinstance(pkg_head, dict) else {}

    lcsc_info = result.get("lcsc") or result.get("szlcsc") or {}
    if not isinstance(lcsc_info, dict):
        lcsc_info = {}

    easyeda_title = _clean_text(result.get("title") or c_para.get("name") or cid)
    mpn = _clean_text(
        lcsc_detail.get("productModel")
        or c_para.get("Manufacturer Part")
        or c_para.get("name")
        or easyeda_title
    )
    manufacturer = _clean_text(
        lcsc_detail.get("brandNameEn")
        or _strip_cjk_parenthetical(_clean_text(c_para.get("Manufacturer") or ""))
        or c_para.get("Manufacturer")
        or ""
    )
    title = _compose_display_name(
        _clean_text(lcsc_detail.get("title") or ""),
        manufacturer,
        mpn,
        fallback=easyeda_title or cid,
    )

    package_name = _clean_text(
        pkg_detail.get("title")
        or c_para.get("package")
        or pkg_para.get("package")
        or lcsc_detail.get("encapStandard")
        or f"{cid}_FP"
    )
    packaging = _clean_text(lcsc_detail.get("encapStandard") or "")
    category = _clean_text(
        lcsc_detail.get("wmCatalogNameEn")
        or lcsc_detail.get("catalogName")
        or ""
    )

    # Compose detailed English description + LCSC Part # for Schematic description
    desc_parts: list[str] = []
    intro_en = _clean_text(lcsc_detail.get("productIntroEn") or "")
    key_attrs = _clean_text(
        lcsc_detail.get("productKeyAttributes")
        or lcsc_detail.get("productNameEn")
        or ""
    )
    easyeda_desc = _strip_cjk_parenthetical(_clean_text(result.get("description") or ""))
    if intro_en:
        desc_parts.append(intro_en)
    if key_attrs and key_attrs.casefold() not in intro_en.casefold():
        desc_parts.append(key_attrs)
    if not desc_parts and easyeda_desc:
        desc_parts.append(easyeda_desc)
    if not desc_parts and category:
        desc_parts.append(category)
    base_desc = " - ".join(desc_parts)
    if base_desc:
        description = f"{base_desc} (LCSC: {cid})"
    else:
        description = f"{title} ({packaging or package_name}) (LCSC: {cid})"

    prefix = _clean_text(c_para.get("pre") or pkg_para.get("pre") or "U?")
    if not prefix.endswith("?"):
        prefix = prefix + "?"
    datasheet_url = _normalize_url(
        _clean_text(
            lcsc_detail.get("pdfUrl")
            or c_para.get("link")
            or pkg_para.get("link")
            or lcsc_info.get("pdf")
            or ""
        )
    )
    lcsc_url = _normalize_url(
        _clean_text(lcsc_info.get("url") or f"https://www.lcsc.com/product-detail/{cid}.html")
    )
    lcsc_images = lcsc_detail.get("productImages")
    primary_lcsc_img = (
        _clean_text(lcsc_images[0])
        if isinstance(lcsc_images, list) and lcsc_images
        else ""
    )
    thumb_url = _normalize_url(primary_lcsc_img or _clean_text(result.get("thumb") or ""))
    jlcpcb_class = _clean_text(c_para.get("JLCPCB Part Class") or "")

    # Extract live LCSC unit price (USD) and stock quantity
    unit_price_usd: float | None = None
    price_list = lcsc_detail.get("productPriceList")
    if isinstance(price_list, list) and price_list:
        first_tier = price_list[0]
        if isinstance(first_tier, dict):
            raw_price = first_tier.get("usdPrice") or first_tier.get("productPrice")
            try:
                if raw_price is not None:
                    unit_price_usd = float(raw_price)
            except (TypeError, ValueError):
                pass
    if unit_price_usd is None and lcsc_info.get("price") is not None:
        try:
            unit_price_usd = float(lcsc_info["price"])
        except (TypeError, ValueError):
            pass

    stock_qty: int | None = None
    raw_stock = lcsc_detail.get("stockNumber")
    if raw_stock is None:
        raw_stock = lcsc_info.get("stock")
    if raw_stock is not None:
        try:
            stock_qty = int(raw_stock)
        except (TypeError, ValueError):
            pass

    uuid_3d, model_title = _extract_3d_uuid_from_package(pkg_detail)

    meta = ComponentMetadata(
        lcsc_id=cid,
        title=title,
        mpn=mpn,
        manufacturer=manufacturer,
        package_name=package_name,
        description=description,
        prefix=prefix,
        datasheet_url=datasheet_url,
        lcsc_url=lcsc_url,
        thumb_url=thumb_url,
        category=category,
        packaging=packaging,
        jlcpcb_class=jlcpcb_class,
        unit_price_usd=unit_price_usd,
        stock_qty=stock_qty,
        has_3d_uuid=uuid_3d,
        model_3d_title=model_title or package_name,
    )

    bundle = ComponentBundle(meta=meta, raw_result=result)

    # Fetch clean raw STEP directly from EasyEDA's component 3D asset server (NO PCB board!)
    if fetch_step and uuid_3d:
        step_url = f"{STEP_DOWNLOAD_BASE}/{uuid_3d}"
        try:
            sr = session.get(step_url, timeout=30)
            if sr.status_code == 200 and len(sr.content) > 64:
                header_probe = sr.content[:256]
                if b"ISO-10303-21" in header_probe:
                    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", meta.model_3d_title or cid).strip("_")
                    bundle.step_bytes = sr.content
                    bundle.step_filename = f"{safe_stem or cid}.step"
                else:
                    bundle.warnings.append(
                        "Server EasyEDA tidak menyediakan file STEP valid untuk UUID 3D ini."
                    )
            else:
                bundle.warnings.append(
                    f"Model 3D STEP belum tersedia di server (HTTP {sr.status_code})."
                )
        except Exception as exc:
            bundle.warnings.append(f"Gagal mengunduh model 3D STEP: {exc}")

    # Fetch product thumbnail for UI preview
    if fetch_thumb and thumb_url:
        try:
            tr = session.get(thumb_url, timeout=10)
            if tr.status_code == 200 and len(tr.content) > 100:
                bundle.thumb_bytes = tr.content
        except Exception:
            pass

    return bundle


def search_lcsc_keyword(keyword: str, limit: int = 12) -> list[SearchHit]:
    """Search EasyEDA/LCSC component catalog by MPN or keyword."""
    query = keyword.strip()
    if not query:
        return []
    url = "https://easyeda.com/api/components/search"
    payload = {
        "type": 3,
        "doctype[]": 2,
        "wd": query,
        "returnListStyle": "classifyarr",
        "version": "6.4.19.5",
    }
    resp = requests.post(url, data=payload, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    res = data.get("result") or {}
    lists = res.get("lists") or {}
    candidates: list[dict[str, Any]] = []
    if isinstance(lists, dict):
        for bucket in ("lcsc", "system", "easyeda"):
            items = lists.get(bucket)
            if isinstance(items, list):
                candidates.extend(items)
    elif isinstance(lists, list):
        candidates.extend(lists)

    hits: list[SearchHit] = []
    seen_ids: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        lcsc_obj = item.get("lcsc") or item.get("szlcsc") or {}
        cid = _clean_text(
            (lcsc_obj.get("number") if isinstance(lcsc_obj, dict) else "")
            or item.get("lcsc_id")
            or ""
        )
        if not cid:
            ds = item.get("dataStr")
            if isinstance(ds, dict):
                cp = (ds.get("head") or {}).get("c_para") or {}
                cid = _clean_text(cp.get("Supplier Part") or "")
        cid = extract_lcsc_id(cid) or ""
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        ds = item.get("dataStr") if isinstance(item.get("dataStr"), dict) else {}
        cp = (ds.get("head") or {}).get("c_para") or {}
        hits.append(
            SearchHit(
                lcsc_id=cid,
                title=_clean_text(item.get("title") or cid),
                package=_clean_text(cp.get("package") or ""),
                manufacturer=_clean_text(cp.get("Manufacturer") or ""),
                description=_clean_text(item.get("description") or ""),
            )
        )
        if len(hits) >= limit:
            break
    return hits
