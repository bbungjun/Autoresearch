"""#107 날짜순 행동 데이터 생성 도구.

[파이프라인] 로컬 raw 합성 이력 생성과 품질 검사를 연결한다.
[기능] 고정 날짜/세 seed 데이터를 신규 경로에 저장하고 감사 결과를 기록한다.
[비책임] production batch CLI·학습·평가·봉인 final은 실행하지 않는다.
"""

import argparse
from pathlib import Path
import time

from autoresearch.research_harness.behavior_data import BehaviorDataRequest, generate_behavior_data
from autoresearch.research_harness.behavior_data_audit import write_audit
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    audits = []
    for seed in (10701, 10702, 10703):
        root = args.output / f"world-{seed}"
        generate_behavior_data(root, BehaviorDataRequest(seed))
        audit = write_audit(root)
        audits.append({"seed": seed, "manifest_sha256": audit["manifest_sha256"],
                       "quality_passed": audit["quality_passed"], "events": audit["total_events"]})
        print(f"seed={seed} events={audit['total_events']} quality_passed={audit['quality_passed']}", flush=True)
    summary = {"worlds": audits, "elapsed_seconds": time.perf_counter() - started, "final_evaluations": 0}
    (args.output / "summary.json").write_bytes(canonical_json_bytes(summary))
    if not all(audit["quality_passed"] for audit in audits):
        raise SystemExit("behavior_quality_gate_failed")


if __name__ == "__main__":
    main()
