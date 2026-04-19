"""Shared prompt building and response parsing for subtitle translation."""

import re
from typing import List

from translation.srt_parser import Cue

_NUMBER_PREFIX_RE = re.compile(r"^\d+[\s]*[:.)\-]\s*")


def build_prompt(cues: List[Cue], source_lang: str, target_lang: str) -> str:
    """Build the numbered translation prompt from cues."""
    lines = []
    for i, cue in enumerate(cues, 1):
        text = cue.text.replace("\n", "<br>")
        lines.append(f"{i}: {text}")
    header = f"Translate from {source_lang} to {target_lang}:\n"
    return header + "\n".join(lines)


def parse_response(response_text: str, expected_count: int) -> List[str]:
    """Parse numbered response lines back into text strings."""
    lines = response_text.strip().split("\n")
    results = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        cleaned = _NUMBER_PREFIX_RE.sub("", line)
        cleaned = cleaned.replace("<br>", "\n")
        results.append(cleaned)
    while len(results) < expected_count:
        results.append("")
    return results[:expected_count]


def build_system_prompt(source_lang: str, target_lang: str) -> str:
    base = (
        f"Professional subtitle translator: {source_lang} → {target_lang}.\n"
        "Rules:\n"
        "- Natural, everyday vocabulary\n"
        "- Correct gender/number agreement. Default to masculine when speaker gender is unknown\n"
        "- Use subjunctive mood for commands and wishes (nunca consumas, no toques)\n"
        "- NEVER invent words. Use simple synonyms if unsure\n"
        "- Return ONLY numbered lines. Same line count. No explanations"
    )
    if target_lang.lower() in ("spanish", "español"):
        base += (
            "\n- IMPORTANT: Do NOT translate character names, place names, or proper nouns."
            " Keep them exactly as they appear in the source text"
            " (e.g., Spider, Kiri, Tuk, High Camp, Windtraders)."
            "\n- Use the informal 'tú' form consistently, never 'usted'."
            "\n- When translating 'Your mother/father', always use 'Tu madre/padre',"
            " never 'Mi madre/padre'."
        )
    return base


_SPANISH_FEW_SHOT_USER = (
    "1: No way.\n"
    "2: Never consume monsters.\n"
    "3: Take a knee. Let's go.\n"
    "4: As long as he's no trouble.\n"
    "5: - No, Dad. - No, Dad. He can't.\n"
    "6: Spider, you're going to live back in High Camp with Norm."
)
_SPANISH_FEW_SHOT_ASSISTANT = (
    "1: No puede ser.\n"
    "2: Nunca consumas monstruos.\n"
    "3: Arrodíllate. Vamos.\n"
    "4: Mientras no sea un problema.\n"
    "5: - No, papá. - No, papá. No puede.\n"
    "6: Spider, vas a volver a vivir en High Camp con Norm."
)


def build_few_shot(source_lang: str, target_lang: str) -> list:
    if target_lang.lower() in ("spanish", "español"):
        return [
            {"role": "user", "content": _SPANISH_FEW_SHOT_USER},
            {"role": "assistant", "content": _SPANISH_FEW_SHOT_ASSISTANT},
        ]
    return []
