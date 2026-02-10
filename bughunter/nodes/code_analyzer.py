"""code_analyzer node - extracts APIs, runs static analysis, identifies candidate bug lines."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from langchain_core.messages import SystemMessage, HumanMessage

from bughunter.llm import get_llm, invoke_with_retry
from bughunter.state import BugHunterState


SYSTEM_PROMPT = """\
You are a senior C++ / RDI semiconductor test-code analyst.
You will receive a snippet of C++ code, context describing the expected behavior, and static analysis hints.

Your job:
1. Number every line of the code starting from 1.
2. List every RDI API / function call (e.g. rdi.dc().vForce(), rdi.smartVec().vecEditMode()).
3. Carefully compare the CODE against the CONTEXT description to find mismatches.
4. For each line containing a CLEAR defect, note the line NUMBER and the specific error.

BUG DETECTION RULES (be thorough):
- Function names that do not exist, are misspelled, or have wrong casing (e.g. iMeans vs iMeas, imeasRange vs iMeasRange)
- Incorrect argument values or swapped argument order (e.g. iClamp(high, low) instead of iClamp(low, high))
- Wrong lifecycle ordering (RDI_END before RDI_BEGIN, or misplaced RDI_BEGIN/RDI_END)
- Pin name mismatch between related operations (e.g. capture on "D0" but read from "DO")
- Using incorrect API method (e.g. .burst() instead of .execute(), .read() instead of .execute())
- Values outside allowed ranges (e.g. vForceRange(35V) when max is 30V, samples > 8192)
- Wrong method chaining order (e.g. iMeas() before addWaveform() when it should be after)
- Incorrect object/method references (e.g. rdi.burstUpload.smartVec() vs rdi.smartVec().burstUpload())
- Non-existent methods (e.g. push_forward instead of push_back)
- Missing required parameters or extra unwanted parameters
- Wrong variable names in related operations (e.g. vec_port1 vs vec_port2 mismatch)

CRITICAL: Cross-reference the CONTEXT description with the code. The context often describes
what the code SHOULD do - if the code does something different, that's a bug.

Do NOT flag lines that are syntactically and semantically correct.

Output EXACTLY (no markdown fences):

APIS:
<api_1>
<api_2>
...

CANDIDATES:
<line_number>|<line_content>|<concrete reason citing what is wrong and what it should be>
"""


def _try_cppcheck(code: str) -> str:
    """Run cppcheck if available; return output or empty string."""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".cpp", mode="w", delete=False
        ) as tmp:
            tmp.write(code)
            tmp.flush()
            result = subprocess.run(
                ["cppcheck", "--enable=all", "--quiet", tmp.name],
                capture_output=True,
                text=True,
                timeout=15,
            )
            Path(tmp.name).unlink(missing_ok=True)
            return (result.stdout + result.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _regex_heuristics(code: str) -> str:
    """Quick regex-based checks for common C++ / RDI issues."""
    issues: list[str] = []
    lines = code.splitlines()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if "RDI_BEGIN" in stripped or "RDI_END" in stripped:
            issues.append(f"Line {i}: RDI lifecycle call, verify ordering")
        if re.search(r"\.vecEditMode\s*\(", stripped):
            issues.append(f"Line {i}: vecEditMode call, verify mode parameter")
        if re.search(r"\.iClamp\s*\(", stripped):
            issues.append(f"Line {i}: iClamp call, verify (low, high) order")
        if re.search(r"\.vForceRange\s*\(", stripped):
            issues.append(f"Line {i}: vForceRange, verify value within allowed range")
        if re.search(r"push_forward", stripped):
            issues.append(f"Line {i}: push_forward is not a standard vector method, should be push_back")
    return "\n".join(issues) if issues else "No heuristic issues found."


def code_analyzer_node(state: BugHunterState) -> dict:
    """Analyze the buggy code and extract APIs + candidate bug lines."""
    code = state["code"]
    context = state.get("context", "")

    static_output = _try_cppcheck(code)
    if not static_output:
        static_output = _regex_heuristics(code)

    llm = get_llm(temperature=0)

    user_content = f"CODE:\n{code}"
    if context:
        user_content += f"\n\nCONTEXT:\n{context}"
    if static_output:
        user_content += f"\n\nSTATIC ANALYSIS HINTS:\n{static_output}"

    text = invoke_with_retry(
        llm,
        [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_content)],
    )

    apis: list[str] = []
    candidates: list[dict] = []

    if "APIS:" in text:
        apis_section = text.split("APIS:")[1]
        if "CANDIDATES:" in apis_section:
            apis_section = apis_section.split("CANDIDATES:")[0]
        apis = [a.strip() for a in apis_section.strip().splitlines() if a.strip()]

    if "CANDIDATES:" in text:
        cand_section = text.split("CANDIDATES:")[1].strip()
        for line in cand_section.splitlines():
            parts = line.strip().split("|", 2)
            if len(parts) == 3:
                candidates.append(
                    {
                        "line_no": parts[0].strip(),
                        "content": parts[1].strip(),
                        "reason": parts[2].strip(),
                    }
                )

    # Generate search queries from APIs and candidate bug reasons
    search_queries = [api + " correct usage" for api in apis[:8]]
    
    # Add queries based on candidate bug content for better doc retrieval
    for cand in candidates[:5]:
        content = cand.get("content", "")
        # Extract function names from the candidate line
        funcs = re.findall(r'\.(\w+)\s*\(', content)
        for func in funcs[:2]:
            query = f"rdi {func} syntax parameters"
            if query not in search_queries:
                search_queries.append(query)

    print(f"  Extracted {len(apis)} APIs, {len(candidates)} candidate lines")

    return {
        "extracted_apis": apis,
        "candidate_lines": candidates,
        "static_analysis": static_output,
        "search_queries": search_queries,
        "iteration": 0,
        "max_iterations": state.get("max_iterations", 2),
    }
