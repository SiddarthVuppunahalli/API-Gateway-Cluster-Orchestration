# Architecture Notes

## Design Goal

Model the control-plane behavior of an LLM-serving system without needing a real model backend. The gateway should make realistic scheduling decisions while workers simulate inference latency and capacity constraints.

## Current Request Path

The current code implements the full simulated request path:

1. Receive an inference request at the gateway.
2. Attempt to admit the request into a bounded in-memory queue.
3. Have a dispatcher goroutine pull the request from the queue.
4. Estimate request cost from prompt size and token budget.
5. Score workers using cached capacity snapshots refreshed on a periodic loop.
6. Wait and retry when the cluster is temporarily saturated instead of failing immediately on worker fullness.
7. Simulate generation on the worker and return a synthetic result.
8. Record queue, router, and request lifecycle counters for visibility.

## Components

### Gateway

Current:

- HTTP API
- request validation
- bounded admission queue
- asynchronous dispatch workers
- queue depth and rejection statistics
- periodic worker-state cache
- request-cost-aware worker scoring
- saturation-aware wait and retry behavior
- worker failover on request errors
- per-worker circuit breakers
- per-key token-bucket rate limiting
- Prometheus request and queue metrics

Next:

- richer heartbeat model
- request prioritization
- authenticated API keys for any public deployment
- bounded lifecycle management for rate-limit buckets

### Worker

Current:

- simulated latency from prompt length and token count
- synthetic capacity model
- health and capacity endpoints

Next:

- continuous batching simulation
- configurable heterogeneous worker classes
- degraded mode and failure injection

### Orchestration

Current:

- local Docker Compose topology
- Kubernetes Deployments and Services
- readiness and liveness probes
- repeatable strategy benchmark and worker-failure drill runners

Next:

- commit a measured Kubernetes failure-drill report
- scale-out experiments

## Routing Strategy

The router now uses a projected score based on cached state:

`score = (queued_tokens + estimated_request_cost) / max_concurrent_requests`

Where projected utilization reflects the request being considered, not just the current snapshot. This is intentionally more expressive than round robin while still being easy to reason about. Future phases can include:

- request-cost-aware admission
- heterogeneous worker weights
- latency feedback
- retry budgets

## Current Lightweight Metrics

- queue depth
- queue capacity
- dispatch worker count
- in-flight request count
- accepted, completed, rejected, and failed request totals
- cached worker count and healthy worker count
- available worker count and saturated worker count
- Prometheus request totals, request duration, and queue depth

## Metrics To Add

- request throughput
- worker selection distribution
- worker failure count
- retry count
- circuit-breaker state and rate-limit rejection counts

## Failure Scenarios To Test

- one worker returns 500s
- one worker becomes slow
- one worker disappears during traffic
- all workers saturate at once

## Portfolio Evidence

The project should tell a complete engineering story:

- clear problem statement
- visible architecture boundaries
- working services and deployment assets
- visible concurrency control rather than only synchronous proxying
- cached control-plane state rather than naive live probing on every request
- automated tests and CI checks
- a committed three-trial routing benchmark
- a reproducible Kubernetes failure drill awaiting a measured cluster run
