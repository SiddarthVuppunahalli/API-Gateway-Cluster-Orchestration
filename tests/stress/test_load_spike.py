import unittest
from unittest.mock import patch

from load_spike import build_workload, run_load_test
from run_k8s_failure_drill import summarize


class WorkloadTests(unittest.TestCase):
    def test_mixed_workload_is_deterministic_for_a_seed(self) -> None:
        first = build_workload(25, 256, 256, "mixed", seed=42)
        second = build_workload(25, 256, 256, "mixed", seed=42)
        different = build_workload(25, 256, 256, "mixed", seed=43)

        self.assertEqual(first, second)
        self.assertNotEqual(first, different)

    @patch("load_spike.send_request")
    def test_latency_percentiles_include_only_successes(self, send_request) -> None:
        def fake_request(_url, prompt_size, _max_tokens, _timeout, _api_key):
            if prompt_size == 64:
                return 200, 100.0, "ok"
            return 429, 1.0, "http_429"

        send_request.side_effect = fake_request
        summary = run_load_test(
            url="http://unused",
            requests=5,
            concurrency=1,
            prompt_size=256,
            max_tokens=256,
            timeout=1,
            workload="mixed",
            seed=42,
            api_key_count=2,
        )

        self.assertEqual(summary["successful_latency_samples"], 1)
        self.assertEqual(summary["p50_latency_ms"], 100.0)
        self.assertEqual(summary["p95_latency_ms"], 100.0)
        self.assertEqual(summary["status_counts"], {200: 1, 429: 4})

    def test_failure_drill_summary_separates_statuses_and_workers(self) -> None:
        summary = summarize(
            [
                {"status": 200, "worker_id": "worker-a", "latency_ms": 100.0},
                {"status": 200, "worker_id": "worker-c", "latency_ms": 200.0},
                {"status": 504, "worker_id": "", "latency_ms": 500.0},
            ]
        )

        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["successful"], 2)
        self.assertEqual(summary["status_counts"], {"200": 2, "504": 1})
        self.assertEqual(summary["worker_counts"], {"worker-a": 1, "worker-c": 1})
        self.assertEqual(summary["p95_success_latency_ms"], 100.0)


if __name__ == "__main__":
    unittest.main()
