#!/usr/bin/env python3
"""
generate_dp_header.py — Generate tuya_dp_defs.h from tuya_dp_schema.json.

Reads the Tuya Data Point schema JSON (shared/protocols/tuya_dp_schema.json),
validates it, and emits a C header file (glasses-firmware/main/tuya/tuya_dp_defs.h)
with:
  * dp_type_t enum (bool / value / string / enum / raw)
  * dp_direction_t enum (upload / download)
  * dp_id_t enum (one entry per DP, IDs 1-10 upload, 101-104 download)
  * dp_mode_t enum (for enum-type DPs, using the range array)
  * DP_CODE_* string macros
  * Property constraint macros (min / max / step)
  * String length limit macros (maxlen)
  * Tuya DP frame format constants
  * dp_value_t union and dp_report_entry_t struct

Usage:
    python tools/generate_dp_header.py [--schema PATH] [--output PATH]

Defaults:
    --schema  shared/protocols/tuya_dp_schema.json
    --output  glasses-firmware/main/tuya/tuya_dp_defs.h

Exit codes:
    0  success
    1  schema file not found / invalid JSON
    2  schema validation failed (missing required fields, ID conflict, etc.)
    3  file I/O error writing the output header

Copyright (c) 2024 Railway Inspection AR Glasses Project
License: Apache-2.0
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────
# Tuya DP type mapping: JSON type string → (C enum name, C enum value)
# ─────────────────────────────────────────────────────────────────────────

DP_TYPE_MAP: Dict[str, Tuple[str, int]] = {
    "bool":   ("DP_TYPE_BOOL",   0),
    "value":  ("DP_TYPE_VALUE",  1),
    "string": ("DP_TYPE_STRING", 2),
    "enum":   ("DP_TYPE_ENUM",   3),
    "raw":    ("DP_TYPE_RAW",    4),
}


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────

REQUIRED_FIELDS = ["id", "code", "name_cn", "name_en", "type", "mode", "direction"]


def validate_schema(schema: Dict[str, Any]) -> Tuple[List[Dict], List[Dict]]:
    """
    Validate the parsed schema dict.

    Returns (upload_dps, download_dps) lists on success.
    Raises ValueError with a descriptive message on failure.
    """
    if "data_points" not in schema:
        raise ValueError("schema missing 'data_points' key")

    dp_section = schema["data_points"]
    upload = dp_section.get("upload", [])
    download = dp_section.get("download", [])

    all_dps = upload + download
    seen_ids: set = set()

    for dp in all_dps:
        # Check required fields.
        for field in REQUIRED_FIELDS:
            if field not in dp:
                raise ValueError(
                    f"DP id={dp.get('id', '?')} missing required field '{field}'"
                )

        # ID uniqueness.
        dp_id = dp["id"]
        if dp_id in seen_ids:
            raise ValueError(f"duplicate DP id={dp_id}")
        seen_ids.add(dp_id)

        # Type must be known.
        dp_type = dp["type"]
        if dp_type not in DP_TYPE_MAP:
            raise ValueError(
                f"DP id={dp_id} has unknown type '{dp_type}' "
                f"(expected one of {list(DP_TYPE_MAP.keys())})"
            )

        # Enum types must have a range.
        if dp_type == "enum":
            prop = dp.get("property", {})
            if "range" not in prop or not isinstance(prop["range"], list):
                raise ValueError(
                    f"DP id={dp_id} (enum) must define property.range array"
                )

        # Value types must have property with min/max.
        if dp_type == "value":
            prop = dp.get("property", {})
            for req in ("min", "max"):
                if req not in prop:
                    raise ValueError(
                        f"DP id={dp_id} (value) must define property.{req}"
                    )

    # Validate validation_rules (if present).
    rules = schema.get("validation_rules", {})
    expected_upload = rules.get("upload_count", len(upload))
    expected_download = rules.get("download_count", len(download))
    expected_total = rules.get("total_count", len(all_dps))

    if len(upload) != expected_upload:
        raise ValueError(
            f"upload count mismatch: expected {expected_upload}, got {len(upload)}"
        )
    if len(download) != expected_download:
        raise ValueError(
            f"download count mismatch: expected {expected_download}, "
            f"got {len(download)}"
        )
    if len(all_dps) != expected_total:
        raise ValueError(
            f"total count mismatch: expected {expected_total}, got {len(all_dps)}"
        )

    return upload, download


# ─────────────────────────────────────────────────────────────────────────
# C identifier helpers
# ─────────────────────────────────────────────────────────────────────────

def code_to_macro(code: str) -> str:
    """Convert 'battery_level' → 'BATTERY_LEVEL'."""
    return code.upper()


def code_to_enum_name(code: str) -> str:
    """Convert 'battery_level' → 'DP_ID_BATTERY_LEVEL'."""
    return "DP_ID_" + code.upper()


def mode_to_enum_name(mode_str: str) -> str:
    """Convert 'standby' → 'DP_MODE_STANDBY'."""
    return "DP_MODE_" + mode_str.upper()


# ─────────────────────────────────────────────────────────────────────────
# Header generation
# ─────────────────────────────────────────────────────────────────────────

def generate_header(schema: Dict[str, Any],
                    upload_dps: List[Dict],
                    download_dps: List[Dict]) -> str:
    """Produce the full C header file content as a string."""

    product = schema.get("product_info", {})
    product_name = product.get("product_name", "Unknown Product")
    schema_version = schema.get("schema_version", "0.0.0")
    gen_date = datetime.now().strftime("%Y-%m-%d")

    lines: List[str] = []

    # ── File header ──────────────────────────────────────────────
    lines.append("/**")
    lines.append(f" * @file tuya_dp_defs.h")
    lines.append(f" * @brief Tuya IoT Data Point (DP) definitions — auto-generated from")
    lines.append(f" *        shared/protocols/tuya_dp_schema.json.")
    lines.append(" *")
    lines.append(" * This header is regenerated by tools/generate_dp_header.py. Do not edit")
    lines.append(" * by hand — modify the JSON schema and re-run the generator.")
    lines.append(" *")
    lines.append(f" * DP summary (schema v{schema_version}):")
    lines.append(f" *   Upload  (device -> cloud): {len(upload_dps)} DPs "
                 f"(IDs {upload_dps[0]['id']}-{upload_dps[-1]['id']})")
    lines.append(f" *   Download (cloud -> device):  {len(download_dps)} DPs "
                 f"(IDs {download_dps[0]['id']}-{download_dps[-1]['id']})")
    lines.append(f" *   Total: {len(upload_dps) + len(download_dps)} DPs")
    lines.append(" *")
    lines.append(f" * Product: {product_name}")
    lines.append(" *")
    lines.append(" * @copyright Copyright (c) 2024 Railway Inspection AR Glasses Project")
    lines.append(" * @license Apache-2.0")
    lines.append(f" * @note Generated: {gen_date} — verify against tuya_dp_schema.json.")
    lines.append(" */")
    lines.append("#ifndef TUYA_DP_DEFS_H")
    lines.append("#define TUYA_DP_DEFS_H")
    lines.append("")
    lines.append("#include <stdint.h>")
    lines.append("#include <stdbool.h>")
    lines.append("")
    lines.append("#ifdef __cplusplus")
    lines.append('extern "C" {')
    lines.append("#endif")
    lines.append("")

    # ── DP Type enum ──────────────────────────────────────────────
    lines.append("/* =========================================================================")
    lines.append(" * DP Type enum (matches Tuya cloud type encoding)")
    lines.append(" * ========================================================================= */")
    lines.append("")
    lines.append("typedef enum {")
    for json_type, (c_name, c_val) in DP_TYPE_MAP.items():
        comment = {
            "bool":   "Boolean (true/false)",
            "value":  "Numeric (int32 with min/max)",
            "string": "UTF-8 string",
            "enum":   "Enumerated (index into range)",
            "raw":    "Raw binary (opaque bytes)",
        }[json_type]
        lines.append(f"    {c_name:<20s} = {c_val},  /**< {comment} */")
    lines.append("} dp_type_t;")
    lines.append("")

    # ── DP Direction enum ─────────────────────────────────────────
    lines.append("/* =========================================================================")
    lines.append(" * DP Direction enum")
    lines.append(" * ========================================================================= */")
    lines.append("")
    lines.append("typedef enum {")
    lines.append("    DP_DIR_UPLOAD   = 0,  /**< Device -> Cloud (report)     */")
    lines.append("    DP_DIR_DOWNLOAD = 1,  /**< Cloud -> Device (command)    */")
    lines.append("} dp_direction_t;")
    lines.append("")

    # ── DP ID enum ────────────────────────────────────────────────
    lines.append("/* =========================================================================")
    lines.append(" * DP ID enum — Upload (device -> cloud), "
                 f"IDs {upload_dps[0]['id']}-{upload_dps[-1]['id']}")
    lines.append(" * ========================================================================= */")
    lines.append("")
    lines.append("typedef enum {")
    lines.append("    /* --- Upload DPs "
                 f"({upload_dps[0]['id']}-{upload_dps[-1]['id']}) --- */")

    for dp in upload_dps:
        enum_name = code_to_enum_name(dp["code"])
        comment = (f"{dp['type']}, {dp['mode']} — {dp['name_cn']}")
        if dp["type"] == "value":
            prop = dp.get("property", {})
            comment += f" [{prop.get('min', '?')}-{prop.get('max', '?')}]"
            if "unit" in prop:
                comment += prop["unit"]
        elif dp["type"] == "string":
            comment += f" (maxlen={dp.get('maxlen', '?')})"
        lines.append(f"    {enum_name:<30s} = {dp['id']:<4d}, /**< {comment} */")

    lines.append("")
    lines.append(f"    /* --- Download DPs "
                 f"({download_dps[0]['id']}-{download_dps[-1]['id']}) --- */")

    for dp in download_dps:
        enum_name = code_to_enum_name(dp["code"])
        comment = (f"{dp['type']}, {dp['mode']} — {dp['name_cn']}")
        if dp["type"] == "value":
            prop = dp.get("property", {})
            comment += f" [{prop.get('min', '?')}-{prop.get('max', '?')}]"
            if "unit" in prop:
                comment += prop["unit"]
        lines.append(f"    {enum_name:<30s} = {dp['id']:<4d}, /**< {comment} */")

    total_count = len(upload_dps) + len(download_dps)
    lines.append("")
    lines.append(f'    /** Sentinel: total number of DPs defined. */')
    lines.append(f"    DP_ID_COUNT                = {total_count},")
    lines.append("} dp_id_t;")
    lines.append("")

    # ── Mode enum (for enum-type DPs) ────────────────────────────
    # Collect all unique enum ranges.
    enum_ranges: Dict[str, List[str]] = {}
    for dp in upload_dps + download_dps:
        if dp["type"] == "enum":
            prop = dp.get("property", {})
            range_list = prop.get("range", [])
            key = dp["code"]
            enum_ranges[key] = range_list

    # If there are enum DPs, generate a combined mode enum (or per-DP enums).
    # For simplicity, we generate a unified dp_mode_t using the first enum DP's
    # range (all enum DPs in this schema share the same mode enum).
    if enum_ranges:
        first_key = next(iter(enum_ranges))
        mode_range = enum_ranges[first_key]

        lines.append("/* =========================================================================")
        lines.append(f" * Mode enum values (for {first_key} / all enum-type DPs)")
        lines.append(" * ========================================================================= */")
        lines.append("")
        lines.append("typedef enum {")
        for idx, mode_str in enumerate(mode_range):
            enum_name = mode_to_enum_name(mode_str)
            lines.append(f"    {enum_name:<20s} = {idx},  /**< {mode_str}  */")
        lines.append("} dp_mode_t;")
        lines.append("")

    # ── DP Code string macros ─────────────────────────────────────
    lines.append("/* =========================================================================")
    lines.append(" * DP Code strings (for JSON serialisation and debugging)")
    lines.append(" * ========================================================================= */")
    lines.append("")
    for dp in upload_dps + download_dps:
        macro = "DP_CODE_" + code_to_macro(dp["code"])
        lines.append(f'#define {macro:<40s} "{dp["code"]}"')
    lines.append("")

    # ── Property constraints ──────────────────────────────────────
    value_dps = [dp for dp in upload_dps + download_dps if dp["type"] == "value"]
    if value_dps:
        lines.append("/* =========================================================================")
        lines.append(" * Property constraints (for value-type DPs)")
        lines.append(" * ========================================================================= */")
        lines.append("")
        for dp in value_dps:
            prop = dp.get("property", {})
            macro_prefix = "DP_" + code_to_macro(dp["code"])
            lines.append(f"#define {macro_prefix}_MIN   {prop.get('min', 0)}")
            lines.append(f"#define {macro_prefix}_MAX   {prop.get('max', 0)}")
            lines.append(f"#define {macro_prefix}_STEP  {prop.get('step', 1)}")
            lines.append("")

    # ── String length limits ─────────────────────────────────────
    string_dps = [dp for dp in upload_dps + download_dps
                  if dp["type"] == "string"]
    if string_dps:
        lines.append("/* =========================================================================")
        lines.append(" * String length limits")
        lines.append(" * ========================================================================= */")
        lines.append("")
        for dp in string_dps:
            macro = "DP_MAXLEN_" + code_to_macro(dp["code"])
            maxlen = dp.get("maxlen", 256)
            lines.append(f"#define {macro:<40s} {maxlen}")
        lines.append("")

    # ── Tuya DP frame format constants ────────────────────────────
    lines.append("/* =========================================================================")
    lines.append(" * Tuya DP command frame (on-wire format over BLE NUS)")
    lines.append(" *")
    lines.append(" * The Tuya BLE protocol wraps each DP in a Type-Length-Value frame:")
    lines.append(" *   [dp_id:2B] [dp_type:1B] [len:2B] [value:lenB]")
    lines.append(" *")
    lines.append(" * The glasses firmware uses this struct to pack DP values into a")
    lines.append(" * MSG_DEVICE_STATUS or MSG_DEVICE_CONFIG message payload.")
    lines.append(" * ========================================================================= */")
    lines.append("")
    lines.append("#define TUYA_DP_FRAME_HDR_SIZE  5U  /* id(2) + type(1) + len(2) */")
    lines.append("#define TUYA_DP_MAX_VALUE_SIZE  512U  /* max string DP value */")
    lines.append("#define TUYA_DP_MAX_FRAME_SIZE  "
                 "(TUYA_DP_FRAME_HDR_SIZE + TUYA_DP_MAX_VALUE_SIZE)")
    lines.append("")
    lines.append("/** Maximum total payload for a single DP report "
                 "(multiple DPs batched). */")
    lines.append("#define TUYA_DP_REPORT_MAX_PAYLOAD  1024U")
    lines.append("")
    lines.append(f"/** Maximum number of DPs that can be batched in one report. */")
    lines.append(f"#define TUYA_DP_REPORT_MAX_COUNT    {total_count}U")
    lines.append("")

    # ── dp_value_t union ──────────────────────────────────────────
    lines.append("/**")
    lines.append(" * @brief Tuya DP value union — covers all supported DP types.")
    lines.append(" */")
    lines.append("typedef struct {")
    lines.append("    dp_type_t type;")
    lines.append("    union {")
    lines.append("        bool       bval;     /**< DP_TYPE_BOOL  */")
    lines.append("        int32_t    ival;     /**< DP_TYPE_VALUE */")
    lines.append('        const char *sval;    /**< DP_TYPE_STRING (points to external buffer) */')
    lines.append("        uint32_t   eval;     /**< DP_TYPE_ENUM  */")
    lines.append("        struct {")
    lines.append("            const uint8_t *data;")
    lines.append("            uint16_t       len;")
    lines.append("        } raw;                /**< DP_TYPE_RAW   */")
    lines.append("    } v;")
    lines.append("} dp_value_t;")
    lines.append("")

    # ── dp_report_entry_t struct ──────────────────────────────────
    lines.append("/**")
    lines.append(" * @brief A single DP report entry (for batch upload).")
    lines.append(" */")
    lines.append("typedef struct {")
    lines.append("    uint16_t   id;     /**< DP ID (see dp_id_t) */")
    lines.append("    dp_type_t  type;   /**< DP type */")
    lines.append("    uint16_t   len;     /**< Value length in bytes */")
    lines.append("    const uint8_t *value; /**< Pointer to serialised value */")
    lines.append("} dp_report_entry_t;")
    lines.append("")

    # ── Footer ────────────────────────────────────────────────────
    lines.append("#ifdef __cplusplus")
    lines.append("}")
    lines.append("#endif")
    lines.append("")
    lines.append("#endif /* TUYA_DP_DEFS_H */")
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate tuya_dp_defs.h from tuya_dp_schema.json."
    )
    # Resolve paths relative to the repository root (parent of this script).
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent  # glasses-firmware → railway-ar-glasses-embedded

    parser.add_argument(
        "--schema",
        type=Path,
        default=repo_root / "shared" / "protocols" / "tuya_dp_schema.json",
        help="Path to tuya_dp_schema.json (default: shared/protocols/tuya_dp_schema.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repo_root / "glasses-firmware" / "main" / "tuya" / "tuya_dp_defs.h",
        help="Output path for tuya_dp_defs.h "
             "(default: glasses-firmware/main/tuya/tuya_dp_defs.h)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate the schema but do not write the output file.",
    )
    args = parser.parse_args()

    # ── 1. Load schema ────────────────────────────────────────────
    schema_path: Path = args.schema
    if not schema_path.exists():
        print(f"ERROR: schema file not found: {schema_path}", file=sys.stderr)
        return 1

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON in {schema_path}: {exc}", file=sys.stderr)
        return 1

    print(f"Loaded schema: {schema_path}")
    print(f"  Schema version: {schema.get('schema_version', 'unknown')}")
    print(f"  Product: {schema.get('product_info', {}).get('product_name', 'unknown')}")

    # ── 2. Validate ───────────────────────────────────────────────
    try:
        upload_dps, download_dps = validate_schema(schema)
    except ValueError as exc:
        print(f"ERROR: schema validation failed: {exc}", file=sys.stderr)
        return 2

    print(f"  Upload DPs:   {len(upload_dps)}")
    print(f"  Download DPs: {len(download_dps)}")
    print(f"  Total DPs:    {len(upload_dps) + len(download_dps)}")

    if args.check_only:
        print("Schema validation passed (--check-only).")
        return 0

    # ── 3. Generate header ────────────────────────────────────────
    header_content = generate_header(schema, upload_dps, download_dps)

    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(header_content)
    except OSError as exc:
        print(f"ERROR: failed to write {output_path}: {exc}", file=sys.stderr)
        return 3

    print(f"Generated: {output_path} ({len(header_content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
