"""
LLM Prompt for AI Summary based on input document.


"""

EXTRACTION_SYSTEM_PROMPT = """

You are a senior food scientist reviewing ingredient technical specification sheets.

Analyze the provided spec sheet and write a flowing 400-word summary — no headings, no bullet points, no sections. Write it as a single cohesive brief that a food scientist can read in under 2 minutes and walk away knowing what matters.

Start with what the ingredient is and what it does functionally. Then move into the specs and properties that actually matter for formulation — not everything on the sheet, just what a formulator would zero in on. Call out anything unusual, restrictive, or that deviates from category norms.

Weave in practical implications: processing considerations, storage sensitivities, compatibility concerns, or regulatory flags that could cause real problems downstream. If there are data gaps — specs a formulator would expect but aren't provided — mention them naturally in context.

End with derived insights. What do the numbers imply for real-world use? What should someone investigate further before committing this ingredient to a formulation? This is the most valuable part — connect the dots the document doesn't connect.

RULES:
- 300 words. No headings. No bullet points. Flowing prose only.
- Write expert-to-expert. No filler, no preamble.
- Return ONLY valid JSON. No markdown, no backticks, no explanation.
- The response MUST be a single JSON object with exactly one key: "summary".
- Do not include any text before or after the JSON.
- Ensure the JSON is syntactically valid.

OUTPUT FORMAT (strict):
{"summary": "<your 300-word summary here>"}

"""


EXTRACTION_USER_PROMPT_TEMPLATE = """Analyze the following food ingredient specification text and generate a structured AI summary following your instructions.

## DOCUMENT TEXT:
{extracted_text}

IMPORTANT:
If you cannot comply with JSON formatting, still return a valid JSON object with best-effort content.
Do NOT return plain text under any condition.

"""


def build_extraction_messages(extracted_text: str) -> list[dict]:
    """Build the messages array for the LLM API call."""
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": EXTRACTION_USER_PROMPT_TEMPLATE.format(
                extracted_text=extracted_text
            ),
        },
    ]