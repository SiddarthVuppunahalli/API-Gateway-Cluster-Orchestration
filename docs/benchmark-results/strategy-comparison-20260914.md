# Local Routing Strategy Benchmark

This report compares the gateway's round-robin and cost-aware routing policies under the same deterministic mixed workload.

## Methodology

- Generated at (UTC): `20260915T063226Z`
- Trials per strategy: `3`
- Requests per trial: `300` at concurrency `64`
- Workload: `mixed` with deterministic seed `42`
- Synthetic API-key buckets: `64`
- Client timeout: `25.0` seconds; gateway timeout: `20` seconds
- Strategy execution order alternates between trials to reduce ordering bias.
- Latency percentiles include successful HTTP 200 responses only.
- API-key values are synthetic rate-limit bucket identifiers, not credentials.

## Median results

| Strategy | Successful | Successful req/sec | P50 ms | P95 ms | P99 ms | 429s | 503s | 504s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `round_robin` | 298/300 | 10.54 | 4317.98 | 7756.91 | 12860.74 | 0 | 0 | 2 |
| `cost` | 293/300 | 10.76 | 4344.93 | 7402.69 | 7953.00 | 0 | 0 | 7 |

## Observed result

Across the median trial, cost-aware routing delivered `10.76` successful requests/sec versus `10.54` for round-robin (`+2.1%`).

Its median P95 was `7402.69 ms` versus `7756.91 ms` (`4.6%` lower), and median P99 was `7953.00 ms` versus `12860.74 ms` (`38.2%` lower).

The trade-off was completion rate: cost-aware routing completed a median `293/300` requests versus `298/300` for round-robin (`-1.7` percentage points), with the remaining requests returning `504` at the gateway deadline.

These results suggest a tail-latency and throughput benefit under this workload, paired with a small completion-rate regression. They support discussing a measurable scheduling trade-off, not claiming that cost-aware routing is universally superior.

## Individual trials

| Trial | Strategy | Successful | Successful req/sec | P50 ms | P95 ms | P99 ms | 429s | 503s | 504s | Other |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `round_robin` | 292/300 | 10.45 | 4268.98 | 7330.06 | 7879.85 | 0 | 0 | 8 | 0 |
| 1 | `cost` | 300/300 | 10.67 | 4400.06 | 10554.03 | 13927.68 | 0 | 0 | 0 | 0 |
| 2 | `cost` | 293/300 | 10.77 | 4240.38 | 7402.69 | 7687.28 | 0 | 0 | 7 | 0 |
| 2 | `round_robin` | 298/300 | 10.64 | 4317.98 | 7756.91 | 15313.37 | 0 | 0 | 2 | 0 |
| 3 | `round_robin` | 298/300 | 10.54 | 4347.46 | 9300.37 | 12860.74 | 0 | 0 | 2 | 0 |
| 3 | `cost` | 292/300 | 10.76 | 4344.93 | 7373.25 | 7953.00 | 0 | 0 | 8 | 0 |

## Interpretation

Higher successful throughput and completion count are better; lower latency percentiles are better. These measurements characterize this simulator and workload only. They should not be generalized to real model serving or presented as proof that one routing policy always wins.

The raw JSON report contains status counts, failure reasons, gateway snapshots, worker snapshots, and process logs for every trial.
