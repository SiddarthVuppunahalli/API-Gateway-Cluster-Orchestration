# Stress Testing Plan

This directory will hold synthetic load-generation and failure-injection assets.

Planned scenarios:

- mixed short and long prompt distributions
- sudden traffic spikes
- worker saturation tests
- worker termination during active traffic

Target outputs:

- throughput and latency summaries
- retry and failure counts
- worker utilization snapshots

Current starter artifact:

- `load_spike.py`: fires a concurrent burst at the gateway and prints status-code distribution plus simple latency percentiles

Example usage:

```bash
python tests/stress/load_spike.py --requests 500 --concurrency 128
```

Mixed workload example:

```bash
python tests/stress/load_spike.py --requests 500 --concurrency 128 --workload mixed
```

Suggested comparison workflow:

```bash
ROUTING_STRATEGY=round_robin go run ./gateway/cmd/server
python tests/stress/load_spike.py --requests 300 --concurrency 64
```

```bash
ROUTING_STRATEGY=cost go run ./gateway/cmd/server
python tests/stress/load_spike.py --requests 300 --concurrency 64
```

Automated comparison workflow:

- start the worker processes first
- then run:

```bash
python tests/stress/run_strategy_benchmark.py --requests 300 --concurrency 64 --workload mixed
```

Outputs:

- console summaries for both strategies
- live progress messages while the benchmark is running
- a saved JSON report in `benchmarks/`
- a saved Markdown summary in `benchmarks/`
- automatic cleanup of a leftover listener on the gateway port before the run starts

The saved artifacts also include failure-reason counts so non-HTTP failures are no longer hidden inside a generic `Other` bucket.

Kubernetes comparison workflow:

- make sure the cluster deployment is already running
- then run:

```bash
python tests/stress/run_k8s_strategy_benchmark.py --requests 300 --concurrency 64 --workload mixed
```

This runner:

- patches the gateway ConfigMap strategy
- restarts the gateway Deployment between runs
- uses `kubectl port-forward` to hit the in-cluster Service
- restores the original strategy when finished

Optional flag:

- `--skip-cleanup` if you do not want the script to clear an existing process on the gateway port

If the benchmark runner fails before `/healthz` is ready:

- make sure no old gateway process is still using port `8080`
- make sure `go run ./gateway/cmd/server` works by itself from the repo root
- rerun the script and check the printed gateway output for the exact startup error
