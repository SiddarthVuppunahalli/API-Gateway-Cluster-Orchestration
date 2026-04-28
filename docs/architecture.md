# Architecture Notes

## Design Goal

Model the control-plane behavior of an LLM-serving system without needing a real model backend. The gateway should make realistic scheduling decisions while workers simulate inference latency and capacity constraints.

## Current Phase

The current code now covers the first concurrency milestone:

1. Receive an inference request at the gateway.
2. Attempt to admit the request into a bounded in-memory queue.
3. Have a dispatcher goroutine pull the request from the queue.
4. Estimate request cost from prompt size and token budget.
5. Score workers using cached capacity snapshots refreshed on a periodic loop.
6. Simulate generation on the worker and return a synthetic result.
7. Record queue, router, and request lifecycle counters for visibility.

## Planned Evolution

### Gateway

Current:

- HTTP API
- request validation
- bounded admission queue
- asynchronous dispatch workers
- queue depth and rejection statistics
- periodic worker-state cache
- request-cost-aware worker scoring
- worker failover on request errors

Next:

- richer heartbeat model
- request prioritization
- retry policy with circuit-breaking behavior
- Prometheus-compatible metrics exposure

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

Next:

- Kubernetes Deployments and Services
- readiness and liveness probes
- scale-out experiments

## Routing Strategy

The router now uses a projected score based on cached state:

`score ~= projected_utilization + projected_queued_tokens`

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

## Metrics To Add

- request throughput
- worker selection distribution
- worker failure count
- retry count
- p50, p95, and p99 latency

## Failure Scenarios To Test

- one worker returns 500s
- one worker becomes slow
- one worker disappears during traffic
- all workers saturate at once

## Why This Reads Well On GitHub

The project should tell a complete engineering story:

- clear problem statement
- visible architecture boundaries
- working starter services
- visible concurrency control rather than only synchronous proxying
- cached control-plane state rather than naive live probing on every request
- phased roadmap with measurable goals
- benchmark artifacts once later phases land
