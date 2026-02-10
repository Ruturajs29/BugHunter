"""reporter node - formats final output for the CSV."""

from __future__ import annotations

import re

from bughunter.state import BugHunterState


def _clean_line_numbers(raw: str) -> str:
    """Extract and normalize line numbers to a clean comma-separated string."""
    numbers = re.findall(r'\d+', raw)
    if not numbers:
        return "1"
    seen: set[str] = set()
    unique: list[str] = []
    for n in numbers:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return ",".join(unique)


def _clean_explanation(text: str) -> str:
    """Clean explanation to be concise and human-readable."""
    # Remove common verbose patterns
    patterns_to_remove = [
        r"Evidence:.*?(?=Line \d|$)",
        r"CONTEXT (?:says|states|mentions|quote).*?(?=Line \d|\.|$)",
        r"DOCS (?:state|mention|show).*?(?=Line \d|\.|$)",
        r"Note:.*$",
        r"However,? without.*$",
        r"Further verification.*$",
        r"These potential.*$",
        r"It is essential to verify.*$",
        r"The exact allowed ranges.*$",
        r"This would need to be verified.*$",
        r"Additionally,?.*?(?=Line \d|\.|$)",
        r"This implies that.*?(?=\.|$)",
        r"which implies.*?(?=\.|$)",
        r"The correct (?:code|answer|order).*?(?=\.|$)",
        r"Therefore,?.*?(?=\.|$)",
    ]
    
    result = text.strip()
    for pattern in patterns_to_remove:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE | re.DOTALL)
    
    # Clean up multiple spaces and normalize
    result = re.sub(r'\s+', ' ', result).strip()
    result = re.sub(r'\s*\.\s*\.', '.', result)  # Remove double periods
    result = re.sub(r'\s+\.', '.', result)  # Fix spacing before periods
    result = re.sub(r',\s*,', ',', result)  # Remove double commas
    
    # Split long explanations by "Line X:" and format nicely
    if len(result) > 200:
        parts = re.split(r'(Line \d+:)', result)
        if len(parts) > 2:
            formatted = []
            i = 1
            while i < len(parts):
                if i + 1 < len(parts):
                    line_part = parts[i].strip() + " " + parts[i+1].strip()
                    # Take only first sentence of each line explanation
                    first_sentence = re.match(r'^(.*?[.!?])\s', line_part + " ")
                    if first_sentence:
                        formatted.append(first_sentence.group(1))
                    else:
                        formatted.append(line_part[:100])
                i += 2
            result = " | ".join(formatted)
    
    # Final cleanup
    result = result.strip()
    if not result:
        result = "Bug detected in code."
    
    return result


def reporter_node(state: BugHunterState) -> dict:
    """Produce the final bug report fields for the output CSV."""
    bug_line = state.get("bug_line", "").strip()
    bug_explanation = state.get("bug_explanation", "").strip()

    if bug_line and bug_line != "ERROR":
        bug_line = _clean_line_numbers(bug_line)

    bug_explanation = _clean_explanation(bug_explanation)

    if not bug_line or bug_line == "1":
        candidates = state.get("candidate_lines", [])
        if candidates:
            line_nos = [str(c.get("line_no", "")) for c in candidates if c.get("line_no")]
            if line_nos:
                bug_line = ",".join(line_nos[:3])
            if not bug_explanation:
                bug_explanation = "; ".join(
                    c.get("reason", "") for c in candidates[:3] if c.get("reason")
                )

    if not bug_line:
        bug_line = "Unable to identify"
    if not bug_explanation:
        bug_explanation = "Analysis inconclusive."

    return {
        "bug_line": bug_line,
        "bug_explanation": bug_explanation,
    }
