"""Run fixed fictional investigation cases with the existing signed-in Codex CLI.

No mail providers or production data are opened. The output contains only demo text.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from email_reader.context import ApplicationContext
from email_reader.demo import populate_demo
from email_reader.investigation import InvestigationService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cases", help="Comma-separated 1-based fictional case numbers")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = json.loads((Path(__file__).parents[1] / "tests/fixtures/investigator_cases.json").read_text())
    os.environ["EMAIL_READER_TESTING"] = "1"
    with tempfile.TemporaryDirectory(prefix="email-reader-agent-eval-") as directory:
        os.environ["EMAIL_READER_DATA_DIR"] = directory
        context = ApplicationContext.create(testing=True)
        try:
            context.settings.ai_backend = "codex"
            populate_demo(context.repository)
            service = InvestigationService(context.repository, context.search, context.openai,
                                           context.settings)
            recorded = []
            operations = []
            original_structured = context.openai.structured
            def trace_structured(*structured_args, **structured_kwargs):
                operations.append(structured_kwargs.get("operation", ""))
                result = original_structured(*structured_args, **structured_kwargs)
                if structured_kwargs.get("operation") == "investigation_answer":
                    recorded.append(result.model_dump(mode="json"))
                return result
            context.openai.structured = trace_structured
            selected_cases = ([index for index in range(len(cases)) if index + 1 in
                               {int(value) for value in args.cases.split(",")}]
                              if args.cases else list(range(min(len(cases), max(0, args.limit)))))
            results = []
            for index in selected_cases:
                case = cases[index]
                recorded.clear()
                operations.clear()
                context.openai.reset_cancellation()
                started = time.monotonic()
                result = service.investigate(case["question"], account_id=None,
                                              deadline=started + 60)
                answer = result["answer"]
                citations = [context.repository.citation_for_chunk(identifier)
                             for identifier in answer.source_ids]
                cited = bool(citations) and all(citations)
                normalized_answer = answer.text.casefold().replace(" percent", "%")
                normalized_expected = case.get("expected", "").casefold().replace(" percent", "%")
                one_final_answer = operations.count("investigation_answer") == 1
                if case["kind"] == "uncertain":
                    passed = (answer.insufficient_evidence and result["status"] == "completed"
                              and cited and one_final_answer)
                else:
                    passed = (result["status"] == "completed" and cited
                              and normalized_expected in normalized_answer and one_final_answer)
                item = {"case": index + 1, "kind": case["kind"], "passed": passed,
                        "status": result["status"], "cited": cited,
                        "seconds": round(time.monotonic()-started, 2),
                        "activity": result["activity"], "operations": operations.copy(),
                        "question": case["question"],
                        "answer": answer.text, "model_answer": recorded[-1] if recorded else None}
                results.append(item)
                print(f"{index + 1:02}/{len(cases)} {case['kind']} "
                      f"{'PASS' if passed else 'REVIEW'} {item['seconds']}s", flush=True)
            report = {"answerable_passed": sum(i["passed"] for i in results if i["kind"] == "answer"),
                      "uncertain_passed": sum(i["passed"] for i in results if i["kind"] == "uncertain"),
                      "answerable_total": sum(i["kind"] == "answer" for i in results),
                      "uncertain_total": sum(i["kind"] == "uncertain" for i in results),
                      "cases": results}
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
            print(json.dumps({key: value for key, value in report.items() if key != "cases"}), flush=True)
            if len(results) < len(cases):
                return 0
            return 0 if report["answerable_passed"] >= 12 and report["uncertain_passed"] == 5 else 1
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
