"""
Summarize endpoint — fetches a structured ingredient record from the
upstream Quantum Palate API, cleans the noise out of it, and runs an
LLM summarization prompt over the cleaned JSON.

Auth model: token passthrough.
    The frontend sends a Laravel Sanctum bearer token.
    - This endpoint validates the token via `require_bearer` (auth.py).
    - The same raw token is forwarded to Quantum Palate on every downstream
      call, so the ingredient API enforces the *caller's* row-level permissions
      — not some shared service identity.

Lifecycle:
    This module does NOT own a FastAPI lifespan. It exposes
    `startup()` and `shutdown()` hooks that the main app's lifespan calls.
    Rationale: a FastAPI app can only have ONE lifespan; resource wiring
    lives in main.py.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.security import HTTPAuthorizationCredentials

from auth import require_bearer, _bearer_scheme
from cleaner_json import clean_ingredient
from summarize_prompt import EXTRACTION_SYSTEM_PROMPT, EXTRACTION_USER_PROMPT_TEMPLATE

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_URL = os.getenv("OLLAMA_URL")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")

INGREDIENTS_API_BASE = os.getenv(
    "INGREDIENTS_API_BASE",
    "https://quantumpalate.com/api/v1",
)

# ---------------------------------------------------------------------------
# Shared HTTP clients
# Created by startup(), closed by shutdown() — both called from main.py lifespan.
# No Authorization header on the ingredients client — set per-request.
# ---------------------------------------------------------------------------

_ingredients_client: httpx.AsyncClient | None = None
_ollama_client: httpx.AsyncClient | None = None


async def startup() -> None:
    """Initialize shared HTTP clients. Call once from main lifespan startup."""
    global _ingredients_client, _ollama_client

    _ingredients_client = httpx.AsyncClient(
        base_url=INGREDIENTS_API_BASE,
        headers={"Accept": "application/json"},
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
    )

    ollama_headers = {"Content-Type": "application/json"}
    if OLLAMA_API_KEY:
        ollama_headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"

    _ollama_client = httpx.AsyncClient(
        base_url=OLLAMA_URL,
        headers=ollama_headers,
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
    )


async def shutdown() -> None:
    """Close shared HTTP clients. Call once from main lifespan shutdown."""
    global _ingredients_client, _ollama_client
    if _ingredients_client is not None:
        await _ingredients_client.aclose()
        _ingredients_client = None
    if _ollama_client is not None:
        await _ollama_client.aclose()
        _ollama_client = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


# Auth is declared in the decorator (same pattern as /extract). FastAPI
# resolves `require_bearer` before the handler runs — no token, no entry.
# We additionally pull `_bearer_scheme` in as a parameter (NOT a second
# validation) so we can access the raw token string and forward it to the
# downstream ingredient API for row-level permission enforcement.
@router.post("/summarize/{ingredient_id}", dependencies=[Depends(require_bearer)])
async def summarize(
    ingredient_id: int = Path(..., gt=0, description="Ingredient ID to summarize"),
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
):
    # Step 1: fetch ingredient using the CALLER's token (not a service token)
    ingredient = await fetch_ingredient(ingredient_id, credentials.credentials)

    # Step 2: strip noise — keep structure, drop plumbing
    cleaned = clean_ingredient(ingredient)

    # Step 3: serialize cleaned JSON for the LLM
    spec_text = json.dumps(cleaned, indent=2, ensure_ascii=False, default=str)

    # Step 4: LLM summarization
    try:
        structured_data = await call_ollama(spec_text)
    except json.JSONDecodeError:
        raise HTTPException(502, "LLM returned invalid JSON")
    except httpx.TimeoutException:
        raise HTTPException(504, "LLM request timed out")
    except Exception as e:
        raise HTTPException(502, f"LLM analysis failed: {str(e)}")

    return {
        "ingredient_id": ingredient_id,
        "ingredient_code": ingredient.get("ingredient_code"),
        "ingredient_name": (ingredient.get("latest_basic_info") or {}).get("ingredient_name"),
        **structured_data,
    }


# ---------------------------------------------------------------------------
# Step 1 — Fetch ingredient (using caller's token)
# ---------------------------------------------------------------------------

async def fetch_ingredient(ingredient_id: int, bearer_token: str) -> dict[str, Any]:
    """Fetch ingredient from upstream API using the caller's own bearer token."""
    if _ingredients_client is None:
        raise HTTPException(500, "Ingredients HTTP client not initialized (startup() not called?)")

    try:
        response = await _ingredients_client.get(
            f"/ingredients/{ingredient_id}",
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "Upstream ingredient API timed out")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Upstream ingredient API unreachable: {e}")

    # Forward meaningful auth/authorization failures instead of masking them as 502.
    # The user's token passed /currentUser but might not have rights to THIS ingredient.
    if response.status_code == 401:
        raise HTTPException(401, "Token rejected by ingredient API")
    if response.status_code == 403:
        raise HTTPException(403, "Not authorized to access this ingredient")
    if response.status_code == 404:
        raise HTTPException(404, f"Ingredient {ingredient_id} not found")
    if response.status_code >= 400:
        raise HTTPException(502, f"Upstream ingredient API error ({response.status_code})")

    try:
        body = response.json()
    except ValueError:
        raise HTTPException(502, "Upstream ingredient API returned non-JSON")

    if not body.get("success"):
        raise HTTPException(502, body.get("message", "Upstream returned success=false"))

    # Upstream wraps as { success, message, data: { data: {...} } }
    ingredient = ((body.get("data") or {}).get("data")) or {}
    if not ingredient:
        raise HTTPException(502, "Upstream ingredient payload was empty")

    return ingredient


# ---------------------------------------------------------------------------
# Step 4 — LLM call
# ---------------------------------------------------------------------------

async def call_ollama(extracted_text: str) -> dict:
    if _ollama_client is None:
        raise HTTPException(500, "Ollama HTTP client not initialized (startup() not called?)")

    async def make_request(messages):
        payload = {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False,
            "format": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
            "options": {
                "temperature": 0.1,
                "num_ctx": 16384,
            },
        }

        response = await _ollama_client.post("/api/chat", json=payload)
        response.raise_for_status()
        return response.json()["message"]["content"]

    def build_messages():
        return [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": EXTRACTION_USER_PROMPT_TEMPLATE.format(
                    extracted_text=extracted_text
                ),
            },
        ]

    last_error: Exception | None = None

    for attempt in range(2):
        try:
            content = await make_request(build_messages())

            print("=" * 80)
            print(f"ATTEMPT {attempt} — RAW LLM OUTPUT:")
            print(repr(content))
            print("=" * 80)

            try:
                return json.loads(content, strict=False)
            except json.JSONDecodeError:
                pass

            cleaned = re.sub(r"^```(?:json)?\s*", "", content.strip())
            cleaned = re.sub(r"\s*```$", "", cleaned)
            try:
                return json.loads(cleaned, strict=False)
            except json.JSONDecodeError:
                pass

            json_match = re.search(r"\{[\s\S]*\}", cleaned)
            if json_match:
                try:
                    return json.loads(json_match.group(), strict=False)
                except json.JSONDecodeError:
                    pass

            try:
                from json_repair import repair_json
                repaired = repair_json(cleaned)
                return json.loads(repaired, strict=False)
            except (ImportError, Exception):
                pass

            raise json.JSONDecodeError("Could not parse LLM output", content, 0)

        except Exception as e:
            last_error = e
            print(f"❌ ATTEMPT {attempt} FAILED: {type(e).__name__}: {e}")
            if attempt == 1:
                raise last_error