"""
nutrition_normalizer.py

Post-processes LLM extraction output to normalize all nutritional values
to a per-100g reference basis.

Rules:
- If reference_basis is already "100g" → no-op
- If reference_basis is a different gram value → scale all non-% nutrients
- nutrient_unit == "%" → skip (percentage-based nutrients can't be scaled)
- actual_value is preserved as original document value
- nutrient_value gets the converted value
- reference_basis is updated to "100g" after conversion
"""

import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Nutrients that are percentage-only and must never be scaled
_SKIP_UNITS = {"%"}

# Regex to extract the gram value from strings like:
# "14g", "14 g", "14G", "1 serving (14g)", "14 grams", "30 G"
_GRAM_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*g(?:rams?)?", re.IGNORECASE)


def _parse_serving_grams(reference_basis: str) -> float | None:
    """
    Extract numeric gram value from a reference_basis string.

    Examples:
        "14g"                  -> 14.0
        "14 G"                 -> 14.0
        "1 Tablespoon 14G"     -> 14.0  (takes LAST match = serving weight)
        "1 serving (30g)"      -> 30.0
        "100g"                 -> 100.0
        "28 grams"             -> 28.0
        ""                     -> None
    """
    if not reference_basis:
        return None

    matches = _GRAM_PATTERN.findall(reference_basis)
    if not matches:
        logger.warning("Could not parse gram value from reference_basis: %r", reference_basis)
        return None

    # If multiple matches (e.g. "1 serving (14g)"), take the last one
    # which is the actual weight, not the serving count
    return float(matches[-1])


def _safe_float(value: str) -> float | None:
    """
    Safely parse a nutrient value string to float.
    Handles: "14", "0.2", "0.0", "117", "<1"
    Returns None if unparseable — those are left untouched.
    """
    if not value:
        return None
    # Strip leading < > ~ signs (e.g. "<1", "~0.5") — take the number
    cleaned = re.sub(r"^[<>~≤≥]", "", str(value).strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def _round_nutrient(value: float, unit: str) -> str:
    """
    Round converted value appropriately based on unit.
    - kcal / kJ  → 1 decimal
    - mg         → 2 decimals
    - g          → 2 decimals
    - others     → 2 decimals
    """
    if unit.lower() in ("kcal", "kj", "cal"):
        return str(round(value, 1))
    return str(round(value, 2))


def normalize_nutrition_to_100g(llm_output: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize nutritional composition to per-100g basis in-place.

    Args:
        llm_output: The full parsed JSON dict returned by the LLM

    Returns:
        The same dict, mutated with normalized nutrient values.
        If no conversion needed or not possible, returns unchanged.
    """
    try:
        nutrition = llm_output.get("specifications", {}).get("nutritional_composition", {})
    except AttributeError:
        return llm_output

    if not nutrition:
        return llm_output

    reference_basis: str = nutrition.get("reference_basis", "100g") or "100g"

    serving_grams = _parse_serving_grams(reference_basis)

    if serving_grams is None:
        logger.warning("Skipping normalization: could not parse reference_basis %r", reference_basis)
        return llm_output

    if abs(serving_grams - 100.0) < 0.001:
        # Already per 100g — nothing to do
        return llm_output

    factor = 100.0 / serving_grams
    logger.info(
        "Normalizing nutrients from %s to 100g (factor=%.6f)",
        reference_basis,
        factor,
    )

    nutrients: list[dict] = nutrition.get("nutrients", [])
    converted_count = 0
    skipped_count = 0

    for nutrient in nutrients:
        unit: str = (nutrient.get("nutrient_unit") or "").strip()

        # Skip percentage-based nutrients — they cannot be scaled
        if unit in _SKIP_UNITS:
            skipped_count += 1
            continue

        raw_value: str = nutrient.get("actual_value", "")
        parsed = _safe_float(raw_value)

        if parsed is None:
            # Unparseable value (e.g. "N/A", empty) — leave untouched
            skipped_count += 1
            continue

        converted = parsed * factor
        nutrient["actual_value"] = _round_nutrient(converted, unit)
        # actual_value preserved as-is (original document value)
        converted_count += 1

    # Update reference basis
    nutrition["reference_basis"] = "100g"

    logger.info(
        "Nutrition normalization complete: %d converted, %d skipped",
        converted_count,
        skipped_count,
    )

    return llm_output