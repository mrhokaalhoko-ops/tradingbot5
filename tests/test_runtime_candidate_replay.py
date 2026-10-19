import json
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

from utils.runtime_candidate_replay import load_decision_events, simulate_market_entry


class TestRuntimeCandidateReplay(unittest.TestCase):
    def test_loads_and_deduplicates_any_supported_export_horizon(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = []
            for part, horizon in ((1, "48h"), (2, "72h")):
                path = root / f"part{part}.zip"
                event = {
                    "event_id": "shared" if part == 2 else "first",
                    "timestamp": f"2026-09-0{part}T00:00:00Z",
                    "stage": "RR_FILTER",
                }
                duplicate = {
                    "event_id": "shared",
                    "timestamp": "2026-09-02T01:00:00Z",
                    "stage": "SNIPER",
                }
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(
                        f"decision_audit_{horizon}.jsonl",
                        "\n".join(json.dumps(item) for item in (event, duplicate)),
                    )
                paths.append(path)

            events = load_decision_events(paths)

        self.assertEqual([event["event_id"] for event in events], ["first", "shared"])

    def test_cli_help_runs_from_repository_root(self):
        project_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "scripts/replay_runtime_candidates.py", "--help"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Replay RR-pass candidates", result.stdout)

    def test_reports_same_bar_collision_as_ambiguous(self):
        result = simulate_market_entry(
            side="BUY",
            entry=100.0,
            stop=98.0,
            target=104.0,
            bars=[{
                "timestamp": "2026-01-01T00:15:00Z",
                "high": 104.5,
                "low": 97.5,
                "close": 101.0,
            }],
        )

        self.assertEqual(result["outcome"], "AMBIGUOUS_SAME_BAR")
        self.assertIsNone(result["final_r"])

    def test_buy_path_reports_tp_before_later_stop(self):
        result = simulate_market_entry(
            side="BUY",
            entry=100.0,
            stop=98.0,
            target=104.0,
            bars=[
                {
                    "timestamp": "2026-01-01T00:15:00Z",
                    "high": 103.0,
                    "low": 99.0,
                    "close": 102.0,
                },
                {
                    "timestamp": "2026-01-01T00:30:00Z",
                    "high": 104.2,
                    "low": 101.0,
                    "close": 104.0,
                },
                {
                    "timestamp": "2026-01-01T00:45:00Z",
                    "high": 104.0,
                    "low": 97.0,
                    "close": 98.0,
                },
            ],
        )

        self.assertEqual(result["outcome"], "TP_FIRST")
        self.assertEqual(result["final_r"], 2.0)

    def test_sell_path_reports_stop_first(self):
        result = simulate_market_entry(
            side="SELL",
            entry=100.0,
            stop=102.0,
            target=96.0,
            bars=[{
                "timestamp": "2026-01-01T00:15:00Z",
                "high": 102.2,
                "low": 99.0,
                "close": 101.5,
            }],
        )

        self.assertEqual(result["outcome"], "SL_FIRST")
        self.assertEqual(result["final_r"], -1.0)


if __name__ == "__main__":
    unittest.main()
