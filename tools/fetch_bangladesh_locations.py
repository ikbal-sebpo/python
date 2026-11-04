#!/usr/bin/env python3
"""Fetch Bangladesh administrative location research data.

This script is intentionally data-first: it does not import into Django models
and does not import into the final Orbis schema. It writes a normalized JSON
artifact that can be reviewed before seeding District/City Corporation,
Thana/Upazila, and Union/Ward values.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ARCGIS_UNION_LAYER_URL = (
    "https://services.arcgis.com/5T5nSi527N4F7luB/ArcGIS/rest/services/"
    "bgd_adm4_union/FeatureServer/0"
)
DGHS_ALL_UNION_LAYER_URL = (
    "https://gis.dghs.gov.bd/server/rest/services/Hosted/All_Union/"
    "FeatureServer/0"
)
DGHS_CODESYSTEM_URLS = (
    "https://fhir.dghs.gov.bd/core/CodeSystem-bd-geocodes.json",
    "https://fhir.dghs.gov.bd/core/CodeSystem-bd-geocodes.json.html",
    "https://fhir.dghs.gov.bd/core/CodeSystem-bd-geocodes.html",
)
PAGE_SIZE = 2000
TIMEOUT_SECONDS = 60

EXPECTED_COUNTS = {
    "divisions": 8,
    "districts": 64,
    "upazilas": 499,
    "unions": 4599,
}
UNION_SOURCE_CHOICES = (
    "arcgis-adm4",
    "dghs-all-union",
    "geocode-complete",
    "none",
)
GEOCODE_COMPLETE_REPO_URL = "https://github.com/nuhil/bangladesh-geocode"
GEOCODE_COMPLETE_FILES = {
    "divisions": (
        Path("divisions/divisions.json"),
        Path("divisions/divisions.csv"),
        ["id", "name", "bn_name", "url"],
    ),
    "districts": (
        Path("districts/districts.json"),
        Path("districts/districts.csv"),
        ["id", "division_id", "name", "bn_name", "lat", "lon", "url"],
    ),
    "upazilas": (
        Path("upazilas/upazilas.json"),
        Path("upazilas/upazilas.csv"),
        ["id", "district_id", "name", "bn_name", "url"],
    ),
    "unions": (
        Path("unions/unions.json"),
        Path("unions/unions.csv"),
        ["id", "upazila_id", "name", "bn_name", "url"],
    ),
}

CITY_CORPORATION_MARKERS = (
    "city corporation",
    "city corperation",
    "city corp.",
)
PAURASHAVA_MARKERS = (
    "paurashav",
    "pourashav",
    "municipality",
    "municipal",
)
PAURASHAVA_CODE_SUFFIXES = ("99", "00")
GENERIC_PAURASHAVA_NAMES = {
    "paurashava",
    "pourashava",
    "paurashav",
    "pourashav",
    "municipality",
    "municipal",
}

KNOWN_DGHS_REVIEW_CODES = {
    "30260022": (
        "Gaffargaon appears with a Dhaka district prefix in the DGHS "
        "CodeSystem; national portal lists it under Mymensingh."
    ),
    "30260023": (
        "Gauripur appears with a Dhaka district prefix in the DGHS "
        "CodeSystem; national portal lists it under Mymensingh."
    ),
}

ADM_FIELDS = (
    "ADM0_EN",
    "ADM0_PCODE",
    "ADM1_EN",
    "ADM1_PCODE",
    "ADM2_EN",
    "ADM2_PCODE",
    "ADM3_EN",
    "ADM3_PCODE",
    "ADM4_EN",
    "ADM4_PCODE",
)


class FetchError(RuntimeError):
    """Raised when a required source cannot be fetched or parsed."""


def clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def request_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "User-Agent": "OrbisLocationResearch/1.0",
        },
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        content_type = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(content_type, errors="replace")


def request_json(url: str) -> dict[str, Any]:
    text = request_text(url)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"Expected JSON from {url}") from exc


def read_json_or_text(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() in {".json", ".geojson"}:
        return json.loads(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def read_csv_rows_without_header(
    path: Path,
    fieldnames: list[str],
) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows: list[dict[str, str]] = []
        for values in csv.reader(handle):
            if not values:
                continue
            rows.append(
                {
                    field: clean(values[index]) if index < len(values) else ""
                    for index, field in enumerate(fieldnames)
                }
            )
        return rows


def feature_layer_query(layer_url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = {"f": "json", **params}
    url = f"{layer_url}/query?{urlencode(query)}"
    data = request_json(url)
    if "error" in data:
        raise FetchError(f"FeatureServer query failed: {data['error']}")
    return data


def arcgis_query(params: dict[str, Any]) -> dict[str, Any]:
    return feature_layer_query(ARCGIS_UNION_LAYER_URL, params)


def normalize_arcgis_record(record: dict[str, Any]) -> dict[str, str]:
    row = {field: clean(record.get(field)) for field in ADM_FIELDS}
    if any(row[field] for field in ("ADM1_EN", "ADM2_EN", "ADM3_EN", "ADM4_EN")):
        return row

    # DGHS All_Union exports use friendly lowercase field names instead of
    # ADM1/ADM2/ADM3/ADM4 aliases.
    pcode = clean(record.get("adm4_pcode") or record.get("ADM4_PCODE"))
    row["ADM0_EN"] = "Bangladesh"
    row["ADM0_PCODE"] = "BD"
    row["ADM1_EN"] = clean(record.get("division"))
    row["ADM2_EN"] = clean(record.get("district"))
    row["ADM3_EN"] = clean(record.get("upazila"))
    row["ADM4_EN"] = clean(record.get("union_") or record.get("union"))
    row["ADM4_PCODE"] = pcode
    if pcode and pcode.isdigit():
        row["ADM1_PCODE"] = pcode[:2]
        row["ADM2_PCODE"] = pcode[:4]
        row["ADM3_PCODE"] = pcode[:6]
    return row


def read_arcgis_union_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FetchError(f"Union source file does not exist: {path}")

    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [normalize_arcgis_record(row) for row in csv.DictReader(handle)]

    data = read_json_or_text(path)
    if isinstance(data, str):
        raise FetchError(f"Unsupported union source file format: {path}")

    if isinstance(data, list):
        return [normalize_arcgis_record(row) for row in data]

    features = data.get("features")
    if not isinstance(features, list):
        raise FetchError(
            "Union source JSON must contain a 'features' array, a GeoJSON "
            "'features' array, or be a list of attribute rows."
        )

    rows: list[dict[str, str]] = []
    for feature in features:
        attrs = feature.get("attributes") or feature.get("properties") or feature
        rows.append(normalize_arcgis_record(attrs))
    return rows


def fetch_arcgis_union_rows(page_size: int = PAGE_SIZE) -> list[dict[str, str]]:
    count_data = arcgis_query(
        {
            "where": "1=1",
            "returnCountOnly": "true",
            "returnGeometry": "false",
        }
    )
    total_count = int(count_data.get("count", 0))
    rows: list[dict[str, str]] = []
    offset = 0

    while offset < total_count:
        data = arcgis_query(
            {
                "where": "1=1",
                "outFields": ",".join(ADM_FIELDS),
                "returnGeometry": "false",
                "orderByFields": "ADM1_EN,ADM2_EN,ADM3_EN,ADM4_EN",
                "resultOffset": offset,
                "resultRecordCount": page_size,
            }
        )
        features = data.get("features", [])
        if not features:
            break
        for feature in features:
            attrs = feature.get("attributes", {})
            rows.append(normalize_arcgis_record(attrs))
        offset += len(features)

    if total_count and len(rows) != total_count:
        raise FetchError(f"ArcGIS returned {len(rows)} rows; expected {total_count}")
    return rows


def fetch_dghs_all_union_rows(page_size: int = PAGE_SIZE) -> list[dict[str, str]]:
    count_data = feature_layer_query(
        DGHS_ALL_UNION_LAYER_URL,
        {
            "where": "1=1",
            "returnCountOnly": "true",
            "returnGeometry": "false",
        },
    )
    total_count = int(count_data.get("count", 0))
    rows: list[dict[str, str]] = []
    offset = 0

    while offset < total_count:
        data = feature_layer_query(
            DGHS_ALL_UNION_LAYER_URL,
            {
                "where": "1=1",
                "outFields": "adm4_pcode,division,district,upazila,union_",
                "returnGeometry": "false",
                "orderByFields": "division,district,upazila,union_",
                "resultOffset": offset,
                "resultRecordCount": page_size,
            },
        )
        features = data.get("features", [])
        if not features:
            break
        for feature in features:
            attrs = feature.get("attributes", {})
            rows.append(normalize_arcgis_record(attrs))
        offset += len(features)

    if total_count and len(rows) != total_count:
        raise FetchError(f"DGHS All_Union returned {len(rows)} rows; expected {total_count}")
    return rows


def unique_rows(rows: list[dict[str, str]], keys: tuple[str, ...]) -> list[dict[str, str]]:
    seen: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        identity = tuple(row.get(key, "") for key in keys)
        if all(identity):
            seen.setdefault(identity, {key: row.get(key, "") for key in keys})
    return sorted(seen.values(), key=lambda item: tuple(item[key] for key in keys))


def comparison_name(value: str) -> str:
    value = value.lower().replace("&", "and")
    value = value.replace("corperation", "corporation")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return clean(value)


def pcode_digits(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def adm3_pcode_to_dghs_thana_code(value: str) -> str:
    digits = pcode_digits(value)
    if len(digits) == 8:
        return digits
    if len(digits) == 6:
        return f"{digits[:4]}00{digits[4:]}"
    return ""


def summarize_union_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    divisions = unique_rows(rows, ("ADM1_PCODE", "ADM1_EN"))
    districts = unique_rows(rows, ("ADM1_PCODE", "ADM1_EN", "ADM2_PCODE", "ADM2_EN"))
    thanas = unique_rows(
        rows,
        ("ADM1_PCODE", "ADM1_EN", "ADM2_PCODE", "ADM2_EN", "ADM3_PCODE", "ADM3_EN"),
    )
    unions = unique_rows(
        rows,
        (
            "ADM1_PCODE",
            "ADM1_EN",
            "ADM2_PCODE",
            "ADM2_EN",
            "ADM3_PCODE",
            "ADM3_EN",
            "ADM4_PCODE",
            "ADM4_EN",
        ),
    )
    return {
        "counts": {
            "division_rows": len(divisions),
            "district_rows": len(districts),
            "thana_upazila_rows": len(thanas),
            "union_rows": len(unions),
            "source_rows": len(rows),
        },
        "divisions": divisions,
        "districts": districts,
        "thanas_upazilas": thanas,
        "unions": unions,
        "normalized_source_rows": rows,
    }


def strip_tags(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", html))


def parse_codesystem_html(text: str) -> list[dict[str, str]]:
    concepts: list[dict[str, str]] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.IGNORECASE | re.DOTALL):
        cells = [
            clean(strip_tags(cell))
            for cell in re.findall(
                r"<t[dh][^>]*>(.*?)</t[dh]>",
                row_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
        ]
        for index, cell in enumerate(cells[:-1]):
            if re.fullmatch(r"\d{2,10}", cell):
                display = clean(cells[index + 1])
                if display and display.lower() != "display":
                    concepts.append({"code": cell, "display": display})
                    break
    if concepts:
        return concepts

    plain = clean(strip_tags(text))
    pattern = re.compile(r"\b(\d{2,10})\s+([A-Za-z][A-Za-z0-9 .'\-/&()]+)")
    for code, display in pattern.findall(plain):
        concepts.append({"code": code, "display": clean(display)})
    return concepts


def extract_concepts_from_json(data: dict[str, Any]) -> list[dict[str, str]]:
    concepts = data.get("concept", [])
    return [
        {"code": clean(item.get("code")), "display": clean(item.get("display"))}
        for item in concepts
        if clean(item.get("code")) and clean(item.get("display"))
    ]


def fetch_dghs_concepts() -> tuple[str, list[dict[str, str]]]:
    errors: list[str] = []
    for url in DGHS_CODESYSTEM_URLS:
        try:
            text = request_text(url)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                concepts = parse_codesystem_html(text)
            else:
                concepts = extract_concepts_from_json(data)
            if concepts:
                return url, concepts
            errors.append(f"{url}: no concepts found")
        except Exception as exc:  # noqa: BLE001 - report all source failures.
            errors.append(f"{url}: {exc}")
    joined = "\n".join(errors)
    raise FetchError(f"Could not fetch DGHS code concepts:\n{joined}")


def read_dghs_concepts(path: Path) -> tuple[str, list[dict[str, str]]]:
    if not path.exists():
        raise FetchError(f"DGHS source file does not exist: {path}")

    data = read_json_or_text(path)
    if isinstance(data, str):
        concepts = parse_codesystem_html(data)
    elif isinstance(data, dict):
        concepts = extract_concepts_from_json(data)
    else:
        raise FetchError(f"Unsupported DGHS source file format: {path}")

    if not concepts:
        raise FetchError(f"No DGHS concepts found in {path}")
    return str(path), concepts


def is_city_corporation(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in CITY_CORPORATION_MARKERS)


def has_paurashava_marker(name: str) -> bool:
    normalized = comparison_name(name)
    return any(marker in normalized for marker in PAURASHAVA_MARKERS)


def is_paurashava_adm4(row: dict[str, str]) -> bool:
    if has_paurashava_marker(row.get("ADM4_EN", "")):
        return True
    code = pcode_digits(row.get("ADM4_PCODE", ""))
    return len(code) >= 2 and code.endswith(PAURASHAVA_CODE_SUFFIXES)


def paurashava_display_name(row: dict[str, str]) -> str:
    name = clean(row.get("ADM4_EN", ""))
    if not is_paurashava_adm4(row):
        return name
    parent_name = clean(row.get("ADM3_EN", ""))
    if comparison_name(name) in GENERIC_PAURASHAVA_NAMES and parent_name:
        name = parent_name
    if comparison_name(name).endswith(" paurashav"):
        name = f"{name}a"
    if name and not has_paurashava_marker(name):
        return f"{name} Paurashava"
    return name


def classify_dghs(concepts: list[dict[str, str]]) -> dict[str, Any]:
    by_code = {item["code"]: item["display"] for item in concepts}
    divisions = [
        {"code": code, "name": name}
        for code, name in by_code.items()
        if len(code) == 2
    ]
    districts = [
        {
            "code": code,
            "name": name,
            "division_code": code[:2],
            "division": by_code.get(code[:2], ""),
        }
        for code, name in by_code.items()
        if len(code) == 4
    ]
    local_authorities = [
        {
            "code": code,
            "name": name,
            "district_code": code[:4],
            "district": by_code.get(code[:4], ""),
            "division_code": code[:2],
            "division": by_code.get(code[:2], ""),
            "kind": "city_corporation" if is_city_corporation(name) else "district_na",
        }
        for code, name in by_code.items()
        if len(code) == 6
    ]

    city_corporation_codes = {
        item["code"]
        for item in local_authorities
        if item["kind"] == "city_corporation"
    }
    district_upazilas: list[dict[str, str]] = []
    city_corporation_thanas: list[dict[str, str]] = []
    orphan_thanas: list[dict[str, str]] = []

    for code, name in by_code.items():
        if len(code) != 8:
            continue
        parent_code = code[:6]
        district_code = code[:4]
        base = {
            "code": code,
            "name": name,
            "parent_code": parent_code,
            "parent": by_code.get(parent_code, ""),
            "district_code": district_code,
            "district": by_code.get(district_code, ""),
            "division_code": code[:2],
            "division": by_code.get(code[:2], ""),
        }
        if parent_code in city_corporation_codes:
            city_corporation_thanas.append(base)
        elif parent_code in by_code:
            district_upazilas.append(base)
        else:
            orphan_thanas.append(base)

    sort_key = lambda item: (item.get("division", ""), item.get("district", ""), item["name"])
    return {
        "counts": {
            "divisions": len(divisions),
            "districts": len(districts),
            "local_authorities": len(local_authorities),
            "city_corporations": len(city_corporation_codes),
            "district_upazilas": len(district_upazilas),
            "city_corporation_thanas": len(city_corporation_thanas),
            "orphan_thanas": len(orphan_thanas),
            "all_concepts": len(concepts),
        },
        "divisions": sorted(divisions, key=lambda item: item["code"]),
        "districts": sorted(districts, key=lambda item: item["code"]),
        "local_authorities": sorted(local_authorities, key=lambda item: item["code"]),
        "city_corporations": sorted(
            [item for item in local_authorities if item["kind"] == "city_corporation"],
            key=lambda item: item["code"],
        ),
        "district_upazilas": sorted(district_upazilas, key=sort_key),
        "city_corporation_thanas": sorted(city_corporation_thanas, key=sort_key),
        "orphan_thanas": sorted(orphan_thanas, key=sort_key),
    }


def build_dghs_review_flags(dghs: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    code_to_name: dict[str, str] = {}

    for key in (
        "divisions",
        "districts",
        "local_authorities",
        "district_upazilas",
        "city_corporation_thanas",
        "orphan_thanas",
    ):
        for row in dghs.get(key, []):
            code = row.get("code")
            name = row.get("name")
            if code and name:
                code_to_name[code] = name

    for code, note in KNOWN_DGHS_REVIEW_CODES.items():
        if code in code_to_name:
            flags.append(f"{code} {code_to_name[code]}: {note}")

    for code, name in code_to_name.items():
        if "corperation" in name.lower():
            flags.append(f"{code} {name}: source spelling says 'Corperation'.")

    return flags


def find_missing_required_fields(
    rows: list[dict[str, str]],
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        row_missing = [field for field in fields if not row.get(field)]
        if row_missing:
            missing.append(
                {
                    "row_number": index,
                    "missing_fields": row_missing,
                    "row": row,
                }
            )
    return missing


def find_duplicate_groups(
    rows: list[dict[str, str]],
    keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in rows:
        identity = tuple(row.get(key, "") for key in keys)
        if all(identity):
            grouped.setdefault(identity, []).append(row)

    duplicates: list[dict[str, Any]] = []
    for identity, group_rows in grouped.items():
        if len(group_rows) > 1:
            duplicates.append(
                {
                    "key": {key: identity[index] for index, key in enumerate(keys)},
                    "count": len(group_rows),
                    "rows": group_rows,
                }
            )
    return sorted(
        duplicates,
        key=lambda item: tuple(item["key"][key] for key in keys),
    )


def dghs_thana_key(row: dict[str, str]) -> tuple[str, str]:
    return (
        comparison_name(row.get("district", "")),
        comparison_name(row.get("name", "")),
    )


def union_thana_key(row: dict[str, str]) -> tuple[str, str]:
    return (
        comparison_name(row.get("ADM2_EN", "")),
        comparison_name(row.get("ADM3_EN", "")),
    )


def union_thana_identity(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        row.get("ADM2_PCODE", ""),
        row.get("ADM2_EN", ""),
        row.get("ADM3_PCODE", ""),
        row.get("ADM3_EN", ""),
    )


def union_thana_lookup_keys(row: dict[str, str]) -> list[tuple[str, ...]]:
    keys: list[tuple[str, ...]] = []
    dghs_code = adm3_pcode_to_dghs_thana_code(row.get("ADM3_PCODE", ""))
    if dghs_code:
        keys.append(("code", dghs_code))
    parent_name = comparison_name(row.get("ADM2_EN", ""))
    thana_name = comparison_name(row.get("ADM3_EN", ""))
    if parent_name and thana_name:
        keys.append(("parent_thana", parent_name, thana_name))
    return keys


def dghs_thana_lookup_keys(
    row: dict[str, str],
    *,
    parent_kind: str | None = None,
    parent_name: str | None = None,
) -> list[tuple[str, ...]]:
    keys: list[tuple[str, ...]] = []
    code = row.get("code", "")
    if code:
        keys.append(("code", code))
    district = comparison_name(row.get("district", ""))
    name = comparison_name(row.get("name", ""))
    if district and name:
        keys.append(("parent_thana", district, name))
    if parent_name and name:
        keys.append(("parent_thana", comparison_name(parent_name), name))
    if parent_kind == "city_corporation" and district and name:
        # Several ADM4 rows keep city-corporation wards under the district
        # parent and thana name, while DGHS scopes that thana under the city
        # corporation. The district+thana key lets those rows attach without
        # merging city and non-city City rows.
        keys.append(("city_district_thana", district, name))
    return keys


def find_union_thana_alignment(
    union_hierarchy: dict[str, Any],
    dghs: dict[str, Any],
) -> dict[str, Any]:
    union_thanas = union_hierarchy["thanas_upazilas"]
    union_by_key: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in union_thanas:
        for key in union_thana_lookup_keys(row):
            union_by_key.setdefault(key, []).append(row)

    dghs_district_upazilas = dghs["district_upazilas"]
    dghs_district_by_key: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in dghs_district_upazilas:
        for key in dghs_thana_lookup_keys(row):
            dghs_district_by_key.setdefault(key, []).append(row)

    union_thanas_missing_in_dghs = [
        row
        for row in union_thanas
        if not any(key in dghs_district_by_key for key in union_thana_lookup_keys(row))
    ]
    dghs_district_upazilas_without_union_rows = [
        row
        for row in dghs_district_upazilas
        if not any(key in union_by_key for key in dghs_thana_lookup_keys(row))
    ]

    city_collision_rows: list[dict[str, Any]] = []
    for city_row in dghs["city_corporation_thanas"]:
        district_key = dghs_thana_key(city_row)
        for district_row in dghs_district_by_key.get(district_key, []):
            city_collision_rows.append(
                {
                    "district": city_row.get("district", ""),
                    "name": city_row.get("name", ""),
                    "city_corporation": city_row.get("parent", ""),
                    "city_corporation_thana_code": city_row.get("code", ""),
                    "district_upazila_parent": district_row.get("parent", ""),
                    "district_upazila_code": district_row.get("code", ""),
                }
            )

    return {
        "counts": {
            "selected_union_thanas": len(union_thanas),
            "dghs_district_upazilas": len(dghs_district_upazilas),
            "dghs_city_corporation_thanas": len(dghs["city_corporation_thanas"]),
            "selected_union_thanas_missing_in_dghs_district_upazilas": len(
                union_thanas_missing_in_dghs
            ),
            "dghs_district_upazilas_without_union_rows": len(
                dghs_district_upazilas_without_union_rows
            ),
            "city_corporation_thana_name_collisions_with_district_upazilas": len(
                city_collision_rows
            ),
        },
        "selected_union_thanas_missing_in_dghs_district_upazilas": (
            union_thanas_missing_in_dghs
        ),
        "dghs_district_upazilas_without_union_rows": (
            dghs_district_upazilas_without_union_rows
        ),
        "city_corporation_thana_name_collisions_with_district_upazilas": (
            city_collision_rows
        ),
    }


def build_validation_details(
    union_hierarchy: dict[str, Any],
    dghs: dict[str, Any],
) -> dict[str, Any]:
    union_source_rows = union_hierarchy.get(
        "normalized_source_rows",
        union_hierarchy["unions"],
    )
    missing_union_fields = find_missing_required_fields(
        union_source_rows,
        (
            "ADM1_EN",
            "ADM1_PCODE",
            "ADM2_EN",
            "ADM2_PCODE",
            "ADM3_EN",
            "ADM3_PCODE",
            "ADM4_EN",
            "ADM4_PCODE",
        ),
    )
    duplicate_unions = find_duplicate_groups(
        union_source_rows,
        ("ADM2_PCODE", "ADM3_PCODE", "ADM4_PCODE", "ADM4_EN"),
    )
    duplicate_union_names_under_same_thana = find_duplicate_groups(
        union_source_rows,
        ("ADM2_EN", "ADM3_EN", "ADM4_EN"),
    )
    duplicate_dghs_district_upazilas = find_duplicate_groups(
        dghs["district_upazilas"],
        ("district", "name"),
    )
    duplicate_dghs_city_corporation_thanas = find_duplicate_groups(
        dghs["city_corporation_thanas"],
        ("parent", "name"),
    )
    alignment = find_union_thana_alignment(union_hierarchy, dghs)

    return {
        "counts": {
            "union_rows_missing_required_fields": len(missing_union_fields),
            "duplicate_union_code_groups": len(duplicate_unions),
            "duplicate_union_name_groups_under_same_thana": len(
                duplicate_union_names_under_same_thana
            ),
            "duplicate_dghs_district_upazila_name_groups": len(
                duplicate_dghs_district_upazilas
            ),
            "duplicate_dghs_city_corporation_thana_name_groups": len(
                duplicate_dghs_city_corporation_thanas
            ),
            **alignment["counts"],
        },
        "union_rows_missing_required_fields": missing_union_fields,
        "duplicate_union_code_groups": duplicate_unions,
        "duplicate_union_name_groups_under_same_thana": (
            duplicate_union_names_under_same_thana
        ),
        "duplicate_dghs_district_upazila_name_groups": (
            duplicate_dghs_district_upazilas
        ),
        "duplicate_dghs_city_corporation_thana_name_groups": (
            duplicate_dghs_city_corporation_thanas
        ),
        "thana_alignment": alignment,
    }


def build_validation(
    union_hierarchy: dict[str, Any],
    dghs: dict[str, Any],
    *,
    union_source_label: str,
    validation_details: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    union_counts = union_hierarchy["counts"]
    dghs_counts = dghs["counts"]
    detail_counts = validation_details["counts"]

    if union_source_label == "none":
        warnings.append(
            "Union hierarchy fetch skipped; artifact contains DGHS thana/city "
            "corporation codes only."
        )
    else:
        if union_counts["division_rows"] != EXPECTED_COUNTS["divisions"]:
            warnings.append(
                f"{union_source_label} division count "
                f"{union_counts['division_rows']} does not match portal count "
                f"{EXPECTED_COUNTS['divisions']}."
            )
        if union_counts["district_rows"] != EXPECTED_COUNTS["districts"]:
            warnings.append(
                f"{union_source_label} district count "
                f"{union_counts['district_rows']} does not match portal count "
                f"{EXPECTED_COUNTS['districts']}."
            )
        if union_counts["union_rows"] != EXPECTED_COUNTS["unions"]:
            warnings.append(
                f"{union_source_label} union count {union_counts['union_rows']} "
                f"does not match portal count {EXPECTED_COUNTS['unions']}; review "
                "boundary source date before using it as the final seed."
            )
    if dghs_counts["city_corporations"] < 12:
        warnings.append(
            f"DGHS city corporation count {dghs_counts['city_corporations']} is below "
            "the 12 listed by the Election Commission."
        )
    dghs_thana_total = (
        dghs_counts["district_upazilas"]
        + dghs_counts["city_corporation_thanas"]
    )
    if dghs_thana_total < EXPECTED_COUNTS["upazilas"]:
        warnings.append(
            "DGHS thana/upazila count is below the national portal upazila count; "
            "use it for city-corporation separation and cross-check rural upazilas "
            "with the ArcGIS and national portal sources."
        )
    warnings.append(
        "Bogura City Corporation appears on an LGD subordinate-office page but not "
        "in the DGHS city-corporation code list used here; keep it under manual review."
    )
    if detail_counts["union_rows_missing_required_fields"]:
        warnings.append(
            f"{detail_counts['union_rows_missing_required_fields']} union rows "
            "are missing required hierarchy fields."
        )
    if detail_counts["duplicate_union_code_groups"]:
        warnings.append(
            f"{detail_counts['duplicate_union_code_groups']} duplicate union code "
            "groups were found in the selected union source."
        )
    if detail_counts["duplicate_union_name_groups_under_same_thana"]:
        warnings.append(
            f"{detail_counts['duplicate_union_name_groups_under_same_thana']} "
            "duplicate union-name groups were found under the same thana/upazila."
        )
    if detail_counts["city_corporation_thana_name_collisions_with_district_upazilas"]:
        warnings.append(
            "Some city-corporation thana names also appear as district upazila "
            "names in the same district; keep these parent contexts distinct."
        )
    warnings.extend(build_dghs_review_flags(dghs))
    return warnings


def build_seed_readiness(
    *,
    union_hierarchy: dict[str, Any],
    dghs: dict[str, Any],
    validation_details: dict[str, Any],
    union_source_label: str,
) -> dict[str, Any]:
    blockers: list[str] = []
    manual_review: list[str] = []
    union_counts = union_hierarchy["counts"]
    dghs_counts = dghs["counts"]
    detail_counts = validation_details["counts"]

    if union_source_label == "none":
        blockers.append("No union hierarchy source was selected.")
    if union_source_label == "Bangladesh GeoCode complete local source":
        blockers.append(
            "Selected union hierarchy is the community Bangladesh GeoCode "
            "fallback; use it for cross-checking, not as the final authority."
        )

    expected_union_counts = {
        "division_rows": "divisions",
        "district_rows": "districts",
        "union_rows": "unions",
    }
    for actual_key, expected_key in expected_union_counts.items():
        actual = union_counts[actual_key]
        expected = EXPECTED_COUNTS[expected_key]
        if actual != expected:
            blockers.append(
                f"Union hierarchy {actual_key} is {actual}; expected {expected}."
            )

    if dghs_counts["divisions"] != EXPECTED_COUNTS["divisions"]:
        blockers.append(
            f"DGHS division count is {dghs_counts['divisions']}; expected "
            f"{EXPECTED_COUNTS['divisions']}."
        )
    if dghs_counts["districts"] != EXPECTED_COUNTS["districts"]:
        blockers.append(
            f"DGHS district count is {dghs_counts['districts']}; expected "
            f"{EXPECTED_COUNTS['districts']}."
        )
    if dghs_counts["city_corporations"] < 12:
        blockers.append(
            f"DGHS city corporation count is {dghs_counts['city_corporations']}; "
            "expected at least 12 confirmed city corporations."
        )

    dghs_thana_total = (
        dghs_counts["district_upazilas"]
        + dghs_counts["city_corporation_thanas"]
    )
    if dghs_thana_total < EXPECTED_COUNTS["upazilas"]:
        blockers.append(
            f"DGHS district/city thana total is {dghs_thana_total}; expected "
            f"at least {EXPECTED_COUNTS['upazilas']} upazilas before adding "
            "city-corporation thanas."
        )

    blocking_detail_count_keys = (
        "union_rows_missing_required_fields",
        "duplicate_union_code_groups",
        "duplicate_union_name_groups_under_same_thana",
        "duplicate_dghs_district_upazila_name_groups",
        "duplicate_dghs_city_corporation_thana_name_groups",
    )
    for key in blocking_detail_count_keys:
        if detail_counts[key]:
            blockers.append(f"{key} = {detail_counts[key]}.")

    if detail_counts["selected_union_thanas_missing_in_dghs_district_upazilas"]:
        manual_review.append(
            "Some selected union-source thana/upazila names are missing from "
            "DGHS district upazilas; review spelling/source differences."
        )
    if detail_counts["dghs_district_upazilas_without_union_rows"]:
        manual_review.append(
            "Some DGHS district upazilas have no union rows in the selected "
            "union source; verify whether they are urban-only or source gaps."
        )
    if detail_counts["city_corporation_thana_name_collisions_with_district_upazilas"]:
        manual_review.append(
            "Some city-corporation thana names collide with district upazila "
            "names; preserve parent context during schema/import design."
        )
    manual_review.extend(build_dghs_review_flags(dghs))
    manual_review.append(
        "Bogura City Corporation remains a manual-review candidate until its "
        "operational/electoral status and thana/ward data are confirmed."
    )

    return {
        "status": "blocked" if blockers else "ready_for_schema_decision",
        "blockers": blockers,
        "manual_review": manual_review,
        "criteria": {
            "expected_counts_from_national_portal": EXPECTED_COUNTS,
            "requires_selected_union_source": True,
            "requires_authoritative_union_source": True,
            "requires_no_missing_required_union_fields": True,
            "requires_no_duplicate_union_groups": True,
            "requires_dghs_city_corporation_separation": True,
            "manual_review_still_required_before_import": True,
        },
    }


def build_model_seed_preview(
    union_hierarchy: dict[str, Any],
    dghs: dict[str, Any],
) -> dict[str, Any]:
    state_rows = [
        {
            "state_kind": "district",
            "state_code": row.get("code", ""),
            "state_name": row.get("name", ""),
            "district_code": row.get("code", ""),
            "district": row.get("name", ""),
            "division_code": row.get("division_code", ""),
            "division": row.get("division", ""),
        }
        for row in dghs["districts"]
    ]
    state_rows.extend(
        {
            "state_kind": "city_corporation",
            "state_code": row.get("code", ""),
            "state_name": row.get("name", ""),
            "district_code": row.get("district_code", ""),
            "district": row.get("district", ""),
            "division_code": row.get("division_code", ""),
            "division": row.get("division", ""),
        }
        for row in dghs["city_corporations"]
    )

    city_rows = [
        {
            "state_kind": "district",
            "state_code": row.get("district_code", ""),
            "state_name": row.get("district", ""),
            "city_kind": "upazila_or_thana",
            "city_code": row.get("code", ""),
            "city_name": row.get("name", ""),
            "district_code": row.get("district_code", ""),
            "district": row.get("district", ""),
            "division_code": row.get("division_code", ""),
            "division": row.get("division", ""),
        }
        for row in dghs["district_upazilas"]
    ]
    city_rows.extend(
        {
            "state_kind": "city_corporation",
            "state_code": row.get("parent_code", ""),
            "state_name": row.get("parent", ""),
            "city_kind": "city_corporation_thana",
            "city_code": row.get("code", ""),
            "city_name": row.get("name", ""),
            "district_code": row.get("district_code", ""),
            "district": row.get("district", ""),
            "division_code": row.get("division_code", ""),
            "division": row.get("division", ""),
        }
        for row in dghs["city_corporation_thanas"]
    )
    paurashava_city_rows = [
        {
            "state_kind": "district",
            "state_code": pcode_digits(row.get("ADM2_PCODE", "")),
            "state_name": row.get("ADM2_EN", ""),
            "city_kind": "paurashava",
            "city_code": row.get("ADM4_PCODE", ""),
            "city_name": paurashava_display_name(row),
            "district_code": pcode_digits(row.get("ADM2_PCODE", "")),
            "district": row.get("ADM2_EN", ""),
            "division_code": pcode_digits(row.get("ADM1_PCODE", "")),
            "division": row.get("ADM1_EN", ""),
        }
        for row in union_hierarchy["unions"]
        if is_paurashava_adm4(row)
    ]
    city_rows.extend(paurashava_city_rows)
    union_ward_rows = [
        row
        for row in union_hierarchy["unions"]
        if not is_paurashava_adm4(row)
    ]

    return {
        "current_schema": (
            "Country -> Division -> State(District or City Corporation) -> "
            "City(Upazila, Thana, or Paurashava) -> UnionWard(Union or Ward "
            "No) -> existing detailed address fields. Address.union_name "
            "remains a legacy free-text fallback."
        ),
        "field_label_plan": {
            "state": "District / City Corporation",
            "city": "Upazila / Thana / Paurashava",
            "union_ward": "Union / Ward No",
            "union_name": "Union / Ward No (if not listed)",
            "detailed_address": (
                "Use the existing flat_no, holding_no, village_area, "
                "and post_code fields after Union / Ward No."
            ),
        },
        "notes_before_import": [
            (
                "State intentionally stores either a district or a city "
                "corporation for the current Bangladesh-only schema."
            ),
            (
                "City rows stay scoped to their State parent, so the same thana "
                "name can exist under a district State, a city-corporation "
                "State, or a paurashava City without becoming one shared row."
            ),
            (
                "Paurashava rows are equivalent to upazila/thana selection "
                "level for this address UI. They are imported as City rows "
                "under their district, not as UnionWard rows under an upazila."
            ),
            (
                "Union rows are prepared for district/upazila addresses; city "
                "corporation ward data still needs an authoritative source. "
                "The import command seeds UnionWard rows and keeps "
                "Address.union_name as a free-text fallback."
            ),
        ],
        "country": {"name": "Bangladesh", "iso_code": "BD"},
        "states_as_districts_and_city_corporations": sorted(
            state_rows,
            key=lambda item: (
                item.get("division", ""),
                item.get("district", ""),
                item.get("state_kind", ""),
                item.get("state_name", ""),
            ),
        ),
        "cities_as_upazilas_and_thanas": sorted(
            city_rows,
            key=lambda item: (
                item.get("division", ""),
                item.get("state_name", ""),
                item.get("city_kind", ""),
                item.get("city_name", ""),
            ),
        ),
        "cities_as_paurashavas": sorted(
            paurashava_city_rows,
            key=lambda item: (
                item.get("division", ""),
                item.get("state_name", ""),
                item.get("city_name", ""),
            ),
        ),
        "union_or_ward_rows_for_unionward": union_ward_rows,
        # Legacy split keys kept so older review notes/scripts remain readable.
        "states_as_districts": dghs["districts"],
        "cities_as_district_thanas": dghs["district_upazilas"],
        "city_corporations_as_states": dghs["city_corporations"],
        "city_corporation_thanas_as_cities": dghs["city_corporation_thanas"],
        "unions_for_unionward": union_ward_rows,
    }


def union_parent_thana_key(parent_name: str, thana_name: str) -> tuple[str, str]:
    return (comparison_name(parent_name), comparison_name(thana_name))


def build_union_lookup(
    union_hierarchy: dict[str, Any],
) -> dict[tuple[str, ...], list[dict[str, str]]]:
    lookup: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for union in union_hierarchy["unions"]:
        for key in union_thana_lookup_keys(union):
            lookup.setdefault(key, []).append(union)
    return lookup


def unique_union_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        identity = (
            row.get("ADM3_PCODE", ""),
            row.get("ADM4_PCODE", ""),
            row.get("ADM4_EN", ""),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(row)
    return unique


def lookup_union_rows(
    lookup: dict[tuple[str, ...], list[dict[str, str]]],
    keys: list[tuple[str, ...]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for key in keys:
        rows.extend(lookup.get(key, []))
    return unique_union_rows(rows)


def build_hierarchy_thana_row(
    *,
    source_row: dict[str, str],
    parent_kind: str,
    parent_code: str,
    parent_name: str,
    union_rows: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "code": source_row.get("code", ""),
        "name": source_row.get("name", ""),
        "parent_kind": parent_kind,
        "parent_code": parent_code,
        "parent_name": parent_name,
        "district_code": source_row.get("district_code", ""),
        "district": source_row.get("district", ""),
        "division_code": source_row.get("division_code", ""),
        "division": source_row.get("division", ""),
        "union_count": len(union_rows),
        "unions": union_rows,
    }


def build_review_hierarchy(
    union_hierarchy: dict[str, Any],
    dghs: dict[str, Any],
) -> dict[str, Any]:
    union_lookup = build_union_lookup(union_hierarchy)
    matched_union_thanas: set[tuple[str, str, str, str]] = set()
    parents: list[dict[str, Any]] = []

    upazilas_by_district_code: dict[str, list[dict[str, str]]] = {}
    for row in dghs["district_upazilas"]:
        upazilas_by_district_code.setdefault(row.get("district_code", ""), []).append(row)

    city_thanas_by_parent_code: dict[str, list[dict[str, str]]] = {}
    for row in dghs["city_corporation_thanas"]:
        city_thanas_by_parent_code.setdefault(row.get("parent_code", ""), []).append(row)

    for district in dghs["districts"]:
        parent_name = district.get("name", "")
        thana_rows: list[dict[str, Any]] = []
        for thana in sorted(
            upazilas_by_district_code.get(district.get("code", ""), []),
            key=lambda item: item.get("name", ""),
        ):
            union_rows = lookup_union_rows(
                union_lookup,
                dghs_thana_lookup_keys(
                    thana,
                    parent_kind="district",
                    parent_name=parent_name,
                ),
            )
            for union_row in union_rows:
                matched_union_thanas.add(union_thana_identity(union_row))
            thana_rows.append(
                build_hierarchy_thana_row(
                    source_row=thana,
                    parent_kind="district",
                    parent_code=district.get("code", ""),
                    parent_name=parent_name,
                    union_rows=union_rows,
                )
            )
        parents.append(
            {
                "parent_kind": "district",
                "parent_code": district.get("code", ""),
                "parent_name": parent_name,
                "district_code": district.get("code", ""),
                "district": parent_name,
                "division_code": district.get("division_code", ""),
                "division": district.get("division", ""),
                "thana_count": len(thana_rows),
                "union_count": sum(row["union_count"] for row in thana_rows),
                "thanas": thana_rows,
            }
        )

    for city in dghs["city_corporations"]:
        parent_name = city.get("name", "")
        thana_rows = []
        for thana in sorted(
            city_thanas_by_parent_code.get(city.get("code", ""), []),
            key=lambda item: item.get("name", ""),
        ):
            union_rows = lookup_union_rows(
                union_lookup,
                dghs_thana_lookup_keys(
                    thana,
                    parent_kind="city_corporation",
                    parent_name=parent_name,
                ),
            )
            for union_row in union_rows:
                matched_union_thanas.add(union_thana_identity(union_row))
            thana_rows.append(
                build_hierarchy_thana_row(
                    source_row=thana,
                    parent_kind="city_corporation",
                    parent_code=city.get("code", ""),
                    parent_name=parent_name,
                    union_rows=union_rows,
                )
            )
        parents.append(
            {
                "parent_kind": "city_corporation",
                "parent_code": city.get("code", ""),
                "parent_name": parent_name,
                "district_code": city.get("district_code", ""),
                "district": city.get("district", ""),
                "division_code": city.get("division_code", ""),
                "division": city.get("division", ""),
                "thana_count": len(thana_rows),
                "union_count": sum(row["union_count"] for row in thana_rows),
                "thanas": thana_rows,
            }
        )

    unmatched_union_thanas = [
        row
        for row in union_hierarchy["thanas_upazilas"]
        if union_thana_identity(row) not in matched_union_thanas
    ]

    district_parents = [
        parent for parent in parents if parent["parent_kind"] == "district"
    ]
    city_corporation_parents = [
        parent for parent in parents if parent["parent_kind"] == "city_corporation"
    ]
    thana_rows = [thana for parent in parents for thana in parent["thanas"]]

    return {
        "counts": {
            "district_parents": len(district_parents),
            "city_corporation_parents": len(city_corporation_parents),
            "total_parents": len(parents),
            "thanas_under_districts": sum(
                parent["thana_count"] for parent in district_parents
            ),
            "thanas_under_city_corporations": sum(
                parent["thana_count"] for parent in city_corporation_parents
            ),
            "total_thanas": len(thana_rows),
            "union_rows_attached_to_district_thanas": sum(
                parent["union_count"] for parent in district_parents
            ),
            "union_rows_attached_to_city_corporation_thanas": sum(
                parent["union_count"] for parent in city_corporation_parents
            ),
            "selected_union_thanas_not_attached_to_review_hierarchy": len(
                unmatched_union_thanas
            ),
        },
        "parents": sorted(
            parents,
            key=lambda item: (
                item.get("division", ""),
                item.get("district", ""),
                item.get("parent_kind", ""),
                item.get("parent_name", ""),
            ),
        ),
        "unmatched_union_thanas": unmatched_union_thanas,
    }


def flatten_review_hierarchy_thanas(
    review_hierarchy: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for parent in review_hierarchy["parents"]:
        for thana in parent["thanas"]:
            rows.append(
                {
                    "parent_kind": parent["parent_kind"],
                    "parent_code": parent["parent_code"],
                    "parent_name": parent["parent_name"],
                    "division_code": parent["division_code"],
                    "division": parent["division"],
                    "district_code": parent["district_code"],
                    "district": parent["district"],
                    "thana_code": thana["code"],
                    "thana_name": thana["name"],
                    "union_count": thana["union_count"],
                }
            )
    return rows


def flatten_review_hierarchy_unions(
    review_hierarchy: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for parent in review_hierarchy["parents"]:
        for thana in parent["thanas"]:
            for union in thana["unions"]:
                rows.append(
                    {
                        "parent_kind": parent["parent_kind"],
                        "parent_code": parent["parent_code"],
                        "parent_name": parent["parent_name"],
                        "division_code": parent["division_code"],
                        "division": parent["division"],
                        "district_code": parent["district_code"],
                        "district": parent["district"],
                        "thana_code": thana["code"],
                        "thana_name": thana["name"],
                        "union_code": union.get("ADM4_PCODE", ""),
                        "union_name": union.get("ADM4_EN", ""),
                        "source_division_code": union.get("ADM1_PCODE", ""),
                        "source_division": union.get("ADM1_EN", ""),
                        "source_parent_code": union.get("ADM2_PCODE", ""),
                        "source_parent": union.get("ADM2_EN", ""),
                        "source_thana_code": union.get("ADM3_PCODE", ""),
                        "source_thana": union.get("ADM3_EN", ""),
                    }
                )
    return rows


def extract_phpmyadmin_rows(data: Any, table_name: str) -> list[dict[str, str]]:
    if not isinstance(data, list):
        raise FetchError(f"{table_name} JSON must be a phpMyAdmin export list")

    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "table" or item.get("name") != table_name:
            continue
        rows = item.get("data")
        if not isinstance(rows, list):
            raise FetchError(f"{table_name} JSON table entry has no data array")
        return [
            {str(key): clean(value) for key, value in row.items()}
            for row in rows
            if isinstance(row, dict)
        ]

    raise FetchError(f"{table_name} JSON table entry not found")


def load_geocode_table(source_dir: Path, table_name: str) -> list[dict[str, str]]:
    json_path, csv_path, fieldnames = GEOCODE_COMPLETE_FILES[table_name]
    full_json_path = source_dir / json_path
    full_csv_path = source_dir / csv_path

    if full_json_path.exists():
        return extract_phpmyadmin_rows(
            read_json_or_text(full_json_path),
            table_name,
        )
    if full_csv_path.exists():
        return read_csv_rows_without_header(full_csv_path, fieldnames)

    raise FetchError(
        f"{source_dir} is missing {json_path} or {csv_path} for {table_name}."
    )


def build_geocode_complete_union_rows(
    divisions: list[dict[str, str]],
    districts: list[dict[str, str]],
    upazilas: list[dict[str, str]],
    unions: list[dict[str, str]],
) -> list[dict[str, str]]:
    divisions_by_id = {row["id"]: row for row in divisions if row.get("id")}
    districts_by_id = {row["id"]: row for row in districts if row.get("id")}
    upazilas_by_id = {row["id"]: row for row in upazilas if row.get("id")}
    rows: list[dict[str, str]] = []

    for union in unions:
        upazila = upazilas_by_id.get(union.get("upazila_id", ""))
        if not upazila:
            continue
        district = districts_by_id.get(upazila.get("district_id", ""))
        if not district:
            continue
        division = divisions_by_id.get(district.get("division_id", ""))
        if not division:
            continue

        division_id = division.get("id", "")
        district_id = district.get("id", "")
        upazila_id = upazila.get("id", "")
        union_id = union.get("id", "")
        rows.append(
            {
                "ADM0_EN": "Bangladesh",
                "ADM0_PCODE": "BD",
                "ADM1_EN": division.get("name", ""),
                "ADM1_PCODE": f"GC-DIV-{division_id}",
                "ADM2_EN": district.get("name", ""),
                "ADM2_PCODE": f"GC-DIST-{district_id}",
                "ADM3_EN": upazila.get("name", ""),
                "ADM3_PCODE": f"GC-UPZ-{upazila_id}",
                "ADM4_EN": union.get("name", ""),
                "ADM4_PCODE": f"GC-UNI-{union_id}",
            }
        )

    return rows


def load_geocode_complete(source_dir: Path) -> dict[str, Any]:
    divisions = load_geocode_table(source_dir, "divisions")
    districts = load_geocode_table(source_dir, "districts")
    upazilas = load_geocode_table(source_dir, "upazilas")
    unions = load_geocode_table(source_dir, "unions")
    normalized_union_rows = build_geocode_complete_union_rows(
        divisions,
        districts,
        upazilas,
        unions,
    )

    return {
        "source_dir": str(source_dir),
        "source_repo": GEOCODE_COMPLETE_REPO_URL,
        "counts": {
            "divisions": len(divisions),
            "districts": len(districts),
            "upazilas": len(upazilas),
            "unions": len(unions),
            "normalized_union_rows": len(normalized_union_rows),
        },
        "divisions": divisions,
        "districts": districts,
        "upazilas": upazilas,
        "unions": unions,
        "normalized_union_rows": normalized_union_rows,
    }


def load_union_rows(
    *,
    arcgis_file: Path | None,
    geocode_complete_dir: Path | None,
    union_source: str,
) -> tuple[str, str, list[dict[str, str]]]:
    if arcgis_file:
        return (
            "local union source file",
            str(arcgis_file),
            read_arcgis_union_rows(arcgis_file),
        )

    if union_source == "arcgis-adm4":
        return (
            "ArcGIS ADM4 union layer",
            ARCGIS_UNION_LAYER_URL,
            fetch_arcgis_union_rows(),
        )
    if union_source == "dghs-all-union":
        return (
            "DGHS All_Union layer",
            DGHS_ALL_UNION_LAYER_URL,
            fetch_dghs_all_union_rows(),
        )
    if union_source == "geocode-complete":
        if not geocode_complete_dir:
            raise FetchError(
                "--union-source geocode-complete requires --geocode-complete-dir."
            )
        geocode_complete = load_geocode_complete(geocode_complete_dir)
        return (
            "Bangladesh GeoCode complete local source",
            str(geocode_complete_dir),
            geocode_complete["normalized_union_rows"],
        )
    if union_source == "none":
        return "none", "not requested", []

    raise FetchError(
        f"Unsupported union source {union_source!r}; expected one of "
        f"{', '.join(UNION_SOURCE_CHOICES)}."
    )


def load_community_static_json(source_dir: Path) -> dict[str, Any]:
    files = {
        "divisions": source_dir / "divisions.json",
        "districts": source_dir / "districts.json",
        "upazilas": source_dir / "upazilas.json",
        "unions": source_dir / "unions.json",
    }
    missing = [name for name, path in files.items() if not path.exists()]
    if missing:
        raise FetchError(
            f"Community source dir {source_dir} is missing: {', '.join(missing)}"
        )

    rows = {name: read_json_or_text(path) for name, path in files.items()}
    for name, value in rows.items():
        if not isinstance(value, list):
            raise FetchError(f"{files[name]} must contain a JSON array")

    return {
        "source_dir": str(source_dir),
        "counts": {name: len(value) for name, value in rows.items()},
        "divisions": rows["divisions"],
        "districts": rows["districts"],
        "upazilas": rows["upazilas"],
        "unions": rows["unions"],
    }


def build_payload(
    *,
    arcgis_file: Path | None = None,
    dghs_file: Path | None = None,
    community_source_dir: Path | None = None,
    geocode_complete_dir: Path | None = None,
    union_source: str = "arcgis-adm4",
) -> dict[str, Any]:
    union_source_label, union_source_url, union_rows = load_union_rows(
        arcgis_file=arcgis_file,
        geocode_complete_dir=geocode_complete_dir,
        union_source=union_source,
    )
    union_summary = summarize_union_rows(union_rows)
    if dghs_file:
        dghs_source_url, concepts = read_dghs_concepts(dghs_file)
    else:
        dghs_source_url, concepts = fetch_dghs_concepts()

    dghs_summary = classify_dghs(concepts)
    community = (
        load_community_static_json(community_source_dir)
        if community_source_dir
        else None
    )
    geocode_complete = (
        load_geocode_complete(geocode_complete_dir)
        if geocode_complete_dir
        else None
    )

    validation_details = build_validation_details(union_summary, dghs_summary)
    seed_readiness = build_seed_readiness(
        union_hierarchy=union_summary,
        dghs=dghs_summary,
        validation_details=validation_details,
        union_source_label=union_source_label,
    )
    review_hierarchy = build_review_hierarchy(union_summary, dghs_summary)
    payload = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "purpose": "Research artifact only; not a Django fixture.",
            "sources": {
                "national_portal_counts": [
                    "https://bangladesh.gov.bd/views/division-list/",
                    "https://bangladesh.gov.bd/views/district-list",
                    "https://bangladesh.gov.bd/views/upazila-list/Upazilla-List/",
                    "https://bangladesh.gov.bd/views/union-list/",
                ],
                "selected_union_hierarchy": union_source_url,
                "arcgis_adm4_union_layer": ARCGIS_UNION_LAYER_URL,
                "dghs_all_union_layer": DGHS_ALL_UNION_LAYER_URL,
                "dghs_codesystem": dghs_source_url,
                "geocode_complete_repo": GEOCODE_COMPLETE_REPO_URL,
                "lgd_city_corporation_review": (
                    "https://lgd.gov.bd/pages/static-pages/"
                    "69414020c4774958d7b54af5"
                ),
                "ecs_city_corporation_review": (
                    "https://ecs.gov.bd/en/page/city-corporation-election-result"
                ),
            },
            "expected_counts_from_national_portal": EXPECTED_COUNTS,
            "requested_union_source": union_source,
            "resolved_union_source": union_source_label,
            "manual_review": {
                "city_corporation_candidate": "Bogura City Corporation",
                "reason": (
                    "LGD lists it, but DGHS/ECS city-corporation lists used for "
                    "code-based thana separation do not include it."
                ),
            },
        },
        "summary": {
            "union_hierarchy": union_summary["counts"],
            "dghs": dghs_summary["counts"],
        },
        "validation_warnings": build_validation(
            union_summary,
            dghs_summary,
            union_source_label=union_source_label,
            validation_details=validation_details,
        ),
        "validation_details": validation_details,
        "seed_readiness": seed_readiness,
        "review_hierarchy": review_hierarchy,
        "union_hierarchy": union_summary,
        "dghs_geocode_hierarchy": dghs_summary,
        "orbis_current_schema_preview": build_model_seed_preview(
            union_summary,
            dghs_summary,
        ),
    }

    if community:
        payload["summary"]["community_static_json"] = community["counts"]
        payload["community_static_json_crosscheck"] = community
        for key, expected in EXPECTED_COUNTS.items():
            actual = community["counts"].get(key)
            if actual != expected:
                payload["validation_warnings"].append(
                    f"Community static JSON {key} count {actual} does not "
                    f"match national portal count {expected}."
                )

    if geocode_complete:
        payload["summary"]["geocode_complete"] = geocode_complete["counts"]
        payload["geocode_complete_crosscheck"] = geocode_complete
        for key, expected in EXPECTED_COUNTS.items():
            actual = geocode_complete["counts"].get(key)
            if actual != expected:
                payload["validation_warnings"].append(
                    f"Bangladesh GeoCode complete {key} count {actual} does "
                    f"not match national portal count {expected}."
                )
        if (
            geocode_complete["counts"]["normalized_union_rows"]
            != geocode_complete["counts"]["unions"]
        ):
            payload["validation_warnings"].append(
                "Bangladesh GeoCode complete has union rows with missing "
                "upazila/district/division parents; review before using it."
            )

    return payload


def write_csv_rows(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_csv_exports(payload: dict[str, Any], csv_dir: Path) -> list[Path]:
    dghs = payload["dghs_geocode_hierarchy"]
    union_hierarchy = payload["union_hierarchy"]
    validation = payload["validation_details"]
    alignment = validation["thana_alignment"]
    review_hierarchy = payload["review_hierarchy"]
    schema_preview = payload["orbis_current_schema_preview"]

    exports = [
        (
            "dghs_divisions.csv",
            dghs["divisions"],
            ["code", "name"],
        ),
        (
            "dghs_districts.csv",
            dghs["districts"],
            ["code", "name", "division_code", "division"],
        ),
        (
            "dghs_local_authorities.csv",
            dghs["local_authorities"],
            [
                "code",
                "name",
                "kind",
                "district_code",
                "district",
                "division_code",
                "division",
            ],
        ),
        (
            "dghs_district_upazilas.csv",
            dghs["district_upazilas"],
            [
                "code",
                "name",
                "parent_code",
                "parent",
                "district_code",
                "district",
                "division_code",
                "division",
            ],
        ),
        (
            "dghs_city_corporation_thanas.csv",
            dghs["city_corporation_thanas"],
            [
                "code",
                "name",
                "parent_code",
                "parent",
                "district_code",
                "district",
                "division_code",
                "division",
            ],
        ),
        (
            "union_hierarchy_unions.csv",
            union_hierarchy["unions"],
            [
                "ADM1_PCODE",
                "ADM1_EN",
                "ADM2_PCODE",
                "ADM2_EN",
                "ADM3_PCODE",
                "ADM3_EN",
                "ADM4_PCODE",
                "ADM4_EN",
            ],
        ),
        (
            "model_paurashava_cities.csv",
            schema_preview.get("cities_as_paurashavas", []),
            [
                "state_kind",
                "state_code",
                "state_name",
                "city_kind",
                "city_code",
                "city_name",
                "district_code",
                "district",
                "division_code",
                "division",
            ],
        ),
        (
            "review_hierarchy_thanas.csv",
            flatten_review_hierarchy_thanas(review_hierarchy),
            [
                "parent_kind",
                "parent_code",
                "parent_name",
                "division_code",
                "division",
                "district_code",
                "district",
                "thana_code",
                "thana_name",
                "union_count",
            ],
        ),
        (
            "review_hierarchy_unions.csv",
            flatten_review_hierarchy_unions(review_hierarchy),
            [
                "parent_kind",
                "parent_code",
                "parent_name",
                "division_code",
                "division",
                "district_code",
                "district",
                "thana_code",
                "thana_name",
                "union_code",
                "union_name",
                "source_division_code",
                "source_division",
                "source_parent_code",
                "source_parent",
                "source_thana_code",
                "source_thana",
            ],
        ),
        (
            "review_unmatched_union_thanas.csv",
            review_hierarchy["unmatched_union_thanas"],
            [
                "ADM1_PCODE",
                "ADM1_EN",
                "ADM2_PCODE",
                "ADM2_EN",
                "ADM3_PCODE",
                "ADM3_EN",
            ],
        ),
        (
            "validation_union_thanas_missing_in_dghs.csv",
            alignment["selected_union_thanas_missing_in_dghs_district_upazilas"],
            [
                "ADM1_PCODE",
                "ADM1_EN",
                "ADM2_PCODE",
                "ADM2_EN",
                "ADM3_PCODE",
                "ADM3_EN",
            ],
        ),
        (
            "validation_dghs_upazilas_without_union_rows.csv",
            alignment["dghs_district_upazilas_without_union_rows"],
            [
                "code",
                "name",
                "parent_code",
                "parent",
                "district_code",
                "district",
                "division_code",
                "division",
            ],
        ),
        (
            "validation_city_thana_name_collisions.csv",
            alignment[
                "city_corporation_thana_name_collisions_with_district_upazilas"
            ],
            [
                "district",
                "name",
                "city_corporation",
                "city_corporation_thana_code",
                "district_upazila_parent",
                "district_upazila_code",
            ],
        ),
        (
            "validation_union_rows_missing_required_fields.csv",
            [
                {
                    "row_number": row["row_number"],
                    "missing_fields": ", ".join(row["missing_fields"]),
                    **row["row"],
                }
                for row in validation["union_rows_missing_required_fields"]
            ],
            [
                "row_number",
                "missing_fields",
                "ADM1_PCODE",
                "ADM1_EN",
                "ADM2_PCODE",
                "ADM2_EN",
                "ADM3_PCODE",
                "ADM3_EN",
                "ADM4_PCODE",
                "ADM4_EN",
            ],
        ),
    ]

    written: list[Path] = []
    for filename, rows, fieldnames in exports:
        path = csv_dir / filename
        write_csv_rows(path, rows, fieldnames)
        written.append(path)
    return written


def compare_counts(
    *,
    label: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if actual_value != expected_value:
            errors.append(
                f"{label}.{key} is {actual_value!r}; expected {expected_value!r}."
            )
    return errors


def validate_payload_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    required_top_level = (
        "metadata",
        "summary",
        "validation_warnings",
        "validation_details",
        "seed_readiness",
        "review_hierarchy",
        "union_hierarchy",
        "dghs_geocode_hierarchy",
        "orbis_current_schema_preview",
    )
    for key in required_top_level:
        if key not in payload:
            errors.append(f"Missing top-level key: {key}.")
    if errors:
        return {"status": "invalid", "errors": errors, "warnings": warnings}

    union_hierarchy = payload["union_hierarchy"]
    dghs = payload["dghs_geocode_hierarchy"]
    metadata = payload["metadata"]

    required_union_keys = (
        "counts",
        "divisions",
        "districts",
        "thanas_upazilas",
        "unions",
        "normalized_source_rows",
    )
    for key in required_union_keys:
        if key not in union_hierarchy:
            errors.append(f"Missing union_hierarchy key: {key}.")

    required_dghs_keys = (
        "counts",
        "divisions",
        "districts",
        "local_authorities",
        "city_corporations",
        "district_upazilas",
        "city_corporation_thanas",
        "orphan_thanas",
    )
    for key in required_dghs_keys:
        if key not in dghs:
            errors.append(f"Missing dghs_geocode_hierarchy key: {key}.")
    if errors:
        return {"status": "invalid", "errors": errors, "warnings": warnings}

    recomputed_union = summarize_union_rows(union_hierarchy["normalized_source_rows"])
    errors.extend(
        compare_counts(
            label="union_hierarchy.counts",
            expected=recomputed_union["counts"],
            actual=union_hierarchy["counts"],
        )
    )
    summary_union_counts = payload.get("summary", {}).get("union_hierarchy", {})
    errors.extend(
        compare_counts(
            label="summary.union_hierarchy",
            expected=union_hierarchy["counts"],
            actual=summary_union_counts,
        )
    )

    validation_details = build_validation_details(union_hierarchy, dghs)
    errors.extend(
        compare_counts(
            label="validation_details.counts",
            expected=validation_details["counts"],
            actual=payload["validation_details"].get("counts", {}),
        )
    )

    union_source_label = metadata.get("resolved_union_source", "")
    seed_readiness = build_seed_readiness(
        union_hierarchy=union_hierarchy,
        dghs=dghs,
        validation_details=validation_details,
        union_source_label=union_source_label,
    )
    stored_seed_readiness = payload["seed_readiness"]
    if stored_seed_readiness.get("status") != seed_readiness["status"]:
        errors.append(
            "seed_readiness.status is "
            f"{stored_seed_readiness.get('status')!r}; expected "
            f"{seed_readiness['status']!r}."
        )
    if stored_seed_readiness.get("blockers", []) != seed_readiness["blockers"]:
        errors.append("seed_readiness.blockers are stale or inconsistent.")

    review_hierarchy = build_review_hierarchy(union_hierarchy, dghs)
    errors.extend(
        compare_counts(
            label="review_hierarchy.counts",
            expected=review_hierarchy["counts"],
            actual=payload["review_hierarchy"].get("counts", {}),
        )
    )

    if payload["seed_readiness"].get("status") == "blocked":
        warnings.append(
            "Artifact is structurally valid but seed_readiness.status is blocked."
        )

    return {
        "status": "invalid" if errors else "valid",
        "errors": errors,
        "warnings": warnings,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-artifact",
        type=Path,
        help=(
            "Validate an existing generated JSON artifact without fetching any "
            "source data. With --csv-dir, also regenerate review CSVs from the "
            "artifact."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/research/bangladesh_locations.json"),
        help="Path for the normalized research JSON artifact.",
    )
    parser.add_argument(
        "--csv-dir",
        type=Path,
        help=(
            "Optional directory for review CSV exports: DGHS divisions, "
            "districts, local authorities, district upazilas, city corporation "
            "thanas, and selected union rows."
        ),
    )
    parser.add_argument(
        "--arcgis-file",
        "--union-file",
        dest="arcgis_file",
        type=Path,
        help=(
            "Optional local union hierarchy export/query result. Supports CSV, "
            "JSON, GeoJSON, FeatureServer JSON, DGHS All_Union exports, or a "
            "list of ADM attribute rows. Overrides --union-source."
        ),
    )
    parser.add_argument(
        "--union-source",
        choices=UNION_SOURCE_CHOICES,
        default="arcgis-adm4",
        help=(
            "Live union hierarchy source when --arcgis-file/--union-file is not "
            "provided. Use 'dghs-all-union' to test the DGHS hosted layer or "
            "'geocode-complete' with --geocode-complete-dir as an offline "
            "fallback, or 'none' for DGHS thana/city-corporation codes only."
        ),
    )
    parser.add_argument(
        "--dghs-file",
        type=Path,
        help="Optional local DGHS CodeSystem file. Supports JSON or saved HTML.",
    )
    parser.add_argument(
        "--community-source-dir",
        type=Path,
        help=(
            "Optional directory containing divisions.json, districts.json, "
            "upazilas.json, and unions.json for cross-check only."
        ),
    )
    parser.add_argument(
        "--geocode-complete-dir",
        type=Path,
        help=(
            "Optional local checkout/download of nuhil/bangladesh-geocode. "
            "Used for cross-checks, or as the union hierarchy when "
            "--union-source geocode-complete is selected."
        ),
    )
    parser.add_argument(
        "--fail-on-readiness-blockers",
        action="store_true",
        help=(
            "Exit with status 2 when seed_readiness.status is blocked. This is "
            "useful in CI or before treating a generated artifact as ready for "
            "schema/import decisions."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.validate_artifact:
        try:
            payload = read_json_or_text(args.validate_artifact)
            if not isinstance(payload, dict):
                raise FetchError("Artifact JSON must contain an object at the top level.")
            validation = validate_payload_artifact(payload)
            written_csv = write_csv_exports(payload, args.csv_dir) if args.csv_dir else []
        except Exception as exc:  # noqa: BLE001 - CLI should print validation failures.
            print(f"error: {exc}", file=sys.stderr)
            return 1

        print(f"validated {args.validate_artifact}: {validation['status']}")
        for path in written_csv:
            print(f"wrote {path}")
        if validation["errors"]:
            print("\nartifact errors:")
            for error in validation["errors"]:
                print(f"- {error}")
            return 1
        if validation["warnings"]:
            print("\nartifact warnings:")
            for warning in validation["warnings"]:
                print(f"- {warning}")
        seed_readiness = payload.get("seed_readiness", {})
        print(f"\nseed_readiness: {seed_readiness.get('status', 'unknown')}")
        if seed_readiness.get("blockers"):
            print("readiness blockers:")
            for blocker in seed_readiness["blockers"]:
                print(f"- {blocker}")
        if args.fail_on_readiness_blockers and seed_readiness.get("blockers"):
            return 2
        return 0

    try:
        payload = build_payload(
            arcgis_file=args.arcgis_file,
            dghs_file=args.dghs_file,
            community_source_dir=args.community_source_dir,
            geocode_complete_dir=args.geocode_complete_dir,
            union_source=args.union_source,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print source failures.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.csv_dir:
        written_csv = write_csv_exports(payload, args.csv_dir)
    else:
        written_csv = []

    print(f"wrote {args.output}")
    for path in written_csv:
        print(f"wrote {path}")
    print(json.dumps(payload["summary"], indent=2, ensure_ascii=False))
    if payload["validation_warnings"]:
        print("\nwarnings:")
        for warning in payload["validation_warnings"]:
            print(f"- {warning}")
    seed_readiness = payload["seed_readiness"]
    print(f"\nseed_readiness: {seed_readiness['status']}")
    if seed_readiness["blockers"]:
        print("readiness blockers:")
        for blocker in seed_readiness["blockers"]:
            print(f"- {blocker}")
    if args.fail_on_readiness_blockers and seed_readiness["blockers"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
