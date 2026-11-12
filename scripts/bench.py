import argparse
import statistics
from time import perf_counter
from uuid import uuid4

from node.verdict import TemplateVerdict
from satcore.demo import cases
from satcore.models import AOI, UplinkCommand
from satcore.pipeline import run_analysis


def main():
    parser = argparse.ArgumentParser(
        description="Measure offline analysis stages across demo cases."
    )
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    for case in cases():
        measurements: dict[str, list[float]] = {"total": []}
        for _ in range(args.runs):
            cmd = UplinkCommand(
                analysis_id=uuid4(),
                aoi=AOI.model_validate(case["aoi"]),
                date_a=case["date_a"],
                date_b=case["date_b"],
                intent=case["intent"],
                demo_id=case["id"],
            )
            start = perf_counter()
            packet = run_analysis(cmd, lambda *_: None, TemplateVerdict())
            measurements["total"].append((perf_counter() - start) * 1000)
            for stage, ms in packet.timings_ms.items():
                measurements.setdefault(stage, []).append(ms)
        print(f"{case['title']} ({args.runs} runs)")
        for stage, times in measurements.items():
            p95 = sorted(times)[min(len(times) - 1, int(0.95 * len(times)))]
            print(
                f"  {stage:<18} median {statistics.median(times):7.1f} ms  p95 {p95:7.1f} ms"
            )


if __name__ == "__main__":
    main()
