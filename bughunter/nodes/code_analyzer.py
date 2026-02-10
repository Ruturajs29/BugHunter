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

def _try_cppcheck(code: str) -> str:
    """Run cppcheck if available; return output or empty string."""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".cpp", mode="w", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(code)
            tmp.flush()
            result = subprocess.run(
                ["cppcheck", "--enable=all", "--quiet", "--force", tmp.name],
                capture_output=True,
                text=True,
                timeout=15,
            )
            Path(tmp.name).unlink(missing_ok=True)
            return (result.stdout + result.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


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


def _try_clang_tidy(code: str) -> str:
    """Run clang-tidy if available; return output or empty string."""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".cpp", mode="w", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(code)
            tmp.flush()
            result = subprocess.run(
                ["clang-tidy", tmp.name, "--", "-std=c++17"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            Path(tmp.name).unlink(missing_ok=True)
            output = result.stdout + result.stderr
            # Filter to only warning/error lines
            filtered = [l for l in output.splitlines() if "warning:" in l or "error:" in l]
            return "\n".join(filtered[:10])  # Limit output
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _regex_heuristics(code: str) -> str:
    """Enhanced regex-based checks for common C++ / RDI issues."""
    issues: list[str] = []
    lines = code.splitlines()
    
    # Track RDI lifecycle for ordering check
    rdi_begin_line = None
    rdi_end_line = None
    
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        
        # RDI lifecycle tracking
        if "RDI_BEGIN" in stripped:
            rdi_begin_line = i
        if "RDI_END" in stripped:
            rdi_end_line = i
            if rdi_begin_line is None or rdi_end_line < rdi_begin_line:
                issues.append(f"Line {i}: RDI_END appears before RDI_BEGIN - wrong lifecycle order")
        
        # vecEditMode check
        if re.search(r"\.vecEditMode\s*\(", stripped):
            issues.append(f"Line {i}: vecEditMode call, verify mode parameter (TA::VECD or TA::VTT)")
        
        # iClamp argument order check
        match = re.search(r"\.iClamp\s*\(\s*([^,]+),\s*([^)]+)\)", stripped)
        if match:
            arg1, arg2 = match.groups()
            # Check if first arg is positive (likely high) and second is negative (likely low)
            if re.search(r"^\s*\d", arg1.strip()) and re.search(r"^\s*-", arg2.strip()):
                issues.append(f"Line {i}: iClamp({arg1.strip()}, {arg2.strip()}) - possible reversed (low, high) order")
        
        # vForceRange value check
        match = re.search(r"\.vForceRange\s*\(\s*(\d+)\s*(V|mV)?", stripped)
        if match:
            value = int(match.group(1))
            if value > 30:
                issues.append(f"Line {i}: vForceRange({value}V) may exceed max allowed (30V for typical cards)")
        
        # push_forward (non-existent method)
        if re.search(r"push_forward", stripped):
            issues.append(f"Line {i}: push_forward is not a standard vector method, should be push_back")
        
        # Common RDI method typos/case errors
        if re.search(r"\.imeas\s*\(", stripped, re.IGNORECASE) and not re.search(r"\.iMeas\s*\(", stripped):
            issues.append(f"Line {i}: Possible typo - 'imeas' should be 'iMeas' (case-sensitive)")
        if re.search(r"\.vmeas\s*\(", stripped, re.IGNORECASE) and not re.search(r"\.vMeas\s*\(", stripped):
            issues.append(f"Line {i}: Possible typo - 'vmeas' should be 'vMeas' (case-sensitive)")
        if re.search(r"\.imeasrange\s*\(", stripped, re.IGNORECASE) and not re.search(r"\.iMeasRange\s*\(", stripped):
            issues.append(f"Line {i}: Possible typo - should be 'iMeasRange' (case-sensitive)")
        if re.search(r"\.imeans\s*\(", stripped, re.IGNORECASE):
            issues.append(f"Line {i}: 'iMeans' is not valid, should be 'iMeas'")
        if re.search(r"\.vmeans\s*\(", stripped, re.IGNORECASE):
            issues.append(f"Line {i}: 'vMeans' is not valid, should be 'vMeas'")
        
        # Duplicate method calls
        if re.search(r"\.end\s*\(\s*\)\s*\.end\s*\(", stripped):
            issues.append(f"Line {i}: Duplicate .end() call detected")
        if re.search(r"\.burst\s*\([^)]*\)\s*\.burst\s*\(", stripped):
            issues.append(f"Line {i}: Duplicate .burst() call detected, should likely be .execute()")
        
        # samples value check
        match = re.search(r"\.samples\s*\(\s*(\d+)\s*\)", stripped)
        if match:
            samples = int(match.group(1))
            if samples > 8192:
                issues.append(f"Line {i}: samples({samples}) exceeds max 8192 for burst site upload")
    
    return "\n".join(issues) if issues else "No heuristic issues found."


def _run_static_analysis(code: str) -> str:
    """Run available static analysis tools, returning combined output."""
    outputs = []
    
    # Try cpplint first (most commonly available via pip)
    cpplint_out = _try_cpplint(code)
    if cpplint_out:
        outputs.append(f"[cpplint]\n{cpplint_out}")
    
    # Try cppcheck
    cppcheck_out = _try_cppcheck(code)
    if cppcheck_out:
        outputs.append(f"[cppcheck]\n{cppcheck_out}")
    
    # Try clang-tidy if others didn't find anything
    if not cpplint_out and not cppcheck_out:
        clang_out = _try_clang_tidy(code)
        if clang_out:
            outputs.append(f"[clang-tidy]\n{clang_out}")
    
    # Always run regex heuristics for RDI-specific checks
    regex_out = _regex_heuristics(code)
    if regex_out and "No heuristic issues" not in regex_out:
        outputs.append(f"[RDI-heuristics]\n{regex_out}")
    
    return "\n\n".join(outputs) if outputs else "No static analysis issues found."


def code_analyzer_node(state: BugHunterState) -> dict:
    """Analyze the buggy code and extract APIs + candidate bug lines."""
    code = state["code"]
    context = state.get("context", "")

    # Run enhanced static analysis (cppcheck, clang-tidy, and RDI heuristics)
    static_output = _run_static_analysis(code)

    llm = get_llm(temperature=0)

    user_content = f"CODE:\n{code}"
    if context:
        user_content += f"\n\nCONTEXT:\n{context}"
    if static_output and "No static analysis issues" not in static_output:
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
