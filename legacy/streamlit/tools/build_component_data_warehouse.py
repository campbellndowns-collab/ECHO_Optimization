#!/usr/bin/env python
"""
PyThrust Component Data Warehouse Builder V3
=========================================

Builds a broad, provenance-preserving component warehouse for UAV propulsion
optimization. Intended to be run from the PyThrust repository root.

The builder NEVER copies or scrapes eCalc. It uses:
  * the user's local PyThrust motor/prop data;
  * the user's normalized APC physical/price catalog;
  * a local verified-cell seed catalog;
  * optional public/reference sources downloaded locally.

Outputs are layered:
  Tier A  manufacturer / local physics-ready data
  Tier B  reputable current catalog/test data, near complete
  Tier C  legacy/reference/discovery data; not optimizer-ready by default

Important:
  Public availability does not automatically establish redistribution rights.
  Sources with unclear licenses are retained with license_status='unclear' and
  are marked optimizer_eligible=0 unless the record is independently verified.
  This warehouse is designed for local research/design use with provenance.

No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime

TODAY = date.today().isoformat()
USER_AGENT = "Mozilla/5.0 PyThrust-Component-Warehouse/3.1"

# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

SOURCES = [
    {
        "source_name": "PyThrust local motor database",
        "category": "motors",
        "url": "https://github.com/Setuav/PyThrust",
        "license": "Apache-2.0 (software repository; preserve component provenance)",
        "license_status": "known",
        "quality": "A",
        "enabled_default": True,
        "notes": "Local dataset already used by PyThrust."
    },
    {
        "source_name": "PyThrust APC aerodynamic database",
        "category": "props",
        "url": "https://github.com/Setuav/PyThrust",
        "license": "Apache-2.0 (software repository; APC performance provenance retained)",
        "license_status": "known",
        "quality": "A",
        "enabled_default": True,
        "notes": "Ct/Cp data keyed by APC model."
    },
    {
        "source_name": "APC PROP-DATA-FILE_202602 normalized catalog",
        "category": "props,prices",
        "url": "https://www.apcprop.com/technical-information/file-downloads/",
        "license": "Manufacturer-published technical catalog; redistribution terms not asserted",
        "license_status": "unclear",
        "quality": "A",
        "enabled_default": True,
        "notes": "User-supplied workbook normalized to CSV; real mass, SKU, status, price."
    },
    {
        "source_name": "Verified UAV cell seed",
        "category": "battery_cells",
        "url": "manufacturer datasheets listed per record",
        "license": "Per-manufacturer datasheet terms",
        "license_status": "mixed",
        "quality": "A/B",
        "enabled_default": True,
        "notes": "Small high-quality seed; broadened by reference sources below."
    },
    {
        "source_name": "TUM/BetterBat Battery Cell Database",
        "category": "battery_cells",
        "url": "https://github.com/TUMFTM/TechnoEconomicCellSelection/blob/main/inputs/CellDatabase_v6.xlsx",
        "license": "CC BY 4.0 dataset lineage via Zenodo record 10679242",
        "license_status": "known",
        "quality": "B",
        "enabled_default": True,
        "notes": "Datasheet-derived database; published work describes 160+ cells and current source commentary reports hundreds of devices."
    },
    {
        "source_name": "CellDB public catalog",
        "category": "battery_cells",
        "url": "https://www.mewyeahcloud.com/en",
        "license": "Public catalog; redistribution license not established",
        "license_status": "unclear",
        "quality": "C",
        "enabled_default": True,
        "notes": "203 cell models at research date. Discovery/reference only; manufacturer verification required."
    },
    {
        "source_name": "SplineCloud T-MOTOR ESC datasets",
        "category": "escs",
        "url": "https://splinecloud.com/repository/Serhii.K/T-MOTOR_ESCs/",
        "license": "SC-Legacy (SplineCloud Public Access License)",
        "license_status": "known",
        "quality": "B",
        "enabled_default": True,
        "notes": "T-MOTOR FPV, fixed-wing, and multirotor ESC specification spreadsheets."
    },
    {
        "source_name": "Tyto Robotics ESC database",
        "category": "escs",
        "url": "https://database.tytorobotics.com/escs",
        "license": "Tyto Robotics database terms apply",
        "license_status": "unclear",
        "quality": "B/C",
        "enabled_default": True,
        "notes": "55 ESCs in public database at research date; static test/reference metadata."
    },
    {
        "source_name": "USU PropulsionOptimization legacy SQL database",
        "category": "motors,props,battery_packs,escs",
        "url": "https://github.com/usuaero/PropulsionOptimization",
        "license": "No explicit repository LICENSE observed",
        "license_status": "unclear",
        "quality": "C",
        "enabled_default": True,
        "notes": "Archived 2020; README claims >600 props, >5000 motors, >500 batteries, >500 ESCs. Reference/discovery only unless reverified."
    },
    {
        "source_name": "LYGTE battery test index",
        "category": "battery_cells",
        "url": "https://lygte-info.dk/info/batteryIndex.html",
        "license": "Site terms not established by this builder",
        "license_status": "unclear",
        "quality": "C",
        "enabled_default": True,
        "notes": "Broad tested-cell discovery/reference source; incomplete for mass/current limits."
    },
    {
        "source_name": "18650 Lithium Ion Battery Identification Reference",
        "category": "battery_cells",
        "url": "https://docs.google.com/spreadsheets/d/1fYjDxxCJXfm2wdpGWCaOUGq8V8TOEgsnplHQa4YQpRQ/edit",
        "license": "Public reference sheet; explicit redistribution license not established",
        "license_status": "unclear",
        "quality": "C",
        "enabled_default": True,
        "notes": "Legacy/current-reference fields: brand/model/capacity/max discharge/chemistry/datasheet."
    },
    {
        "source_name": "Tattu battery finder",
        "category": "battery_packs",
        "url": "https://www.tattuworld.com/battery-search/",
        "license": "Manufacturer-published catalog; redistribution terms not asserted",
        "license_status": "unclear",
        "quality": "B",
        "enabled_default": True,
        "notes": "Current manufacturer pack specs; price may be absent."
    },
    {
        "source_name": "FPVCompare battery table",
        "category": "battery_packs,prices",
        "url": "https://fpvcompare.com/batteries/",
        "license": "Site terms not established by this builder",
        "license_status": "unclear",
        "quality": "C",
        "enabled_default": True,
        "notes": "Current comparison/reference source; use for discovery/cross-checking, not as sole engineering authority."
    },
]

MOTOR_FIELDS = [
    "record_id","canonical_id","manufacturer","model","kv_rpm_per_v",
    "resistance_ohm","no_load_current_a","no_load_voltage_v","max_current_a",
    "max_power_w","mass_g","min_cells","max_cells","diameter_mm","length_mm",
    "shaft_mm","price_usd","source_name","source_url","source_record_id",
    "source_license","license_status","data_quality_tier","optimizer_eligible",
    "checked_date","notes"
]

PROP_FIELDS = [
    "record_id","canonical_id","manufacturer","model","diameter_in","pitch_in",
    "blade_count","mass_g","max_rpm","price_usd","status","aero_data_available",
    "aero_id","rpm_min","rpm_max","source_name","source_url","source_record_id",
    "source_license","license_status","data_quality_tier","optimizer_eligible",
    "checked_date","notes"
]

CELL_FIELDS = [
    "record_id","canonical_id","manufacturer","model","format","chemistry",
    "nominal_voltage_v","capacity_ah","energy_wh","charge_voltage_v",
    "cutoff_voltage_v","max_cont_discharge_a","max_burst_discharge_a",
    "mass_g","diameter_mm","height_mm","width_mm","length_mm",
    "dcir_mohm","impedance_mohm","impedance_type","price_usd_each",
    "retail_status","datasheet_url","source_name","source_url","source_record_id",
    "source_license","license_status","data_quality_tier","optimizer_eligible",
    "checked_date","notes"
]

PACK_FIELDS = [
    "record_id","canonical_id","manufacturer","model","chemistry","series_cells",
    "parallel_cells","voltage_nominal_v","capacity_ah","energy_wh","c_rating_cont",
    "c_rating_burst","max_cont_current_a","max_burst_current_a","mass_g",
    "length_mm","width_mm","height_mm","connector","price_usd","status",
    "source_name","source_url","source_record_id","source_license",
    "license_status","data_quality_tier","optimizer_eligible","checked_date","notes"
]

ESC_FIELDS = [
    "record_id","canonical_id","manufacturer","model","continuous_current_a",
    "burst_current_a","min_cells","max_cells","voltage_max_v","mass_g",
    "resistance_ohm","efficiency","bec","dimensions","price_usd","status",
    "source_name","source_url","source_record_id","source_license",
    "license_status","data_quality_tier","optimizer_eligible","checked_date","notes"
]

PRICE_FIELDS = [
    "price_id","component_type","canonical_id","manufacturer","model","vendor",
    "price_usd","quantity","in_stock","source_url","checked_date",
    "source_name","license_status","notes"
]

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def norm_space(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()

def norm_key(value):
    s = norm_space(value).upper()
    s = s.replace("×", "X")
    s = re.sub(r"[^A-Z0-9.]+", "", s)
    return s

def canonical_id(kind, manufacturer, model):
    raw = f"{kind}|{norm_key(manufacturer)}|{norm_key(model)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

def record_id(source, source_id, kind="record"):
    raw = f"{kind}|{source}|{source_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

def optional_float(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s.lower() in {"none","nan","n/a","na","-","--"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None

def optional_int(v):
    x = optional_float(v)
    return int(round(x)) if x is not None else None

def truthy(v):
    return str(v).strip().lower() in {"1","true","yes","y","in stock","available"}

def first_value(row, names, default=None):
    normalized = {norm_key(k): v for k, v in row.items()}
    for n in names:
        k = norm_key(n)
        if k in normalized and normalized[k] not in (None, ""):
            return normalized[k]
    return default

def parse_cells(value):
    s = norm_space(value).upper()
    # 6S, 6S1P, 6 CELL, 6 CELLS
    m = re.search(r"(\d+)\s*S(?:\s*(\d+)\s*P)?", s)
    if m:
        return int(m.group(1)), int(m.group(2) or 1)
    m = re.search(r"(\d+)\s*CELL", s)
    if m:
        return int(m.group(1)), 1
    x = optional_int(s)
    return (x, 1) if x else (None, None)

def parse_dimensions(value):
    s = norm_space(value).lower().replace("×", "x")
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    vals = [float(x) for x in nums[:3]]
    while len(vals) < 3:
        vals.append(None)
    return tuple(vals[:3])

def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})

def read_csv(path):
    with Path(path).open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def fetch(url, cache_path=None, timeout=30, retries=2):
    if cache_path and Path(cache_path).exists():
        return Path(cache_path).read_bytes()

    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if cache_path:
                Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
                Path(cache_path).write_bytes(data)
            return data
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"Download failed: {url}: {last}")

class SimpleTableParser(HTMLParser):
    """Extract text from ordinary HTML tables without third-party packages."""
    def __init__(self):
        super().__init__()
        self.tables = []
        self._table_depth = 0
        self._row = None
        self._cell = None
        self._rows = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
        elif self._table_depth == 1 and tag == "tr":
            self._row = []
        elif self._table_depth == 1 and tag in ("td", "th"):
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._table_depth == 1 and tag in ("td", "th"):
            if self._row is not None and self._cell is not None:
                self._row.append(norm_space(html.unescape("".join(self._cell))))
            self._cell = None
        elif self._table_depth == 1 and tag == "tr":
            if self._rows is not None and self._row and any(self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table_depth:
            if self._table_depth == 1 and self._rows is not None:
                self.tables.append(self._rows)
                self._rows = None
            self._table_depth -= 1

def html_tables(data):
    text = data.decode("utf-8", errors="replace")
    p = SimpleTableParser()
    p.feed(text)
    return p.tables

def best_table(tables, required_tokens):
    required = [norm_key(x) for x in required_tokens]
    best = None
    best_score = -1
    for table in tables:
        if not table:
            continue
        head = " ".join(table[0])
        h = norm_key(head)
        score = sum(1 for token in required if token in h)
        if score > best_score:
            best, best_score = table, score
    return best if best_score > 0 else None

def table_to_dicts(table):
    if not table or len(table) < 2:
        return []
    headers = [norm_space(h) or f"col_{i}" for i, h in enumerate(table[0])]
    # disambiguate duplicate headers
    seen = {}
    unique = []
    for h in headers:
        seen[h] = seen.get(h, 0) + 1
        unique.append(h if seen[h] == 1 else f"{h}_{seen[h]}")
    out = []
    for row in table[1:]:
        row = list(row) + [""] * max(0, len(unique) - len(row))
        out.append(dict(zip(unique, row[:len(unique)])))
    return out

# ---------------------------------------------------------------------------
# Local PyThrust extraction
# ---------------------------------------------------------------------------

def extract_pythrust_motors(root):
    data_dir = root / "pythrust" / "data" / "motors"
    rows = []
    if not data_dir.exists():
        return rows

    # Directly parse the stable JSON schema. This avoids requiring import path
    # configuration and preserves all local records.
    for p in sorted(data_dir.glob("**/*.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        mid = norm_space(m.get("id"))
        if not mid:
            continue
        manufacturer = norm_space(m.get("manufacturer") or "Unknown")
        model = norm_space(m.get("name") or mid)
        kv = optional_float(m.get("kv"))
        r = optional_float(m.get("resistance"))
        io = optional_float(m.get("io"))
        imax = optional_float(m.get("max_current"))
        mass = optional_float(m.get("weight_g"))
        eligible = int(all(x is not None and x > 0 for x in (kv, r, imax, mass)))
        rows.append({
            "record_id": record_id("PyThrust", mid, "motor"),
            "canonical_id": canonical_id("motor", manufacturer, model),
            "manufacturer": manufacturer,
            "model": model,
            "kv_rpm_per_v": kv,
            "resistance_ohm": r,
            "no_load_current_a": io,
            "no_load_voltage_v": optional_float(m.get("io_voltage")),
            "max_current_a": imax,
            "max_power_w": optional_float(m.get("max_power")),
            "mass_g": mass,
            "source_name": "PyThrust local motor database",
            "source_url": "https://github.com/Setuav/PyThrust",
            "source_record_id": mid,
            "source_license": "Apache-2.0 repository; preserve record provenance",
            "license_status": "known",
            "data_quality_tier": "A" if eligible else "B",
            "optimizer_eligible": eligible,
            "checked_date": TODAY,
            "notes": f"Local file: {p.relative_to(root)}",
        })
    return rows

def extract_pythrust_props(root):
    base = root / "pythrust" / "data" / "propellers"
    rows = []
    if not base.exists():
        return rows

    for p in sorted(base.glob("**/*.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        required = ("id","manufacturer","model","diameter_in","pitch_in","blade_count","data_csv")
        if not all(k in m for k in required):
            continue

        aero_id = norm_space(m["id"])
        data_csv = p.parent / str(m["data_csv"])
        rpm_vals = []
        if data_csv.exists():
            try:
                with data_csv.open("r", newline="", encoding="utf-8-sig") as f:
                    for r in csv.DictReader(f):
                        rpm = optional_float(r.get("rpm"))
                        if rpm and math.isfinite(rpm):
                            rpm_vals.append(rpm)
            except Exception:
                pass

        manufacturer = norm_space(m["manufacturer"])
        model = norm_space(m["model"])
        rows.append({
            "record_id": record_id("PyThrust", aero_id, "prop"),
            "canonical_id": canonical_id("prop", manufacturer, model),
            "manufacturer": manufacturer,
            "model": model,
            "diameter_in": optional_float(m["diameter_in"]),
            "pitch_in": optional_float(m["pitch_in"]),
            "blade_count": optional_int(m["blade_count"]),
            "aero_data_available": int(bool(rpm_vals) or data_csv.exists()),
            "aero_id": aero_id,
            "rpm_min": min(rpm_vals) if rpm_vals else None,
            "rpm_max": max(rpm_vals) if rpm_vals else None,
            "source_name": "PyThrust APC aerodynamic database",
            "source_url": "https://github.com/Setuav/PyThrust",
            "source_record_id": aero_id,
            "source_license": "Apache-2.0 repository; APC provenance retained",
            "license_status": "known",
            "data_quality_tier": "A",
            "optimizer_eligible": 1,
            "checked_date": TODAY,
            "notes": f"Metadata file: {p.relative_to(root)}",
        })
    return rows

def find_local(root, filename):
    candidates = [
        root / filename,
        root / "pythrust" / "data" / "propellers" / filename,
        root / "pythrust" / "data" / "batteries" / filename,
        root / "component_data_seed" / filename,
        root / "data_seed" / filename,
    ]
    return next((p for p in candidates if p.exists()), None)

def enrich_props_with_apc(root, prop_rows, price_rows):
    path = find_local(root, "apc_catalog_202602.csv")
    if not path:
        return prop_rows, []

    catalog = read_csv(path)
    apc_by_model = {}
    for r in catalog:
        model = norm_space(r.get("product_name"))
        if not model:
            continue
        apc_by_model.setdefault(norm_key(model), []).append(r)

    matched = set()
    for p in prop_rows:
        if norm_key(p.get("manufacturer")) != "APC":
            continue
        keys = [norm_key(p.get("model")), norm_key(p.get("aero_id")).replace("APC", "", 1)]
        candidates = []
        for k in keys:
            candidates.extend(apc_by_model.get(k, []))
        # fall back to dimensions if exact model differs
        if not candidates:
            for r in catalog:
                d = optional_float(r.get("diameter_in"))
                pitch = optional_float(r.get("pitch_in"))
                if (
                    d is not None and pitch is not None
                    and p.get("diameter_in") is not None and p.get("pitch_in") is not None
                    and abs(d - p["diameter_in"]) < 0.02
                    and abs(pitch - p["pitch_in"]) < 0.02
                ):
                    candidates.append(r)
        if not candidates:
            continue
        # Prefer in-stock and exact normalized name.
        candidates.sort(key=lambda r: (
            0 if norm_space(r.get("status")).lower() == "in stock" else 1,
            0 if norm_key(r.get("product_name")) in keys else 1,
        ))
        r = candidates[0]
        p["mass_g"] = optional_float(r.get("weight_g"))
        p["price_usd"] = optional_float(r.get("price_usd"))
        p["status"] = norm_space(r.get("status"))
        p["data_quality_tier"] = "A"
        p["optimizer_eligible"] = int(
            p.get("aero_data_available") == 1
            and p.get("mass_g") is not None
            and p.get("mass_g") > 0
        )
        p["notes"] = (p.get("notes") or "") + f"; APC SKU {norm_space(r.get('sku'))}; physical/price data from {path.name}"
        matched.add(id(r))
        if p.get("price_usd") is not None:
            price_rows.append({
                "price_id": record_id("APC", f"{p['canonical_id']}|{p['price_usd']}", "price"),
                "component_type": "prop",
                "canonical_id": p["canonical_id"],
                "manufacturer": "APC",
                "model": p["model"],
                "vendor": "APC",
                "price_usd": p["price_usd"],
                "quantity": 1,
                "in_stock": int(norm_space(p.get("status")).lower() == "in stock"),
                "source_url": "https://www.apcprop.com/technical-information/file-downloads/",
                "checked_date": TODAY,
                "source_name": "APC PROP-DATA-FILE_202602 normalized catalog",
                "license_status": "unclear",
                "notes": f"Catalog snapshot from {path.name}",
            })

    # Add unmatched APC physical catalog items too, useful for breadth even when
    # no PyThrust aero data is available.
    extras = []
    for i, r in enumerate(catalog):
        manufacturer = "APC"
        model = norm_space(r.get("product_name"))
        if not model:
            continue
        cid = canonical_id("prop", manufacturer, model)
        if any(p["canonical_id"] == cid for p in prop_rows):
            continue
        mass = optional_float(r.get("weight_g"))
        d = optional_float(r.get("diameter_in"))
        pitch = optional_float(r.get("pitch_in"))
        price = optional_float(r.get("price_usd"))
        extras.append({
            "record_id": record_id("APC", norm_space(r.get("sku") or model), "prop"),
            "canonical_id": cid,
            "manufacturer": manufacturer,
            "model": model,
            "diameter_in": d,
            "pitch_in": pitch,
            "mass_g": mass,
            "price_usd": price,
            "status": norm_space(r.get("status")),
            "aero_data_available": 0,
            "source_name": "APC PROP-DATA-FILE_202602 normalized catalog",
            "source_url": "https://www.apcprop.com/technical-information/file-downloads/",
            "source_record_id": norm_space(r.get("sku") or model),
            "source_license": "Manufacturer-published catalog; redistribution terms not asserted",
            "license_status": "unclear",
            "data_quality_tier": "A",
            "optimizer_eligible": 0,  # requires aero pairing first
            "checked_date": TODAY,
            "notes": "Physical/price catalog record; no paired PyThrust aero entry yet.",
        })
        if price is not None:
            price_rows.append({
                "price_id": record_id("APC", f"{cid}|{price}", "price"),
                "component_type": "prop",
                "canonical_id": cid,
                "manufacturer": manufacturer,
                "model": model,
                "vendor": "APC",
                "price_usd": price,
                "quantity": 1,
                "in_stock": int(norm_space(r.get("status")).lower() == "in stock"),
                "source_url": "https://www.apcprop.com/technical-information/file-downloads/",
                "checked_date": TODAY,
                "source_name": "APC PROP-DATA-FILE_202602 normalized catalog",
                "license_status": "unclear",
                "notes": "Manufacturer catalog snapshot.",
            })
    prop_rows.extend(extras)
    return prop_rows, extras

def extract_seed_cells(root, price_rows):
    path = find_local(root, "uav_real_cell_catalog_v1.csv")
    rows = []
    if not path:
        return rows
    for r in read_csv(path):
        manufacturer = norm_space(r.get("manufacturer"))
        model = norm_space(r.get("model"))
        if not manufacturer or not model:
            continue
        mass = optional_float(r.get("weight_max_g"))
        capacity = optional_float(r.get("typical_capacity_ah"))
        vn = optional_float(r.get("nominal_voltage_v"))
        imax = optional_float(r.get("max_cont_discharge_a"))
        eligible = int(all(x is not None and x > 0 for x in (mass, capacity, vn, imax)))
        cid = canonical_id("cell", manufacturer, model)
        price = optional_float(r.get("price_usd_each"))
        row = {
            "record_id": record_id("VerifiedSeed", f"{manufacturer}|{model}", "cell"),
            "canonical_id": cid,
            "manufacturer": manufacturer,
            "model": model,
            "format": norm_space(r.get("format")),
            "chemistry": "Li-ion",
            "nominal_voltage_v": vn,
            "capacity_ah": capacity,
            "energy_wh": optional_float(r.get("typical_energy_wh")),
            "charge_voltage_v": optional_float(r.get("charge_voltage_v")),
            "cutoff_voltage_v": optional_float(r.get("cutoff_voltage_v")),
            "max_cont_discharge_a": imax,
            "mass_g": mass,
            "diameter_mm": optional_float(r.get("diameter_max_mm")),
            "height_mm": optional_float(r.get("height_max_mm")),
            "impedance_mohm": optional_float(r.get("impedance_mohm")),
            "impedance_type": norm_space(r.get("impedance_type")),
            "price_usd_each": price,
            "retail_status": norm_space(r.get("retail_status")),
            "datasheet_url": norm_space(r.get("manufacturer_source")),
            "source_name": "Verified UAV cell seed",
            "source_url": norm_space(r.get("manufacturer_source")),
            "source_record_id": f"{manufacturer}|{model}",
            "source_license": "Per-manufacturer datasheet terms",
            "license_status": "mixed",
            "data_quality_tier": "A" if eligible else "B",
            "optimizer_eligible": eligible,
            "checked_date": norm_space(r.get("checked_date")) or TODAY,
            "notes": norm_space(r.get("notes")),
        }
        rows.append(row)
        if price is not None:
            price_rows.append({
                "price_id": record_id("CellRetail", f"{cid}|{price}", "price"),
                "component_type": "battery_cell",
                "canonical_id": cid,
                "manufacturer": manufacturer,
                "model": model,
                "vendor": "Referenced retailer",
                "price_usd": price,
                "quantity": 1,
                "in_stock": None,
                "source_url": norm_space(r.get("price_source")),
                "checked_date": norm_space(r.get("checked_date")) or TODAY,
                "source_name": "Verified UAV cell seed",
                "license_status": "mixed",
                "notes": norm_space(r.get("retail_status")),
            })
    return rows


# ---------------------------------------------------------------------------
# Minimal XLSX reader (standard library only)
# ---------------------------------------------------------------------------


def xlsx_sheet_rows(path):
    """Return {sheet_name: rows} for all worksheets without third-party packages."""
    import xml.etree.ElementTree as ET

    ns = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "p": "http://schemas.openxmlformats.org/package/2006/relationships",
    }

    def col_index(ref):
        m = re.match(r"([A-Z]+)", ref or "")
        if not m:
            return 0
        n = 0
        for ch in m.group(1):
            n = n * 26 + ord(ch) - 64
        return n - 1

    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                text = "".join(
                    (t.text or "")
                    for t in si.iter(
                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
                    )
                )
                shared.append(text)

        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_map = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        output = {}

        for sheet_info in wb.find("m:sheets", ns):
            sheet_name = sheet_info.attrib.get("name", "Sheet")
            rid = sheet_info.attrib[
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            ]
            target = rel_map[rid]
            sheet_path = (
                "xl/" + target if not target.startswith("/") else target.lstrip("/")
            )

            sheet = ET.fromstring(z.read(sheet_path))
            rows = []

            for row in sheet.findall(".//m:sheetData/m:row", ns):
                values = {}
                max_idx = -1

                for c in row.findall("m:c", ns):
                    idx = col_index(c.attrib.get("r"))
                    max_idx = max(max_idx, idx)
                    ctype = c.attrib.get("t")
                    value = None

                    if ctype == "inlineStr":
                        is_node = c.find("m:is", ns)
                        if is_node is not None:
                            value = "".join(
                                (t.text or "")
                                for t in is_node.iter(
                                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"
                                )
                            )
                    else:
                        v = c.find("m:v", ns)
                        if v is not None:
                            raw = v.text
                            if ctype == "s":
                                try:
                                    value = shared[int(raw)]
                                except Exception:
                                    value = raw
                            elif ctype == "b":
                                value = "1" if raw == "1" else "0"
                            else:
                                value = raw

                    values[idx] = value

                if max_idx >= 0:
                    rows.append([values.get(i, "") for i in range(max_idx + 1)])

            output[sheet_name] = rows

        return output


def detect_header_window(rows, required_any, max_header_rows=4):
    """
    Detect a 1-4 row Excel header window.

    Many engineering workbooks use merged/multi-row headers such as:
        Cell data | Electrical data | Geometry
        Manufacturer | Cell name | Nominal capacity [Ah] | ...
    V3 assumed a single header row, which is why some valid workbooks produced
    zero records.
    """
    required = [norm_key(x) for x in required_any if norm_key(x)]
    best = None
    best_score = -1.0

    max_start = min(len(rows), 80)
    for i in range(max_start):
        for height in range(1, max_header_rows + 1):
            if i + height > len(rows):
                break
            window = rows[i : i + height]
            joined = " ".join(
                norm_space(cell)
                for row in window
                for cell in row
                if norm_space(cell)
            )
            key = norm_key(joined)
            token_score = sum(1 for token in required if token in key)
            nonempty = sum(
                1 for row in window for cell in row if norm_space(cell)
            )
            # Strongly favor required token coverage, lightly favor informative
            # headers, and slightly penalize unnecessarily tall windows.
            score = token_score * 100.0 + min(nonempty, 80) - 2.0 * (height - 1)
            if score > best_score:
                best = (i, height)
                best_score = score

    if best is None:
        return None
    # Require at least one core token.
    return best if best_score >= 100.0 else None


def rows_to_dicts(rows, required_any):
    header = detect_header_window(rows, required_any)
    if header is None:
        return []

    hidx, hheight = header
    header_rows = rows[hidx : hidx + hheight]
    width = max(len(r) for r in header_rows)

    # Build one normalized column name by concatenating the non-empty cells
    # vertically for each column.
    headers = []
    for col in range(width):
        parts = []
        for hr in header_rows:
            value = norm_space(hr[col] if col < len(hr) else "")
            if value and (not parts or value != parts[-1]):
                parts.append(value)
        headers.append(" | ".join(parts) if parts else f"col_{col}")

    counts, unique = {}, []
    for h in headers:
        counts[h] = counts.get(h, 0) + 1
        unique.append(h if counts[h] == 1 else f"{h}_{counts[h]}")

    out = []
    for row in rows[hidx + hheight :]:
        row = list(row) + [""] * max(0, len(unique) - len(row))
        d = dict(zip(unique, row[: len(unique)]))
        if any(norm_space(v) for v in d.values()):
            out.append(d)

    return out


def first_value_fuzzy(row, alternatives, default=None):
    """
    Find a value by exact normalized header first, then by substring/token match.
    This is intentionally used only for messy imported workbooks.
    """
    exact = {norm_key(k): v for k, v in row.items()}
    for alt in alternatives:
        k = norm_key(alt)
        if k in exact and exact[k] not in (None, ""):
            return exact[k]

    # Prefer the shortest matching header so a specific field beats a large
    # concatenated category label.
    candidates = []
    for header, value in row.items():
        if value in (None, ""):
            continue
        hk = norm_key(header)
        for alt in alternatives:
            ak = norm_key(alt)
            if ak and (ak in hk or hk in ak):
                candidates.append((len(hk), value))
                break

    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    return default


def xlsx_to_dicts_all_sheets(path, required_any=("Manufacturer", "Capacity", "Cell")):
    """Return rows from the worksheet(s) that look like component tables."""
    sheets = xlsx_sheet_rows(path)
    candidates = []

    for sheet_name, rows in sheets.items():
        records = rows_to_dicts(rows, required_any)
        if records:
            candidates.append((sheet_name, records))

    if not candidates:
        return []

    # Prefer the largest valid table, but keep additional substantial sheets.
    candidates.sort(key=lambda x: len(x[1]), reverse=True)
    largest = len(candidates[0][1])
    out = []
    for sheet_name, records in candidates:
        if len(records) >= max(5, int(0.10 * largest)):
            for record in records:
                record = dict(record)
                record["_worksheet"] = sheet_name
                out.append(record)
    return out


# ---------------------------------------------------------------------------
# Broad public/reference battery-cell sources
# ---------------------------------------------------------------------------


def import_tum_cell_database(cache_dir):
    """Import TUM/BetterBat datasheet-derived cell database across all worksheets."""
    urls = [
        "https://raw.githubusercontent.com/TUMFTM/TechnoEconomicCellSelection/main/inputs/CellDatabase_v6.xlsx",
        "https://github.com/TUMFTM/TechnoEconomicCellSelection/raw/refs/heads/main/inputs/CellDatabase_v6.xlsx",
    ]
    path = cache_dir / "CellDatabase_v6.xlsx"

    # If V2 cached an HTML error page under the .xlsx name, invalidate it.
    if path.exists():
        try:
            with path.open("rb") as f:
                if f.read(2) != b"PK":
                    path.unlink()
        except Exception:
            pass

    last_exc = None
    for url in urls:
        try:
            data = fetch(url, None, timeout=60, retries=2)
            if data[:2] != b"PK":
                raise RuntimeError("download was not a valid XLSX/ZIP payload")
            path.write_bytes(data)
            break
        except Exception as exc:
            last_exc = exc
    else:
        raise RuntimeError(f"Unable to download TUM cell workbook: {last_exc}")

    records = xlsx_to_dicts_all_sheets(
        path,
        required_any=("Manufacturer", "Capacity", "Voltage", "Mass", "Cell"),
    )

    out = []
    for i, r in enumerate(records):
        manufacturer = norm_space(
            first_value_fuzzy(
                r,
                [
                    "Manufacturer", "Producer", "Company", "Brand", "OEM",
                    "Cell manufacturer", "Cell Manufacturer"
                ],
                "Unknown",
            )
        )
        model = norm_space(
            first_value_fuzzy(
                r,
                [
                    "Cell", "Cell name", "Cell Name", "Model", "Name",
                    "Designation", "Type", "Product", "Cell type",
                    "Cell Type", "Part number", "Part Number",
                    "Cell ID", "ID", "Name of cell", "Cell designation", "Model name"
                ],
                "",
            )
        )
        if not model:
            continue

        capacity = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Capacity [Ah]", "Nominal capacity [Ah]",
                    "Nominal Capacity [Ah]", "Capacity", "Nominal capacity",
                    "Capacity (Ah)", "Nominal Capacity (Ah)", "Nominal cell capacity [Ah]", "Cnom [Ah]"
                ],
            )
        )
        cap_mah = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Capacity [mAh]", "Nominal capacity [mAh]",
                    "Capacity (mAh)", "Nominal Capacity (mAh)"
                ],
            )
        )
        if capacity is None and cap_mah is not None:
            capacity = cap_mah / 1000.0
        elif capacity is not None and capacity > 100:
            capacity /= 1000.0

        voltage = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Nominal voltage [V]", "Nominal Voltage [V]",
                    "Voltage [V]", "Nominal voltage", "Voltage",
                    "U_nom [V]", "U nominal [V]", "Nominal cell voltage [V]", "Unom [V]"
                ],
            )
        )
        energy = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Energy [Wh]", "Nominal energy [Wh]", "Nominal Energy [Wh]",
                    "Energy", "Nominal energy"
                ],
            )
        )
        if energy is None and capacity and voltage:
            energy = capacity * voltage

        mass = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Mass [g]", "Weight [g]", "Cell mass [g]",
                    "Cell Mass [g]", "Mass", "Weight", "m [g]", "Cell weight [g]", "Mass cell [g]"
                ],
            )
        )
        mass_kg = optional_float(first_value_fuzzy(r, ["Mass [kg]", "Weight [kg]"]))
        if mass is None and mass_kg is not None:
            mass = mass_kg * 1000.0
        if mass is not None and 0 < mass < 2:
            # Most single-cell masses are tens/hundreds of grams. Some sheets
            # encode kg without preserving the unit in the header.
            mass *= 1000.0

        current = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Max. continuous discharge current [A]",
                    "Max continuous discharge current [A]",
                    "Maximum continuous discharge current [A]",
                    "Continuous discharge current [A]",
                    "Maximum discharge current [A]",
                    "Max discharge current [A]",
                    "Discharge current [A]",
                    "I_dis,max [A]",
                    "I max [A]", "Maximum continuous discharge [A]", "Continuous discharge [A]",
                ],
            )
        )
        burst = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Max pulse discharge current [A]",
                    "Maximum pulse current [A]",
                    "Pulse discharge current [A]",
                    "Peak discharge current [A]",
                ],
            )
        )

        charge_v = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Charge voltage [V]", "Max voltage [V]",
                    "Upper cut-off voltage [V]", "Upper cutoff voltage [V]",
                    "U_max [V]"
                ],
            )
        )
        cutoff_v = optional_float(
            first_value_fuzzy(
                r,
                [
                    "Cut-off voltage [V]", "Cutoff voltage [V]",
                    "Min voltage [V]", "Lower cut-off voltage [V]",
                    "Lower cutoff voltage [V]", "U_min [V]"
                ],
            )
        )

        chemistry = norm_space(
            first_value_fuzzy(
                r,
                ["Chemistry", "Cathode", "Cell chemistry", "Cell Chemistry"],
                "",
            )
        )
        fmt = norm_space(
            first_value_fuzzy(
                r,
                [
                    "Format", "Cell format", "Cell Format", "Geometry",
                    "Form factor", "Cell geometry"
                ],
                "",
            )
        )
        diameter = optional_float(first_value_fuzzy(r, ["Diameter [mm]", "Diameter"]))
        height = optional_float(
            first_value_fuzzy(r, ["Height [mm]", "Height", "Cell height [mm]"])
        )
        width = optional_float(first_value_fuzzy(r, ["Width [mm]", "Width"]))
        length = optional_float(first_value_fuzzy(r, ["Length [mm]", "Length"]))

        dcir = optional_float(
            first_value_fuzzy(
                r,
                [
                    "DCIR [mOhm]", "DCIR [mΩ]", "DC resistance [mOhm]",
                    "DC resistance [mΩ]", "Internal resistance [mOhm]"
                ],
            )
        )

        cid = canonical_id("cell", manufacturer, model)
        enough_geometry = all(
            x is not None and x > 0 for x in (capacity, voltage, mass)
        )
        eligible = int(bool(enough_geometry and current and current > 0))

        out.append(
            {
                "record_id": record_id(
                    "TUMBetterBat",
                    f"{r.get('_worksheet','')}|{i}|{manufacturer}|{model}",
                    "cell",
                ),
                "canonical_id": cid,
                "manufacturer": manufacturer,
                "model": model,
                "format": fmt,
                "chemistry": chemistry,
                "nominal_voltage_v": voltage,
                "capacity_ah": capacity,
                "energy_wh": energy,
                "charge_voltage_v": charge_v,
                "cutoff_voltage_v": cutoff_v,
                "max_cont_discharge_a": current,
                "max_burst_discharge_a": burst,
                "mass_g": mass,
                "diameter_mm": diameter,
                "height_mm": height,
                "width_mm": width,
                "length_mm": length,
                "dcir_mohm": dcir,
                "source_name": "TUM/BetterBat Battery Cell Database",
                "source_url": urls[0],
                "source_record_id": f"{manufacturer}|{model}",
                "source_license": "CC BY 4.0 dataset lineage via Zenodo record 10679242",
                "license_status": "known",
                "data_quality_tier": "B",
                "optimizer_eligible": eligible,
                "checked_date": TODAY,
                "notes": (
                    f"Datasheet-derived academic cell database; worksheet "
                    f"{r.get('_worksheet','unknown')}. Reverify finalist cells "
                    f"against latest manufacturer datasheet."
                ),
            }
        )


    if not out:
        # Preserve a concise workbook preview so a future parser fix does not
        # require the user to manually inspect the XLSX.
        try:
            preview_path = cache_dir.parent / "raw_reference" / "tum_workbook_preview.txt"
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            sheets = xlsx_sheet_rows(path)
            lines = []
            for sheet_name, sheet_rows in sheets.items():
                lines.append(f"=== SHEET: {sheet_name} ===")
                for row in sheet_rows[:25]:
                    lines.append(" | ".join(norm_space(x) for x in row[:40]))
                lines.append("")
            preview_path.write_text("\n".join(lines), encoding="utf-8")
            eprint(
                f"WARNING TUM workbook parsed zero cell rows. "
                f"Saved workbook preview to {preview_path}"
            )
        except Exception as exc:
            eprint(f"WARNING could not write TUM workbook preview: {exc}")

    return out

def import_celldb_public(cache_dir, max_pages=20):
    """Crawl public CellDB catalog records as discovery/reference rows."""
    # The homepage reported 203 cell models at research time.
    # This crawler follows public /en/cells pages found from the listing/search
    # surface. It does not attempt to access Pro/private downloads.
    base = "https://www.mewyeahcloud.com"
    listing_candidates = [
        f"{base}/en/cells",
        f"{base}/en/cell-database",
        f"{base}/en",
    ]
    links = set()

    class LinkParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag.lower() != "a":
                return
            href = dict(attrs).get("href")
            if href and "/en/cells/" in href:
                links.add(urllib.parse.urljoin(base, href))

    for page_num in range(1, max_pages + 1):
        for root_url in listing_candidates[:2]:
            url = root_url if page_num == 1 else f"{root_url}?page={page_num}"
            try:
                data = fetch(
                    url,
                    cache_dir / f"celldb_list_{page_num}_{hashlib.sha1(root_url.encode()).hexdigest()[:6]}.html",
                    timeout=20,
                    retries=1,
                )
            except Exception:
                continue
            p = LinkParser()
            p.feed(data.decode("utf-8", errors="replace"))

    out = []
    for i, url in enumerate(sorted(links)):
        try:
            data = fetch(
                url,
                cache_dir / f"celldb_{hashlib.sha1(url.encode()).hexdigest()[:16]}.html",
                timeout=20,
                retries=1,
            )
        except Exception:
            continue
        text = re.sub(r"<[^>]+>", " ", data.decode("utf-8", errors="replace"))
        text = html.unescape(re.sub(r"\s+", " ", text))

        # URL slug often starts brand-model-capacity; prefer page title fields.
        title_m = re.search(r"#?\s*([A-Za-z0-9][A-Za-z0-9./_-]{2,})\s+Battery Cell", text, re.I)
        model = title_m.group(1) if title_m else ""
        # More robust field extraction from visible engineering-parameter text.
        brand_m = re.search(r"Brand\s+(.+?)\s+Model\s+", text, re.I)
        model_m = re.search(r"Model\s+(.+?)\s+Form factor\s+", text, re.I)
        fmt_m = re.search(r"Form factor\s+(.+?)\s+Chemistry\s+", text, re.I)
        chem_m = re.search(r"Chemistry\s+(.+?)\s+Nominal capacity\s+", text, re.I)
        cap_m = re.search(r"Nominal capacity\s+([0-9.]+)\s*(Ah|mAh)", text, re.I)
        volt_m = re.search(r"Nominal voltage\s+([0-9.]+)\s*V", text, re.I)
        weight_m = re.search(r"Weight\s+([0-9.]+)\s*(kg|g)", text, re.I)
        dims_m = re.search(r"Dimensions\s+([0-9.]+)\s*[×x]\s*([0-9.]+)\s*[×x]\s*([0-9.]+)\s*mm", text, re.I)
        current_m = re.search(r"Continuous discharge\s+([0-9.]+)\s*A", text, re.I)

        manufacturer = norm_space(brand_m.group(1) if brand_m else "Unknown")
        model = norm_space(model_m.group(1) if model_m else model)
        if not model:
            continue

        capacity = float(cap_m.group(1)) if cap_m else None
        if cap_m and cap_m.group(2).lower() == "mah":
            capacity /= 1000.0
        voltage = float(volt_m.group(1)) if volt_m else None
        mass = None
        if weight_m:
            mass = float(weight_m.group(1))
            if weight_m.group(2).lower() == "kg":
                mass *= 1000.0

        cid = canonical_id("cell", manufacturer, model)
        out.append({
            "record_id": record_id("CellDB", url, "cell"),
            "canonical_id": cid,
            "manufacturer": manufacturer,
            "model": model,
            "format": norm_space(fmt_m.group(1) if fmt_m else ""),
            "chemistry": norm_space(chem_m.group(1) if chem_m else ""),
            "nominal_voltage_v": voltage,
            "capacity_ah": capacity,
            "energy_wh": (voltage * capacity if voltage and capacity else None),
            "max_cont_discharge_a": float(current_m.group(1)) if current_m else None,
            "mass_g": mass,
            "length_mm": float(dims_m.group(1)) if dims_m else None,
            "width_mm": float(dims_m.group(2)) if dims_m else None,
            "height_mm": float(dims_m.group(3)) if dims_m else None,
            "source_name": "CellDB public catalog",
            "source_url": url,
            "source_record_id": model,
            "source_license": "Public catalog; redistribution license not established",
            "license_status": "unclear",
            "data_quality_tier": "C",
            "optimizer_eligible": 0,
            "checked_date": TODAY,
            "notes": "Public discovery record. CellDB itself advises verification against the latest manufacturer datasheet.",
        })
    return out


def import_lygte(cache_dir):
    url = "https://lygte-info.dk/info/batteryIndex.html"
    data = fetch(url, cache_dir / "lygte_battery_index.html")
    table = best_table(html_tables(data), ["Battery Name","Type","Size","Rated"])
    rows = []
    for i, r in enumerate(table_to_dicts(table) if table else []):
        name = first_value(r, ["Battery Name","Name"])
        if not name:
            continue
        # Manufacturer is often the first token. Keep raw name as model too
        # because this is a discovery/reference layer.
        tokens = norm_space(name).split()
        manufacturer = tokens[0] if tokens else "Unknown"
        model = norm_space(name)
        size = norm_space(first_value(r, ["Size"], ""))
        rated_mah = optional_float(first_value(r, ["Rated mAh","Rated","mAh"]))
        fmt = ""
        if "18650" in size or "18650" in model.upper():
            fmt = "18650"
        elif "21700" in size or "21700" in model.upper():
            fmt = "21700"
        cid = canonical_id("cell", manufacturer, model)
        rows.append({
            "record_id": record_id("LYGTE", f"{i}|{model}", "cell"),
            "canonical_id": cid,
            "manufacturer": manufacturer,
            "model": model,
            "format": fmt,
            "chemistry": norm_space(first_value(r, ["Type"], "")),
            "capacity_ah": rated_mah / 1000.0 if rated_mah else None,
            "source_name": "LYGTE battery test index",
            "source_url": url,
            "source_record_id": model,
            "source_license": "Site terms not established",
            "license_status": "unclear",
            "data_quality_tier": "C",
            "optimizer_eligible": 0,
            "checked_date": TODAY,
            "notes": "Broad tested-cell reference record; mass/current/manufacturer spec verification required.",
        })
    return rows

def import_google_18650(cache_dir):
    url = ("https://docs.google.com/spreadsheets/d/"
           "1fYjDxxCJXfm2wdpGWCaOUGq8V8TOEgsnplHQa4YQpRQ/"
           "export?format=csv&gid=0")
    data = fetch(url, cache_dir / "18650_identification_reference.csv")
    text = data.decode("utf-8-sig", errors="replace")
    rows = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text))):
        brand = norm_space(first_value(r, ["Brand"], "Unknown"))
        model = norm_space(first_value(r, ["Model (Markings)","Model"], ""))
        if not model:
            continue
        capacity = optional_float(first_value(r, ["Capacity (mAh)","Capacity"]))
        discharge = optional_float(first_value(r, ["Discharge A (Max)","Discharge A","Max Discharge"]))
        chemistry = norm_space(first_value(r, ["Chemistry"], ""))
        datasheet = norm_space(first_value(r, ["Data Sheet","Data Sheet (Backup)"], ""))
        cid = canonical_id("cell", brand, model)
        rows.append({
            "record_id": record_id("18650Reference", f"{i}|{brand}|{model}", "cell"),
            "canonical_id": cid,
            "manufacturer": brand,
            "model": model,
            "format": "18650",
            "chemistry": chemistry,
            "capacity_ah": capacity / 1000.0 if capacity else None,
            "max_cont_discharge_a": discharge,
            "datasheet_url": datasheet,
            "source_name": "18650 Lithium Ion Battery Identification Reference",
            "source_url": url,
            "source_record_id": f"{brand}|{model}",
            "source_license": "Public reference sheet; explicit redistribution license not established",
            "license_status": "unclear",
            "data_quality_tier": "C",
            "optimizer_eligible": 0,
            "checked_date": TODAY,
            "notes": "Reference sheet; requires current manufacturer verification and mass before optimizer use.",
        })
    return rows

# ---------------------------------------------------------------------------
# Commercial battery packs
# ---------------------------------------------------------------------------

def _html_plain_text(data):
    return html.unescape(
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", data.decode("utf-8", errors="replace")))
    ).strip()


def import_genstattu_current_packs(cache_dir, max_products=320):
    """
    Import current Gens Ace / Tattu commercial packs.

    Uses broad category pages for discovery, then visits product pages so mass
    and dimensions can be captured. This intentionally favors usable Tier-B
    pack records over merely adding product names.
    """
    category_roots = [
        "https://genstattu.com/",
        "https://genstattu.com/hot-items/",
        "https://genstattu.com/mapping-drone-battery.html",
        "https://genstattu.com/surveillance-drone-battery.html",
        "https://genstattu.com/6s-22-2-v-lipo-battery.html",
        "https://genstattu.com/8s-29-6v-lipo-battery.html",
        "https://genstattu.com/12s-44-4v-lipo-battery.html",
        "https://genstattu.com/10000mah/",
        "https://genstattu.com/12000mah-lipo/",
        "https://genstattu.com/16000mah-lipo/",
        "https://genstattu.com/22000mah-lipo/",
        "https://genstattu.com/new-product.html",
    ]

    product_links = set()

    class ProductLinkParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag.lower() != "a":
                return
            href = dict(attrs).get("href")
            if not href:
                return
            full = urllib.parse.urljoin("https://www.genstattu.com/", href)
            if not full.startswith("https://www.genstattu.com/"):
                return
            path = urllib.parse.urlparse(full).path.lower()
            # Product URLs are typically descriptive .html pages. Exclude
            # category/listing/support pages.
            blocked = (
                "new-product.html", "mapping-drone-battery.html",
                "surveillance-drone-battery.html", "find-battery", "blog",
                "page=", "content/", "category", "charger", "adapter",
                "connector", "cable", "t-shirt", "apparel"
            )
            if path.endswith(".html") and not any(x in full.lower() for x in blocked):
                product_links.add(full.split("?")[0])

    for root_url in category_roots:
        for page in range(1, 13):
            sep = "&" if "?" in root_url else "?"
            url = root_url if page == 1 else f"{root_url}{sep}page={page}"
            try:
                data = fetch(
                    url,
                    cache_dir
                    / f"genstattu_list_{hashlib.sha1(url.encode()).hexdigest()[:16]}.html",
                    timeout=25,
                    retries=1,
                )
            except Exception:
                continue
            parser = ProductLinkParser()
            parser.feed(data.decode("utf-8", errors="replace"))
            if len(product_links) >= max_products * 2:
                break

    out = []
    for i, url in enumerate(sorted(product_links)[:max_products]):
        try:
            data = fetch(
                url,
                cache_dir / f"genstattu_{hashlib.sha1(url.encode()).hexdigest()[:18]}.html",
                timeout=25,
                retries=1,
            )
        except Exception:
            continue

        text = _html_plain_text(data)

        # Require battery-like content; skip chargers/accessories.
        if not re.search(r"\b(?:LiPo|Li-ion|Battery Pack|Battery)\b", text, re.I):
            continue
        if re.search(r"\b(?:charger|charging cable|adapter)\b", text[:300], re.I):
            continue

        # Product title.
        title_m = re.search(
            r"(?:#|Quantity:)?\s*((?:Tattu|Gens ace|Gens Ace).{8,180}?(?:Battery|Pack).*?)"
            r"(?:Quantity:|Add to Wish|Description|Specs|\$)",
            text,
            re.I,
        )
        title = norm_space(title_m.group(1) if title_m else "")
        if not title:
            slug = Path(urllib.parse.urlparse(url).path).stem
            title = norm_space(slug.replace("-", " "))

        brand = "Tattu" if "tattu" in title.lower() else "Gens Ace"

        def rx(pattern, flags=re.I):
            m = re.search(pattern, text, flags)
            return norm_space(m.group(1)) if m else ""

        def rx_after_label(label_pattern):
            # Handles rendered table text such as:
            # "Capacity(mAh): | 10000"
            m = re.search(
                label_pattern + r"\s*:?\s*[^0-9A-Za-z.+-]{0,25}"
                r"([A-Za-z0-9.+/-]+)",
                text,
                re.I,
            )
            return norm_space(m.group(1)) if m else ""

        sku = rx(r"SKU:\s*([A-Za-z0-9._-]+)")
        cap = optional_float(rx_after_label(r"Capacity\s*\(mAh\)"))
        if cap is None:
            cap = optional_float(rx(r"\b([0-9]{3,6})\s*mAh\b"))
        cap_ah = cap / 1000.0 if cap and cap > 50 else cap

        config = rx_after_label(r"Configuration")
        if not config:
            config = rx(r"\b([0-9]+\s*S[0-9]*\s*P?)\b")
        s, p = parse_cells(config)

        voltage = optional_float(rx_after_label(r"Voltage\s*\(V\)"))
        if voltage is None:
            voltage = optional_float(rx(r"\b([0-9]+(?:\.[0-9]+)?)\s*V\b"))

        c_cont = optional_float(rx_after_label(r"Discharge Rate\s*\(C\)"))
        if c_cont is None:
            c_cont = optional_float(rx(r"\b([0-9]+(?:\.[0-9]+)?)\s*C\b"))

        c_burst = optional_float(
            rx_after_label(r"Max Burst discharge Rate\s*\(C\)")
        )

        mass = optional_float(rx_after_label(r"Net Weight[^:|]{0,30}"))
        length = optional_float(rx_after_label(r"Length[^:|]{0,30}"))
        width = optional_float(rx_after_label(r"Width[^:|]{0,30}"))
        height = optional_float(rx_after_label(r"Height[^:|]{0,30}"))
        connector = rx_after_label(r"Connector Type")

        # Prefer the first visible sale/current price near the title.
        price = optional_float(rx(r"\$([0-9]+(?:\.[0-9]{1,2})?)"))
        in_stock = None
        if re.search(r"\bOut of Stock\b", text, re.I):
            in_stock = 0
        elif re.search(r"\b(?:Add to Cart|Choose Options|stock)\b", text, re.I):
            in_stock = 1

        if not (s and cap_ah and voltage):
            continue

        energy = voltage * cap_ah
        max_i = c_cont * cap_ah if c_cont else None
        max_burst = c_burst * cap_ah if c_burst else None

        model = sku or title
        cid = canonical_id("pack", brand, model)
        eligible = int(
            all(x is not None and x > 0 for x in (s, voltage, cap_ah, mass))
            and c_cont is not None
            and c_cont > 0
        )

        out.append(
            {
                "record_id": record_id("GensTattu", url, "pack"),
                "canonical_id": cid,
                "manufacturer": brand,
                "model": title,
                "chemistry": "LiPo/LiHV",
                "series_cells": s,
                "parallel_cells": p,
                "voltage_nominal_v": voltage,
                "capacity_ah": cap_ah,
                "energy_wh": energy,
                "c_rating_cont": c_cont,
                "c_rating_burst": c_burst,
                "max_cont_current_a": max_i,
                "max_burst_current_a": max_burst,
                "mass_g": mass,
                "length_mm": length,
                "width_mm": width,
                "height_mm": height,
                "connector": connector,
                "price_usd": price,
                "status": (
                    "In stock" if in_stock == 1
                    else "Out of stock" if in_stock == 0
                    else ""
                ),
                "source_name": "Gens Ace / Tattu current product catalog",
                "source_url": url,
                "source_record_id": model,
                "source_license": "Manufacturer/retailer-published catalog; redistribution terms not asserted",
                "license_status": "unclear",
                "data_quality_tier": "B",
                "optimizer_eligible": eligible,
                "checked_date": TODAY,
                "notes": "Current product-page specification record.",
            }
        )

    return out


def tattu_rows_from_table(table, page_url):
    out = []
    for i, r in enumerate(table_to_dicts(table) if table else []):
        sku = norm_space(first_value(r, ["SKU","Article NO.","Article No","Article"]))
        cells_raw = first_value(r, ["Cell Count","Cells"])
        voltage = optional_float(first_value(r, ["Voltage","Voltage (V)"]))
        c_cont = optional_float(first_value(r, ["C Rate","C-rate","C Rating"]))
        capacity = optional_float(first_value(r, ["Capacity","Capacity (mAh)"]))
        weight = optional_float(first_value(r, ["Net Weight","Weight","Weight (g)"]))
        l = optional_float(first_value(r, ["L","Length"]))
        w = optional_float(first_value(r, ["W","Width"]))
        h = optional_float(first_value(r, ["T","H","Height","Thickness"]))
        plug = norm_space(first_value(r, ["Discharge Plug","Plug","Connector"]))
        s, p = parse_cells(cells_raw)
        if capacity and capacity > 50:  # mAh
            capacity_ah = capacity / 1000.0
        else:
            capacity_ah = capacity
        if not sku and not (s and capacity_ah):
            continue
        model = sku or f"Tattu {s}S {capacity_ah:g}Ah"
        cid = canonical_id("pack", "Tattu", model)
        energy = voltage * capacity_ah if voltage and capacity_ah else None
        max_i = c_cont * capacity_ah if c_cont and capacity_ah else None
        eligible = int(all(x is not None and x > 0 for x in (s, voltage, capacity_ah, weight)))
        out.append({
            "record_id": record_id("Tattu", f"{page_url}|{i}|{model}", "pack"),
            "canonical_id": cid,
            "manufacturer": "Tattu",
            "model": model,
            "chemistry": "LiPo/LiHV (verify per SKU)",
            "series_cells": s,
            "parallel_cells": p,
            "voltage_nominal_v": voltage,
            "capacity_ah": capacity_ah,
            "energy_wh": energy,
            "c_rating_cont": c_cont,
            "max_cont_current_a": max_i,
            "mass_g": weight,
            "length_mm": l,
            "width_mm": w,
            "height_mm": h,
            "connector": plug,
            "source_name": "Tattu battery finder",
            "source_url": page_url,
            "source_record_id": sku or model,
            "source_license": "Manufacturer-published catalog; redistribution terms not asserted",
            "license_status": "unclear",
            "data_quality_tier": "B" if eligible else "C",
            "optimizer_eligible": eligible,
            "checked_date": TODAY,
            "notes": "Manufacturer finder; verify exact chemistry and price before purchase.",
        })
    return out

def import_tattu(cache_dir):
    base = "https://www.tattuworld.com/battery-search/"
    all_rows = []
    seen = set()
    # Try common pagination conventions. Stop after repeated/no new records.
    urls = [base]
    for page in range(2, 16):
        urls.extend([
            f"{base}?page={page}",
            f"{base}page/{page}/",
        ])
    empty_streak = 0
    for n, url in enumerate(urls):
        try:
            safe = re.sub(r"[^A-Za-z0-9]+", "_", url)[-100:]
            data = fetch(url, cache_dir / f"tattu_{safe}.html", timeout=20, retries=1)
        except Exception:
            continue
        table = best_table(html_tables(data), ["SKU","Cell Count","Voltage","Capacity","Net Weight"])
        page_rows = tattu_rows_from_table(table, url)
        new = 0
        for r in page_rows:
            key = r["canonical_id"]
            if key not in seen:
                seen.add(key)
                all_rows.append(r)
                new += 1
        if new == 0:
            empty_streak += 1
        else:
            empty_streak = 0
        if empty_streak >= 5 and all_rows:
            break
    return all_rows

def import_fpvcompare(cache_dir, price_rows):
    url = "https://fpvcompare.com/batteries/"
    data = fetch(url, cache_dir / "fpvcompare_batteries.html")
    table = best_table(html_tables(data), ["Battery brand","Cells","Capacity","Weight","Price"])
    out = []
    if not table:
        return out
    for i, r in enumerate(table_to_dicts(table)):
        # Header labels can be verbose; search by substrings.
        def by_substrings(*parts):
            for k, v in r.items():
                nk = norm_key(k)
                if all(norm_key(p) in nk for p in parts):
                    return v
            return None

        name = norm_space(by_substrings("Battery","brand") or first_value(r, ["Battery","Model"], ""))
        if not name:
            continue
        brand = name.split()[0] if name.split() else "Unknown"
        cells_raw = by_substrings("Cells")
        cap_raw = by_substrings("Capacity")
        weight_raw = by_substrings("Weight")
        current_raw = by_substrings("Current")
        chem = norm_space(by_substrings("Chem") or "")
        dims_raw = by_substrings("Dim")
        plug = norm_space(by_substrings("Plug") or "")
        price_raw = by_substrings("Price")
        s, p = parse_cells(cells_raw)
        cap = optional_float(cap_raw)
        # FPV table usually exposes mAh and Wh together. Treat first number >50
        # as mAh.
        cap_ah = cap / 1000.0 if cap and cap > 50 else cap
        weight = optional_float(weight_raw)
        l, w, h = parse_dimensions(dims_raw)
        price = optional_float(price_raw)
        cid = canonical_id("pack", brand, name)
        out.append({
            "record_id": record_id("FPVCompare", f"{i}|{name}", "pack"),
            "canonical_id": cid,
            "manufacturer": brand,
            "model": name,
            "chemistry": chem,
            "series_cells": s,
            "parallel_cells": p,
            "capacity_ah": cap_ah,
            "mass_g": weight,
            "length_mm": l,
            "width_mm": w,
            "height_mm": h,
            "connector": plug,
            "price_usd": price,
            "source_name": "FPVCompare battery table",
            "source_url": url,
            "source_record_id": name,
            "source_license": "Site terms not established",
            "license_status": "unclear",
            "data_quality_tier": "C",
            "optimizer_eligible": 0,
            "checked_date": TODAY,
            "notes": f"Reference/current comparison source. Current field raw: {norm_space(current_raw)}. Verify manufacturer specifications.",
        })
        if price is not None:
            price_rows.append({
                "price_id": record_id("FPVCompare", f"{cid}|{price}", "price"),
                "component_type": "battery_pack",
                "canonical_id": cid,
                "manufacturer": brand,
                "model": name,
                "vendor": "FPVCompare-linked shops",
                "price_usd": price,
                "quantity": 1,
                "in_stock": None,
                "source_url": url,
                "checked_date": TODAY,
                "source_name": "FPVCompare battery table",
                "license_status": "unclear",
                "notes": "Reference price; verify retailer before purchase.",
            })
    return out


# ---------------------------------------------------------------------------
# Current / reference ESC sources
# ---------------------------------------------------------------------------

def _parse_cell_range(text):
    nums = [int(x) for x in re.findall(r"(\d+)\s*S", norm_space(text).upper())]
    if not nums:
        return None, None
    return min(nums), max(nums)


def import_tmotor_official_escs(cache_dir, max_products=80):
    """Import current T-MOTOR ESC product records directly from official stores."""
    listing_urls = [
        "https://shop.tmotor.com/collections/drone-esc",
        "https://store.tmotor.com/categorys/uav-esc",
        "https://uav-en.tmotor.com/Multirotor/ESC/",
        "https://uav-en.tmotor.com/Multirotor/ESC/alpha/",
        "https://uav-en.tmotor.com/Multirotor/ESC/flame/",
    ]

    product_links = set()

    class TmotorLinkParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag.lower() != "a":
                return
            href = dict(attrs).get("href")
            if not href:
                return
            for base in ("https://shop.tmotor.com", "https://store.tmotor.com"):
                full = urllib.parse.urljoin(base, href)
                if (
                    full.startswith(base)
                    and (
                        "/products/" in full
                        or "/product/" in full
                        or "goods.php?id=" in full
                    )
                ):
                    product_links.add(full.split("#")[0])

    for url in listing_urls:
        try:
            data = fetch(
                url,
                cache_dir / f"tmotor_list_{hashlib.sha1(url.encode()).hexdigest()[:14]}.html",
                timeout=25,
                retries=1,
            )
        except Exception:
            continue
        p = TmotorLinkParser()
        p.feed(data.decode("utf-8", errors="replace"))

    out = []
    for url in sorted(product_links)[:max_products]:
        try:
            data = fetch(
                url,
                cache_dir / f"tmotor_{hashlib.sha1(url.encode()).hexdigest()[:18]}.html",
                timeout=25,
                retries=1,
            )
        except Exception:
            continue
        text = _html_plain_text(data)

        if "ESC" not in text[:800].upper():
            continue

        title_m = re.search(
            r"((?:T-MOTOR\s+)?(?:ALPHA|FLAME|AIR|V|THUNDER|T)\s*[-A-Za-z0-9. ]{2,45}ESC[^$]{0,40})",
            text,
            re.I,
        )
        title = norm_space(title_m.group(1) if title_m else "")
        if not title:
            slug = Path(urllib.parse.urlparse(url).path).stem
            title = norm_space(slug.replace("-", " "))
        if "esc" not in title.lower():
            title += " ESC"

        cont = optional_float(
            next(
                (
                    m.group(1)
                    for pat in [
                        r"continuous(?:\s+current)?\s*(?:is|:)?\s*([0-9.]+)\s*A",
                        r"Con\.\s*Current\s*[:：]?\s*([0-9.]+)\s*A",
                        r"([0-9.]+)\s*A\s+continuous",
                    ]
                    if (m := re.search(pat, text, re.I))
                ),
                None,
            )
        )
        peak = optional_float(
            next(
                (
                    m.group(1)
                    for pat in [
                        r"(?:peak|burst)(?:\s+current)?\s*(?:is|:)?\s*([0-9.]+)\s*A",
                        r"([0-9.]+)\s*A\s+(?:peak|burst)",
                    ]
                    if (m := re.search(pat, text, re.I))
                ),
                None,
            )
        )

        min_s, max_s = _parse_cell_range(text[:5000])
        mass = optional_float(
            next(
                (
                    m.group(1)
                    for pat in [
                        r"(?:ESC\s+)?Weight(?:\s*\([^)]*\))?\s*[:：]?\s*([0-9.]+)\s*g",
                        r"compact\s+([0-9.]+)\s*g\s+design",
                    ]
                    if (m := re.search(pat, text, re.I))
                ),
                None,
            )
        )
        dims = ""
        dm = re.search(
            r"(?:ESC\s+)?Size\s*[:：]?\s*([0-9.]+\s*[x×*]\s*[0-9.]+\s*[x×*]\s*[0-9.]+\s*mm)",
            text,
            re.I,
        )
        if dm:
            dims = norm_space(dm.group(1))

        price = optional_float(
            next(
                (
                    m.group(1)
                    for pat in [r"\$([0-9]+(?:\.[0-9]{1,2})?)"]
                    if (m := re.search(pat, text))
                ),
                None,
            )
        )

        cid = canonical_id("esc", "T-MOTOR", title)
        eligible = int(
            cont is not None and cont > 0
            and max_s is not None and max_s > 0
            and mass is not None and mass > 0
        )

        out.append(
            {
                "record_id": record_id("TMOTORofficial", url, "esc"),
                "canonical_id": cid,
                "manufacturer": "T-MOTOR",
                "model": title,
                "continuous_current_a": cont,
                "burst_current_a": peak,
                "min_cells": min_s,
                "max_cells": max_s,
                "mass_g": mass,
                "dimensions": dims,
                "price_usd": price,
                "status": "Current catalog",
                "source_name": "T-MOTOR official ESC catalog",
                "source_url": url,
                "source_record_id": title,
                "source_license": "Manufacturer-published product catalog; redistribution terms not asserted",
                "license_status": "unclear",
                "data_quality_tier": "A",
                "optimizer_eligible": eligible,
                "checked_date": TODAY,
                "notes": "Current official T-MOTOR product record.",
            }
        )
    return out


def import_hobbywing_official_escs(cache_dir, max_products=120):
    """Import current HOBBYWING XRotor/UAV ESC specs from official product pages."""
    discovery_urls = [
        "https://www.hobbywing.com/en/service?id=17_36_72",
        "https://www.hobbywingdirect.com/collections/xrotor-esc",
        "https://www.hobbywingdirect.com/collections/xrotor-pro-esc",
        "https://www.hobbywingdirect.com/collections/xrotor-series-multicopters-drone/esc",
    ]

    links = set()

    class HWLinkParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag.lower() != "a":
                return
            href = dict(attrs).get("href")
            if not href:
                return
            full = urllib.parse.urljoin("https://www.hobbywing.com", href)
            if "hobbywing.com" in full and "/en/products/" in full:
                links.add(full.split("#")[0])

    for url in discovery_urls:
        try:
            data = fetch(
                url,
                cache_dir / f"hw_list_{hashlib.sha1(url.encode()).hexdigest()[:14]}.html",
                timeout=25,
                retries=1,
            )
        except Exception:
            continue
        p = HWLinkParser()
        p.feed(data.decode("utf-8", errors="replace"))

    out = []
    for url in sorted(links)[:max_products]:
        try:
            data = fetch(
                url,
                cache_dir / f"hw_{hashlib.sha1(url.encode()).hexdigest()[:18]}.html",
                timeout=25,
                retries=1,
            )
        except Exception:
            continue
        text = _html_plain_text(data)

        if "XRotor" not in text and "ESC" not in text[:1000].upper():
            continue

        name_m = re.search(r"Product Name\s+([A-Za-z0-9][A-Za-z0-9 /().*+-]{2,90})", text, re.I)
        name = norm_space(name_m.group(1) if name_m else "")
        if not name:
            continue

        # Filter integrated propulsion systems unless an ESC is clearly the product.
        if "ESC" not in name.upper() and not re.search(r"\bH\d+A\b|\bXRotor\s+\d+A\b", name, re.I):
            continue

        current_m = re.search(
            r"(?:Cont\.?/Peak Current|Continuous current)\s*[:：]?\s*"
            r"([0-9.]+)\s*A(?:\s*/\s*([0-9.]+)\s*A)?",
            text,
            re.I,
        )
        cont = optional_float(current_m.group(1)) if current_m else None
        peak = optional_float(current_m.group(2)) if current_m and current_m.group(2) else None

        # Some industrial pages state "Continuous current : 50A" separately.
        if cont is None:
            m = re.search(r"Continuous current\s*[:：]?\s*([0-9.]+)\s*A", text, re.I)
            cont = optional_float(m.group(1)) if m else None

        cells_text_m = re.search(
            r"(?:LiPo Cells|Input Voltage|Input)\s*[:：]?\s*([^|]{0,80}?)(?=BEC|Continuous|Weight|Size|Wires|$)",
            text,
            re.I,
        )
        cells_text = cells_text_m.group(1) if cells_text_m else text[:5000]
        min_s, max_s = _parse_cell_range(cells_text)

        mass_m = re.search(r"(?:Weight[^:|]{0,30})[:：]?\s*([0-9.]+)\s*g", text, re.I)
        mass = optional_float(mass_m.group(1)) if mass_m else None

        dims_m = re.search(
            r"(?:Size[^:|]{0,20})[:：]?\s*([0-9.]+\s*[x×*]\s*[0-9.]+\s*[x×*]\s*[0-9.]+\s*mm)",
            text,
            re.I,
        )
        dims = norm_space(dims_m.group(1)) if dims_m else ""

        cid = canonical_id("esc", "HOBBYWING", name)
        eligible = int(
            cont is not None and cont > 0
            and max_s is not None and max_s > 0
            and mass is not None and mass > 0
        )

        out.append(
            {
                "record_id": record_id("HOBBYWINGofficial", url, "esc"),
                "canonical_id": cid,
                "manufacturer": "HOBBYWING",
                "model": name,
                "continuous_current_a": cont,
                "burst_current_a": peak,
                "min_cells": min_s,
                "max_cells": max_s,
                "mass_g": mass,
                "dimensions": dims,
                "status": "Current catalog",
                "source_name": "HOBBYWING official ESC catalog",
                "source_url": url,
                "source_record_id": name,
                "source_license": "Manufacturer-published product catalog; redistribution terms not asserted",
                "license_status": "unclear",
                "data_quality_tier": "A",
                "optimizer_eligible": eligible,
                "checked_date": TODAY,
                "notes": "Current official HOBBYWING XRotor/UAV product record.",
            }
        )

    # Official XRotor family table gives three useful entries even when the
    # product-page link discovery changes.
    family_url = "https://www.hobbywingdirect.com/collections/xrotor-esc"
    family = [
        ("XRotor 10A Mini ESC", 10, 15, 2, 3, 6.5, "44.2x12.2x9.2mm"),
        ("XRotor 20A ESC", 20, 30, 3, 4, 14.0, "52.4x21.5x7mm"),
        ("XRotor 40A ESC", 40, 60, 2, 6, 26.0, "68x25x8.7mm"),
    ]
    existing = {norm_key(r["model"]) for r in out}
    for name, cont, peak, smin, smax, mass, dims in family:
        if norm_key(name) in existing:
            continue
        out.append(
            {
                "record_id": record_id("HOBBYWINGofficial", family_url + "|" + name, "esc"),
                "canonical_id": canonical_id("esc", "HOBBYWING", name),
                "manufacturer": "HOBBYWING",
                "model": name,
                "continuous_current_a": cont,
                "burst_current_a": peak,
                "min_cells": smin,
                "max_cells": smax,
                "mass_g": mass,
                "dimensions": dims,
                "status": "Current catalog",
                "source_name": "HOBBYWING official ESC catalog",
                "source_url": family_url,
                "source_record_id": name,
                "source_license": "Manufacturer-published product catalog; redistribution terms not asserted",
                "license_status": "unclear",
                "data_quality_tier": "A",
                "optimizer_eligible": 1,
                "checked_date": TODAY,
                "notes": "Official XRotor family comparison table.",
            }
        )

    return out


def import_splinecloud_tmotor_escs(cache_dir):
    """Import public T-MOTOR ESC spreadsheets from SplineCloud where accessible."""
    # SplineCloud repository currently exposes three spreadsheets.
    repo = "https://splinecloud.com/repository/Serhii.K/T-MOTOR_ESCs/"
    landing = fetch(repo, cache_dir / "splinecloud_tmotor_escs.html", timeout=30, retries=2)
    text = landing.decode("utf-8", errors="replace")

    # Capture select-file links and file labels from the repository HTML.
    link_pairs = []
    for m in re.finditer(
        r'href=["\\\']([^"\\\']*select-file[^"\\\']*)["\\\'][^>]*>(.*?)</a>',
        text, re.I | re.S
    ):
        href = html.unescape(m.group(1))
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        label = norm_space(html.unescape(label))
        if "ESC" in label.upper() and ".XLSX" in label.upper():
            link_pairs.append((urllib.parse.urljoin(repo, href), label))

    out = []
    for file_url, label in link_pairs:
        local = cache_dir / re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        try:
            payload = fetch(file_url, None, timeout=60, retries=2)
            if payload[:2] != b"PK":
                # select-file links can resolve to a preview page rather than
                # the workbook itself. Keep this optional source from polluting
                # the cache with HTML named .xlsx.
                raise RuntimeError("SplineCloud select-file URL returned non-XLSX content")
            local.write_bytes(payload)
            records = xlsx_to_dicts_all_sheets(
                local,
                required_any=("Model", "Current", "Voltage", "Weight", "ESC"),
            )
        except Exception as exc:
            eprint(f"WARNING SplineCloud file {label}: {exc}")
            continue

        for i, r in enumerate(records):
            model = norm_space(first_value(
                r, ["Model", "ESC", "Name", "Product", "Type"], ""
            ))
            if not model:
                continue
            cont = optional_float(first_value(
                r, ["Continuous Current", "Continuous current [A]", "Current", "Cont. Current"]
            ))
            burst = optional_float(first_value(
                r, ["Burst Current", "Burst current [A]", "Peak Current", "Burst"]
            ))
            cells = first_value(r, ["LiPo", "Cells", "Battery", "Input", "Voltage"])
            smin = optional_int(first_value(r, ["Min Cells", "Min S", "S min"]))
            smax = optional_int(first_value(r, ["Max Cells", "Max S", "S max"]))
            if not (smin or smax):
                cell_nums = [int(x) for x in re.findall(r"(\d+)\s*S", norm_space(cells).upper())]
                if cell_nums:
                    smin, smax = min(cell_nums), max(cell_nums)
            mass = optional_float(first_value(r, ["Weight", "Weight [g]", "Mass", "Mass [g]"]))
            price = optional_float(first_value(r, ["Price", "Price [$]", "USD"]))
            bec = norm_space(first_value(r, ["BEC", "BEC Output", "UBEC"], ""))
            dims = norm_space(first_value(r, ["Dimensions", "Size"], ""))

            cid = canonical_id("esc", "T-MOTOR", model)
            out.append({
                "record_id": record_id("SplineCloudTmotor", f"{label}|{i}|{model}", "esc"),
                "canonical_id": cid,
                "manufacturer": "T-MOTOR",
                "model": model,
                "continuous_current_a": cont,
                "burst_current_a": burst,
                "min_cells": smin,
                "max_cells": smax,
                "mass_g": mass,
                "bec": bec,
                "dimensions": dims,
                "price_usd": price,
                "source_name": "SplineCloud T-MOTOR ESC datasets",
                "source_url": file_url,
                "source_record_id": f"{label}:{model}",
                "source_license": "SC-Legacy (SplineCloud Public Access License)",
                "license_status": "known",
                "data_quality_tier": "B",
                "optimizer_eligible": int(bool(cont and cont > 0 and smax and smax > 0 and mass and mass > 0)),
                "checked_date": TODAY,
                "notes": "T-MOTOR specification spreadsheet mirrored/published through SplineCloud.",
            })
    return out

def import_tytorobotics_esc_reference(cache_dir):
    """Import whatever ESC listing metadata Tyto exposes publicly without login."""
    url = "https://database.tytorobotics.com/escs"
    data = fetch(url, cache_dir / "tyto_escs.html", timeout=30, retries=2)
    text = data.decode("utf-8", errors="replace")
    out = []

    # Server-rendered links may expose ESC names even when the full table is
    # hydrated client-side. Capture unique /escs/<slug> links.
    seen = set()
    for m in re.finditer(r'href=["\\\'](/escs/[^"\\\']+)["\\\'][^>]*>(.*?)</a>', text, re.I | re.S):
        href = m.group(1)
        name = norm_space(re.sub(r"<[^>]+>", " ", html.unescape(m.group(2))))
        if not name or name.lower() in {"escs", "view", "details"}:
            continue
        full = urllib.parse.urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        cid = canonical_id("esc", "Unknown", name)
        out.append({
            "record_id": record_id("Tyto", full, "esc"),
            "canonical_id": cid,
            "manufacturer": "",
            "model": name,
            "source_name": "Tyto Robotics ESC database",
            "source_url": full,
            "source_record_id": href,
            "source_license": "Tyto Robotics database terms apply",
            "license_status": "unclear",
            "data_quality_tier": "C",
            "optimizer_eligible": 0,
            "checked_date": TODAY,
            "notes": "Tyto public ESC reference; validate component ratings and static-test limitations before design use.",
        })
    return out


# ---------------------------------------------------------------------------
# USU legacy SQL import
# ---------------------------------------------------------------------------

def download_and_extract_usu(cache_dir):
    zip_url = "https://github.com/usuaero/PropulsionOptimization/archive/refs/heads/master.zip"
    zip_path = cache_dir / "usu_propulsionoptimization_master.zip"
    data = fetch(zip_url, zip_path, timeout=60, retries=2)
    extract_dir = cache_dir / "usu_propulsionoptimization"
    marker = extract_dir / ".extracted"
    if not marker.exists():
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(extract_dir)
        marker.write_text(TODAY)
    return extract_dir

def find_sqlite_files(root):
    found = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            with p.open("rb") as f:
                head = f.read(16)
            if head == b"SQLite format 3\x00":
                found.append(p)
        except Exception:
            pass
    return found

def classify_table(name, columns):
    s = norm_key(name + " " + " ".join(columns))
    if "MOTOR" in s and "ROCKET" not in s:
        return "motor"
    if "PROPELLER" in s or "PROP" in norm_key(name):
        return "prop"
    if "BATTERY" in s or "BATTER" in s:
        return "battery_pack"
    if re.search(r"\bESC\b", name.upper()) or "ELECTRONICSPEEDCONTROL" in s:
        return "esc"
    return None

def generic_legacy_normalize(kind, table, row, cols, rownum, source_url):
    d = dict(zip(cols, row))
    manufacturer = norm_space(first_value(d, ["manufacturer","brand","mfg","make"], "Unknown"))
    model = norm_space(first_value(d, ["name","model","part number","part_number","designation"], f"{table}_{rownum}"))
    source_id = norm_space(first_value(d, ["id","ID","index"], f"{table}:{rownum}"))
    base = {
        "record_id": record_id("USUlegacy", f"{table}|{source_id}", kind),
        "canonical_id": canonical_id(kind, manufacturer, model),
        "manufacturer": manufacturer,
        "model": model,
        "source_name": "USU PropulsionOptimization legacy SQL database",
        "source_url": source_url,
        "source_record_id": f"{table}:{source_id}",
        "source_license": "No explicit repository LICENSE observed",
        "license_status": "unclear",
        "data_quality_tier": "C",
        "optimizer_eligible": 0,
        "checked_date": TODAY,
        "notes": f"Archived/legacy SQL table {table}; reverify before design use.",
    }

    if kind == "motor":
        base.update({
            "kv_rpm_per_v": optional_float(first_value(d, ["kv","Kv","speed constant","speed_constant"])),
            "resistance_ohm": optional_float(first_value(d, ["resistance","R","Rm","resistance ohm"])),
            "no_load_current_a": optional_float(first_value(d, ["no load current","no_load_current","Io","i0"])),
            "max_current_a": optional_float(first_value(d, ["max current","max_current","Imax","current"])),
            "max_power_w": optional_float(first_value(d, ["max power","max_power","power"])),
            "mass_g": optional_float(first_value(d, ["weight","mass","weight g","mass g"])),
            "price_usd": optional_float(first_value(d, ["price","cost"])),
        })
    elif kind == "prop":
        base.update({
            "diameter_in": optional_float(first_value(d, ["diameter","diameter in","diameter_in"])),
            "pitch_in": optional_float(first_value(d, ["pitch","pitch in","pitch_in"])),
            "blade_count": optional_int(first_value(d, ["blades","blade count","blade_count"])),
            "mass_g": optional_float(first_value(d, ["weight","mass","weight g","mass g"])),
            "price_usd": optional_float(first_value(d, ["price","cost"])),
            "aero_data_available": 0,
        })
    elif kind == "battery_pack":
        voltage = optional_float(first_value(d, ["voltage","nominal voltage","voltage_nominal"]))
        capacity = optional_float(first_value(d, ["capacity","capacity mah","mah","capacity ah"]))
        # heuristic: capacities over 100 are almost certainly mAh
        cap_ah = capacity / 1000.0 if capacity and capacity > 100 else capacity
        cells = first_value(d, ["cells","cell count","cell_count","S"])
        s, p = parse_cells(cells)
        base.update({
            "chemistry": norm_space(first_value(d, ["chemistry","type"], "")),
            "series_cells": s,
            "parallel_cells": p,
            "voltage_nominal_v": voltage,
            "capacity_ah": cap_ah,
            "energy_wh": voltage * cap_ah if voltage and cap_ah else None,
            "c_rating_cont": optional_float(first_value(d, ["C","c rating","c_rating","discharge"])),
            "mass_g": optional_float(first_value(d, ["weight","mass","weight g","mass g"])),
            "price_usd": optional_float(first_value(d, ["price","cost"])),
        })
    elif kind == "esc":
        base.update({
            "continuous_current_a": optional_float(first_value(d, ["current","continuous current","cont current","max current","amps"])),
            "burst_current_a": optional_float(first_value(d, ["burst current","burst","burst amps"])),
            "min_cells": optional_int(first_value(d, ["min cells","min_cells","min S"])),
            "max_cells": optional_int(first_value(d, ["max cells","max_cells","max S","cells"])),
            "mass_g": optional_float(first_value(d, ["weight","mass","weight g","mass g"])),
            "resistance_ohm": optional_float(first_value(d, ["resistance","R","resistance ohm"])),
            "price_usd": optional_float(first_value(d, ["price","cost"])),
        })

    # V3 canonicalization fix:
    # The legacy SQL contains many records with missing manufacturer names and
    # generic model labels. V2 collapsed unrelated components together because
    # canonical_id used only manufacturer + model. When identity is weak, add a
    # normalized engineering-spec fingerprint. This is intentionally NOT used
    # for well-identified manufacturer/model records.
    weak_manufacturer = norm_key(manufacturer) in {"", "UNKNOWN", "NONE", "NA", "N/A"}
    weak_model = (
        not norm_space(model)
        or norm_key(model).startswith(norm_key(table))
        or norm_key(model) in {"UNKNOWN", "ESC", "BATTERY", "MOTOR", "PROP", "PROPELLER"}
    )

    if weak_manufacturer or weak_model:
        if kind == "esc":
            identity_bits = [
                base.get("continuous_current_a"),
                base.get("burst_current_a"),
                base.get("min_cells"),
                base.get("max_cells"),
                base.get("mass_g"),
                base.get("resistance_ohm"),
                source_id,
            ]
        elif kind == "battery_pack":
            identity_bits = [
                base.get("series_cells"),
                base.get("parallel_cells"),
                base.get("voltage_nominal_v"),
                base.get("capacity_ah"),
                base.get("c_rating_cont"),
                base.get("mass_g"),
                source_id,
            ]
        else:
            identity_bits = [source_id]

        identity_text = "|".join("" if x is None else str(x) for x in identity_bits)
        base["canonical_id"] = canonical_id(
            kind, manufacturer, f"{model}|{identity_text}"
        )
        base["notes"] += "; V3 identity fingerprint used to avoid false deduplication."

    return base

def import_usu_legacy(cache_dir, raw_dir):
    root = download_and_extract_usu(cache_dir)
    sqlite_files = find_sqlite_files(root)
    result = {"motor": [], "prop": [], "battery_pack": [], "esc": []}
    source_url = "https://github.com/usuaero/PropulsionOptimization"

    for db_path in sqlite_files:
        try:
            con = sqlite3.connect(str(db_path))
            tables = [
                r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                safe_table = re.sub(r"[^A-Za-z0-9_.-]+", "_", table)
                try:
                    cur = con.execute(f'SELECT * FROM "{table.replace(chr(34), chr(34)*2)}"')
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                except Exception:
                    continue

                # Always export raw legacy tables to preserve data even if our
                # heuristic normalizer cannot classify them.
                raw_path = raw_dir / f"usu_{db_path.stem}_{safe_table}.csv"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                with raw_path.open("w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(cols)
                    w.writerows(rows)

                kind = classify_table(table, cols)
                if not kind:
                    continue
                for i, row in enumerate(rows):
                    result[kind].append(
                        generic_legacy_normalize(
                            kind, table, row, cols, i, source_url
                        )
                    )
            con.close()
        except Exception as exc:
            eprint(f"Warning: could not read legacy DB {db_path}: {exc}")

    return result, sqlite_files

# ---------------------------------------------------------------------------
# Dedup / quality helpers
# ---------------------------------------------------------------------------

def dedupe_exact(rows):
    seen = set()
    out = []
    for r in rows:
        key = r.get("record_id")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out

def source_priority(row):
    tier = str(row.get("data_quality_tier") or "Z")
    eligible = int(row.get("optimizer_eligible") or 0)
    lic = row.get("license_status")
    return (
        {"A":0,"B":1,"C":2}.get(tier, 9),
        -eligible,
        0 if lic == "known" else 1,
    )

def canonical_summary(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r.get("canonical_id"), []).append(r)
    summary = []
    for cid, grp in groups.items():
        preferred = sorted(grp, key=source_priority)[0]
        summary.append({
            "canonical_id": cid,
            "manufacturer": preferred.get("manufacturer"),
            "model": preferred.get("model"),
            "best_tier": preferred.get("data_quality_tier"),
            "optimizer_eligible_any": max(int(x.get("optimizer_eligible") or 0) for x in grp),
            "source_records": len(grp),
            "sources": "; ".join(sorted({str(x.get("source_name")) for x in grp})),
        })
    return summary

def completeness(rows, fields):
    out = {}
    n = len(rows)
    for f in fields:
        if f in {"record_id","canonical_id"}:
            continue
        count = sum(1 for r in rows if r.get(f) not in (None, ""))
        out[f] = (count, (100.0 * count / n if n else 0.0))
    return out

# ---------------------------------------------------------------------------
# SQLite export
# ---------------------------------------------------------------------------

def sqlite_write_table(con, table, rows, fields):
    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    coldefs = ", ".join(f'"{f}" TEXT' for f in fields)
    con.execute(f'CREATE TABLE "{table}" ({coldefs})')
    if not rows:
        return
    placeholders = ",".join("?" for _ in fields)
    con.executemany(
        f'INSERT INTO "{table}" VALUES ({placeholders})',
        [[None if r.get(f) is None else str(r.get(f)) for f in fields] for r in rows],
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build broad propulsion-component datasets for PyThrust."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="PyThrust repository root (default: current directory).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use only local PyThrust/APC/seed files; do not download public sources.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Delete network cache and redownload public sources.",
    )
    parser.add_argument(
        "--skip-legacy",
        action="store_true",
        help="Skip archived USU reference database.",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    out = root / "component_data"
    cache = out / "_cache"
    raw = out / "raw_reference"
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)

    if args.refresh and cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("PyThrust Component Data Warehouse Builder")
    print("=" * 78)
    print(f"Root: {root}")
    print(f"Output: {out}")
    print()

    prices = []

    motors = extract_pythrust_motors(root)
    print(f"Local PyThrust motors: {len(motors):,}")

    props = extract_pythrust_props(root)
    print(f"Local PyThrust aerodynamic props: {len(props):,}")

    props, apc_extras = enrich_props_with_apc(root, props, prices)
    print(f"APC physical-only catalog extras: {len(apc_extras):,}")

    cells = extract_seed_cells(root, prices)
    print(f"Verified seed cells: {len(cells):,}")

    packs = []
    escs = []

    if not args.offline:
        # Network sources are isolated: one source failing does not abort the build.
        for label, func in [
            ("TUM/BetterBat cell database", lambda: import_tum_cell_database(cache)),
            ("CellDB public catalog", lambda: import_celldb_public(cache)),
            ("LYGTE battery index", lambda: import_lygte(cache)),
            ("18650 identification sheet", lambda: import_google_18650(cache)),
        ]:
            try:
                new = func()
                cells.extend(new)
                print(f"{label}: +{len(new):,} cell reference rows")
            except Exception as exc:
                eprint(f"WARNING {label}: {exc}")

        try:
            new = import_tattu(cache)
            packs.extend(new)
            print(f"Tattu finder: +{len(new):,} commercial pack rows")
        except Exception as exc:
            eprint(f"WARNING Tattu finder: {exc}")

        try:
            new = import_genstattu_current_packs(cache)
            packs.extend(new)
            print(f"Gens Ace / Tattu current products: +{len(new):,} pack rows")
        except Exception as exc:
            eprint(f"WARNING Gens Ace / Tattu current products: {exc}")

        try:
            new = import_fpvcompare(cache, prices)
            packs.extend(new)
            print(f"FPVCompare: +{len(new):,} pack reference rows")
        except Exception as exc:
            eprint(f"WARNING FPVCompare: {exc}")

        try:
            new = import_tmotor_official_escs(cache)
            escs.extend(new)
            print(f"T-MOTOR official ESCs: +{len(new):,} ESC rows")
        except Exception as exc:
            eprint(f"WARNING T-MOTOR official ESCs: {exc}")

        try:
            new = import_hobbywing_official_escs(cache)
            escs.extend(new)
            print(f"HOBBYWING official ESCs: +{len(new):,} ESC rows")
        except Exception as exc:
            eprint(f"WARNING HOBBYWING official ESCs: {exc}")

        try:
            new = import_splinecloud_tmotor_escs(cache)
            escs.extend(new)
            print(f"SplineCloud T-MOTOR ESCs: +{len(new):,} ESC rows")
        except Exception as exc:
            eprint(f"WARNING SplineCloud T-MOTOR ESCs: {exc}")

        try:
            new = import_tytorobotics_esc_reference(cache)
            escs.extend(new)
            print(f"Tyto Robotics ESC references: +{len(new):,} ESC rows")
        except Exception as exc:
            eprint(f"WARNING Tyto Robotics ESC database: {exc}")

        if not args.skip_legacy:
            try:
                legacy, sqlite_files = import_usu_legacy(cache, raw)
                motors.extend(legacy["motor"])
                props.extend(legacy["prop"])
                packs.extend(legacy["battery_pack"])
                escs.extend(legacy["esc"])
                print(
                    "USU legacy: "
                    f"+{len(legacy['motor']):,} motors, "
                    f"+{len(legacy['prop']):,} props, "
                    f"+{len(legacy['battery_pack']):,} packs, "
                    f"+{len(legacy['esc']):,} ESCs "
                    f"from {len(sqlite_files)} SQLite file(s)"
                )
            except Exception as exc:
                eprint(f"WARNING USU legacy import: {exc}")

    motors = dedupe_exact(motors)
    props = dedupe_exact(props)
    cells = dedupe_exact(cells)
    packs = dedupe_exact(packs)
    escs = dedupe_exact(escs)
    prices = dedupe_exact(prices)

    # Export detailed source-record tables.
    write_csv(out / "motors_master.csv", motors, MOTOR_FIELDS)
    write_csv(out / "props_master.csv", props, PROP_FIELDS)
    write_csv(out / "battery_cells_master.csv", cells, CELL_FIELDS)
    write_csv(out / "battery_packs_master.csv", packs, PACK_FIELDS)
    write_csv(out / "escs_master.csv", escs, ESC_FIELDS)
    write_csv(out / "prices_master.csv", prices, PRICE_FIELDS)

    # Canonical summaries: these are useful for breadth counts without deleting
    # conflicting source records.
    summaries = {}
    for name, rows in [
        ("motors", motors), ("props", props), ("battery_cells", cells),
        ("battery_packs", packs), ("escs", escs),
    ]:
        s = canonical_summary(rows)
        summaries[name] = s
        fields = [
            "canonical_id","manufacturer","model","best_tier",
            "optimizer_eligible_any","source_records","sources"
        ]
        write_csv(out / f"{name}_canonical_index.csv", s, fields)

    # Source manifest.
    source_fields = [
        "source_name","category","url","license","license_status",
        "quality","enabled_default","notes"
    ]
    write_csv(out / "source_manifest.csv", SOURCES, source_fields)

    # Completeness report.
    entity_map = [
        ("motors", motors, MOTOR_FIELDS),
        ("props", props, PROP_FIELDS),
        ("battery_cells", cells, CELL_FIELDS),
        ("battery_packs", packs, PACK_FIELDS),
        ("escs", escs, ESC_FIELDS),
    ]
    comp_rows = []
    for entity, rows, fields in entity_map:
        for field, (count, pct) in completeness(rows, fields).items():
            comp_rows.append({
                "entity": entity,
                "field": field,
                "present_count": count,
                "total_rows": len(rows),
                "present_pct": round(pct, 2),
            })
    write_csv(
        out / "completeness_report.csv",
        comp_rows,
        ["entity","field","present_count","total_rows","present_pct"],
    )

    # Unified SQLite warehouse for fast querying later.
    db_path = out / "component_warehouse.sqlite"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    sqlite_write_table(con, "motors_master", motors, MOTOR_FIELDS)
    sqlite_write_table(con, "props_master", props, PROP_FIELDS)
    sqlite_write_table(con, "battery_cells_master", cells, CELL_FIELDS)
    sqlite_write_table(con, "battery_packs_master", packs, PACK_FIELDS)
    sqlite_write_table(con, "escs_master", escs, ESC_FIELDS)
    sqlite_write_table(con, "prices_master", prices, PRICE_FIELDS)
    sqlite_write_table(
        con, "source_manifest", SOURCES,
        ["source_name","category","url","license","license_status","quality","enabled_default","notes"]
    )
    con.commit()
    con.close()

    # Human-readable status report.
    targets = {
        "motors": 8000,
        "props": 1000,
        "battery_cells": 500,
        "battery_packs": 1000,
        "escs": 1000,
    }
    counts = {
        "motors": len(summaries["motors"]),
        "props": len(summaries["props"]),
        "battery_cells": len(summaries["battery_cells"]),
        "battery_packs": len(summaries["battery_packs"]),
        "escs": len(summaries["escs"]),
    }
    eligible = {
        "motors": sum(int(r.get("optimizer_eligible") or 0) for r in motors),
        "props": sum(int(r.get("optimizer_eligible") or 0) for r in props),
        "battery_cells": sum(int(r.get("optimizer_eligible") or 0) for r in cells),
        "battery_packs": sum(int(r.get("optimizer_eligible") or 0) for r in packs),
        "escs": sum(int(r.get("optimizer_eligible") or 0) for r in escs),
    }

    report_lines = [
        "COMPONENT DATA WAREHOUSE BUILD REPORT",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "Canonical component counts (source duplicates collapsed):",
    ]
    for k in ("motors","props","battery_cells","battery_packs","escs"):
        report_lines.append(
            f"  {k:15s}: {counts[k]:6,d} / target {targets[k]:6,d} "
            f"({100*counts[k]/targets[k]:5.1f}%) | "
            f"optimizer-eligible source rows: {eligible[k]:6,d}"
        )
    report_lines += [
        "",
        "Interpretation:",
        "  * A large Tier-C count is breadth for discovery, not proof of optimizer readiness.",
        "  * Use optimizer_eligible=1 for immediate engineering calculations.",
        "  * Reverify Tier-C winners from manufacturer sources before purchasing/building.",
        "  * prices_master.csv is deliberately separate because price/stock change rapidly.",
        "  * V3 fixes TUM XLSX parsing, reduces false legacy dedup, and adds current manufacturer pack/ESC sources.",
        "",
        "Key files:",
        "  motors_master.csv",
        "  props_master.csv",
        "  battery_cells_master.csv",
        "  battery_packs_master.csv",
        "  escs_master.csv",
        "  prices_master.csv",
        "  source_manifest.csv",
        "  completeness_report.csv",
        "  component_warehouse.sqlite",
    ]

    # Summarize how much of the weak categories is now usable from current,
    # non-legacy sources.
    current_summary = []
    for entity, rows in [
        ("battery_cells", cells),
        ("battery_packs", packs),
        ("escs", escs),
    ]:
        nonlegacy = [
            r for r in rows
            if "USU PropulsionOptimization legacy" not in str(r.get("source_name", ""))
        ]
        current_summary.append({
            "entity": entity,
            "nonlegacy_rows": len(nonlegacy),
            "nonlegacy_optimizer_eligible": sum(
                int(r.get("optimizer_eligible") or 0) for r in nonlegacy
            ),
            "tier_a_b_rows": sum(
                1 for r in nonlegacy
                if str(r.get("data_quality_tier")) in {"A", "B"}
            ),
        })

    write_csv(
        out / "current_source_readiness.csv",
        current_summary,
        [
            "entity", "nonlegacy_rows",
            "nonlegacy_optimizer_eligible", "tier_a_b_rows"
        ],
    )

    report_path = out / "BUILD_REPORT.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    print("\n" + "=" * 78)
    print("BUILD COMPLETE")
    print("=" * 78)
    for k in ("motors","props","battery_cells","battery_packs","escs"):
        print(
            f"{k:15s}: {counts[k]:6,d} canonical | "
            f"{eligible[k]:6,d} optimizer-eligible source rows | "
            f"target {targets[k]:6,d}"
        )
    print(f"\nWarehouse: {db_path}")
    print(f"Report:    {report_path}")
    print("\nUse Tier A/B + optimizer_eligible=1 for optimization.")
    print("Tier C is discovery/reference until independently verified.")

if __name__ == "__main__":
    main()
