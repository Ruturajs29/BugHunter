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
You will receive a snippet of C++ code, context describing the bug, and static analysis hints.

Your job:
1. Number every line of the code starting from 1.
2. List every RDI API / function call (e.g. rdi.dc().vForce(), rdi.smartVec().vecEditMode()).
3. Read the CONTEXT carefully - it usually describes WHAT BUG exists in the code.
4. Find ONLY the line(s) that match the bug described in CONTEXT.

BUG TYPES TO LOOK FOR:
- Wrong mode/constant values (e.g. VECD instead of VTT)
- Misspelled function names (e.g. iMeans instead of iMeas)
- Swapped argument order (e.g. iClamp(high, low) instead of iClamp(low, high))
- Wrong lifecycle ordering (RDI_END before RDI_BEGIN)
- Pin name mismatch between related operations
- Wrong terminal method (e.g. .burst() instead of .execute())
- Values outside documented ranges

CRITICAL RULES:
- Focus on the bug described in CONTEXT. Do not invent additional bugs.
- Only flag lines with DEFINITE errors, not "suspicious" code.
- Do NOT flag RDI_BEGIN or RDI_END unless they are in wrong order.
- Do NOT flag valid method chaining.

Output EXACTLY (no markdown fences):

APIS:
<api_1>
<api_2>
...

CANDIDATES:
<line_number>|<line_content>|<what is wrong and what it should be>
"""

def _try_cpplint(code: str) -> str:
    """Run cpplint if available; return output or empty string."""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".cpp", mode="w", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(code)
            tmp.flush()
            result = subprocess.run(
                ["cpplint", "--quiet", tmp.name],
                capture_output=True,
                text=True,
                timeout=15,
            )
            Path(tmp.name).unlink(missing_ok=True)
            output = result.stderr  # cpplint outputs to stderr
            # Filter to relevant lines (skip "Done processing" etc)
            filtered = [l for l in output.splitlines() 
                       if l.strip() and "Done processing" not in l and "Total errors" not in l]
            return "\n".join(filtered[:15])  # Limit output
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _run_static_analysis(code: str) -> str:
    """Run cpplint static analysis."""
    cpplint_out = _try_cpplint(code)
    if cpplint_out:
        return f"[cpplint]\n{cpplint_out}"
    return ""


def code_analyzer_node(state: BugHunterState) -> dict:
    """Analyze the buggy code and extract APIs + candidate bug lines."""
    code = state["code"]
    context = state.get("context", "")

    # Run static analysis (cpplint only)
    static_output = _run_static_analysis(code)

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
