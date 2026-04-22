"""
Ingredient JSON cleaner.

Transforms the upstream API response into an LLM-friendly shape:
- Strips audit metadata, database plumbing, FK/LOV noise.
- Drops top-level objects that aren't about the ingredient itself.
- Renames `*_selected_value` keys to drop the suffix for readability.
- Merges `*_value` / `*_value_str` pairs into a single `value` key.
- Removes null/empty siblings so the LLM doesn't waste tokens on absence.

The cleaner is deliberately generic — it operates on key patterns, not
on a hardcoded schema — so new fields added upstream flow through
automatically without code changes.
"""

from __future__ import annotations

from typing import Any

# Always stripped, at any nesting depth.
_STRIP_KEYS_RECURSIVE = {
    # Audit
    "created_by", "updated_by", "deleted_by", "last_updated_by",
    "created_at", "updated_at", "deleted_at",
    # Session / security
    "session_id", "last_update_login",
    # DB plumbing
    "id", "version", "display_order",
    # Link-back keys (already implied by nesting)
    "ingredient_id", "ingredient_functionality_id", "company_id",
}

# Stripped only at the top level of the ingredient object.
_STRIP_KEYS_TOP_LEVEL = {
    "creator", "updater", "company", "active_version", "version_history",
}


def clean_ingredient(ingredient: dict[str, Any]) -> dict[str, Any]:
    """Entry point. Produces a cleaned copy of the ingredient object."""
    top_level_filtered = {
        k: v for k, v in ingredient.items() if k not in _STRIP_KEYS_TOP_LEVEL
    }
    return _clean(top_level_filtered)


def _clean(node: Any) -> Any:
    """Recursively clean any JSON-like structure."""
    if isinstance(node, dict):
        return _clean_dict(node)
    if isinstance(node, list):
        cleaned_items = [_clean(item) for item in node]
        # Drop empty dicts that collapsed to nothing after cleaning
        return [item for item in cleaned_items if item not in (None, {}, [])]
    return node


def _clean_dict(obj: dict[str, Any]) -> dict[str, Any]:
    # Pass 1: merge _value / _value_str pairs before any renaming happens
    obj = _merge_value_pairs(obj)

    result: dict[str, Any] = {}
    for key, value in obj.items():
        # Skip keys we always strip
        if key in _STRIP_KEYS_RECURSIVE:
            continue
        # Skip FK and LOV code keys (pattern-based — catches future additions)
        if key.endswith("_fk") or key.endswith("_lov_code"):
            continue

        # Rename `foo_selected_value` -> `foo`
        clean_key = (
            key[: -len("_selected_value")]
            if key.endswith("_selected_value")
            else key
        )

        cleaned_value = _clean(value)

        # Drop null / empty-string / empty-collection leaves.
        # Keep 0, 0.0, False — they are meaningful.
        if cleaned_value is None:
            continue
        if isinstance(cleaned_value, str) and cleaned_value == "":
            continue
        if isinstance(cleaned_value, (list, dict)) and len(cleaned_value) == 0:
            continue

        result[clean_key] = cleaned_value

    return result


def _merge_value_pairs(obj: dict[str, Any]) -> dict[str, Any]:
    """
    Collapse paired `foo_value` (numeric) + `foo_value_str` (text) keys into a
    single `foo_value`, using whichever one is populated.

    Operates on a copy so the caller's dict is untouched.
    """
    merged = dict(obj)
    for str_key in [k for k in obj.keys() if k.endswith("_value_str")]:
        num_key = str_key[: -len("_str")]  # e.g. property_value_str -> property_value
        num_val = merged.get(num_key)
        str_val = merged.get(str_key)
        # Pick whichever is populated, preferring numeric when both exist
        if num_val is not None:
            merged[num_key] = num_val
        elif str_val is not None:
            merged[num_key] = str_val
        else:
            merged[num_key] = None
        del merged[str_key]
    return merged