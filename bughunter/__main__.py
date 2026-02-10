"""CLI entry point for the BugHunter pipeline."""

from __future__ import annotations

import argparse
import sys
import time

from bughunter.csv_io import load_input_csv, write_output_csv
from bughunter.graph import build_graph


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BugHunter Blackbox - C++ Bug Finder"
    )
    parser.add_argument(
        "--input", "-i", required=True, help="Path to input CSV file"
    )
    parser.add_argument(
        "--output", "-o", default="output.csv", help="Path to output CSV file"
    )
    args = parser.parse_args()

    rows = load_input_csv(args.input)
    print(f"Loaded {len(rows)} rows from {args.input}")

    app = build_graph()

    results: list[dict] = []
    total = len(rows)

    for idx, row in enumerate(rows, 1):
        row_id = row["id"]
        print(f"[{idx}/{total}] Processing ID={row_id}")
        t0 = time.time()

        try:
            initial_state = {
                "id": row["id"],
                "code": row["code"],
                "context": row["context"],
                "iteration": 0,
                "max_iterations": 2,
            }
            if row.get("correct_code") and row["correct_code"] != "nan":
                initial_state["correct_code"] = row["correct_code"]
            if row.get("explanation") and row["explanation"] != "nan":
                initial_state["explanation"] = row["explanation"]

            final_state = app.invoke(initial_state)

            results.append(
                {
                    "id": final_state.get("id", row_id),
                    "bug_line": final_state.get("bug_line", "Unable to identify"),
                    "bug_explanation": final_state.get(
                        "bug_explanation", "Analysis failed"
                    ),
                }
            )
        except Exception as e:
            print(f"  Error processing ID={row_id}: {e}")
            results.append(
                {
                    "id": row_id,
                    "bug_line": "ERROR",
                    "bug_explanation": f"Processing error: {e}",
                }
            )

        elapsed = time.time() - t0
        print(f"  Done in {elapsed:.1f}s")

        if idx < total:
            wait_s = 5
            print(f"  Waiting {wait_s}s for rate limit cooldown ...")
            time.sleep(wait_s)

    write_output_csv(results, args.output)


if __name__ == "__main__":
    main()
