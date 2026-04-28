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
