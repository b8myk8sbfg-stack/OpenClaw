"""Lightweight part-number → brand inference (no Selenium)."""

from __future__ import annotations

import re

SMC_PART_PREFIXES = (
    "ZFC",
    "ZS",
    "C96",
    "CQ2",
    "CDQ2",
    "CY",
    "MXS",
    "MXQ",
    "SY",
    "VXZ",
    "AF",
    "KQ2",
    "NCD",
    "IS10",
    "PF",
    "VQC",
    "VT",
    "AR",
    "AN",
    "AS",
)


def normalize_part_key(part_no: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(part_no or "").upper())


def infer_brand_from_part(part_no: str) -> str:
    part_norm = normalize_part_key(part_no)

    for prefix in SMC_PART_PREFIXES:
        if part_norm.startswith(prefix):
            return "SMC"

    if (
        part_norm.startswith("E3Z")
        or part_norm.startswith("E39")
        or part_norm.startswith("E2E")
        or part_norm.startswith("MY2")
        or part_norm.startswith("MY4")
        or part_norm.startswith("H3Y")
        or part_norm.startswith("H3J")
        or part_norm.startswith("H3CR")
        or part_norm.startswith("E5CC")
        or part_norm.startswith("E5CN")
    ):
        return "OMRON"

    if part_norm.startswith("150C") or part_norm.startswith("150C"):
        return "ALLEN-BRADLEY"

    return "UNKNOWN"
