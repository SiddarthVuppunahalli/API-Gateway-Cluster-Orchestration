# Simulated LLM Inference Gateway & Cluster Orchestration

A small distributed system for exploring the infrastructure around LLM inference: admission control, backpressure, request scheduling, worker health, failure isolation, and cluster orchestration.

The workers do not run a real model. They simulate inference cost so the project can focus on the harder control-plane question: **how should a gateway keep an uneven worker cluster useful when traffic is bursty and requests have very different costs?**

## What this project demonstrates

- **Concurrent request dispatch:** a Go gateway feeds admitted requests to a configurable goroutine worker pool.
- **Bounded queuing and backpressure:** the gateway uses a fixed-size in-memory queue and rejects excess work instead of allowing unbounded latency and memory growth.
- **Compute-aware scheduling:** requests are scored from prompt length and token budget, then routed using cached worker capacity and projected load.
- **A meaningful baseline:** the same gateway can use round-robin routing, making the scheduling policy directly benchmarkable.
- **Heterogeneous workers:** three Python/FastAPI workers simulate different latency and concurrency profiles.
- **Failure handling:** stale health data, dispatch errors, saturation retries, and per-worker circuit breakers keep unhealthy workers out of the request path.
- **Operational controls:** token-bucket rate limiting, live gateway statistics, Prometheus metrics, Docker Compose, and Kubernetes health probes and Services.

## Architecture

```mermaid
flowchart LR
    C[Clients / load generator] -->|POST /infer| G[Go API gateway]
    G --> A[Bounded admission queue]
    A --> D[Goroutine dispatch pool]
    D --> R{Routing strategy}
    R -->|cost-aware or round-robin| WA[Python worker A<br/>8 slots]
    R -->|cost-aware or round-robin| WB[Python worker B<br/>6 slots, slower]
    R -->|cost-aware or round-robin| WC[Python worker C<br/>10 slots, faster]
    WA & WB & WC -->|capacity snapshots| R
    G --> S[/stats]
    G --> M[/metrics]
```

### Request lifecycle

1. The gateway validates the request and applies a per-API-key token-bucket limit.
2. It attempts to place the request in a bounded queue. A full queue returns `429`.
3. A dispatcher goroutine estimates cost from prompt length and requested output tokens.
4. The router selects a healthy worker using either cost-aware or round-robin routing.
5. A saturated cluster is retried until capacity becomes available or the request deadline expires.
6. The selected worker sleeps for a cost-dependent interval and returns a synthetic completion.
7. Queue, request, and worker state are exposed through `/stats` and `/metrics`.

## Five-minute demo

### Prerequisites

- Docker with Docker Compose
- Python 3.10+ for the optional load test

### 1. Start the cluster

From the repository root:

```bash
docker compose -f deploy/docker/docker-compose.yml up --build -d
```

Wait until the gateway reports healthy:

```bash
curl http://localhost:8080/healthz
```

Expected response:

```json
{"status":"ok"}
```

### 2. Send a simulated inference request

```bash
curl -X POST http://localhost:8080/infer \
  -H "Content-Type: application/json" \
  -H "X-API-Key: portfolio-demo" \
  -d '{"prompt":"Explain why bounded queues protect a service during traffic spikes.","max_tokens":128}'
```

The response identifies the selected worker and reports simulated latency:

```json
{
  "worker_id": "worker-c",
  "output": "Simulated completion for prompt length 67 and max_tokens 128.",
  "latency_ms": 673
}
```

`X-API-Key` currently identifies a rate-limit bucket; it is not an authentication credential. Do not expose the gateway publicly without adding real authentication or placing it behind an authenticated proxy.

### 3. Create a traffic spike

```bash
python tests/stress/load_spike.py --requests 100 --concurrency 32 --workload mixed
```

The script reports throughput, status-code counts, failure reasons, and p50/p95/p99 latency. The mixed workload rotates through different prompt and token sizes so scheduling decisions matter.

### 4. Inspect the gateway

```bash
curl http://localhost:8080/stats
curl http://localhost:8080/metrics
```

Look for queue depth, accepted/completed/rejected requests, in-flight work, worker health, and the active routing strategy.

### 5. Stop the cluster

```bash
docker compose -f deploy/docker/docker-compose.yml down
```

## Compare routing strategies

This is the strongest technical demo because it turns the scheduler design into an experiment.

Start only the simulated workers in Docker:

```bash
docker compose -f deploy/docker/docker-compose.yml up --build -d worker-a worker-b worker-c
```

With Go and Python installed locally, run the automated comparison:

```bash
python tests/stress/run_strategy_benchmark.py \
  --requests 300 \
  --concurrency 64 \
  --workload mixed
```

The runner builds the gateway once, runs three trials per strategy, alternates strategy order, applies the same seeded workload to both, and writes JSON plus Markdown reports under `benchmarks/`.

Interpret the results carefully: the cost-aware policy is intentionally a simple scheduler, and existing runs do not show a universal winner. The useful engineering story is the repeatable comparison, the observable trade-offs, and the ability to refine the policy from evidence—not a claim that one algorithm always wins.

See the [representative three-trial report](docs/benchmark-results/strategy-comparison-20260914.md), [Benchmark findings](docs/benchmarks.md), and [Stress testing](tests/stress/README.md) for methodology and additional options.

## Kubernetes demo

The Kubernetes manifests preserve the workers' different capacity profiles and expose each worker through a ClusterIP Service.

```bash
docker build -f deploy/docker/Dockerfile.gateway -t llm-gateway:local .
docker build -f deploy/docker/Dockerfile.worker -t llm-worker:local .
kubectl apply -k deploy/k8s
kubectl get pods,services -n llm-sim
kubectl port-forward -n llm-sim service/gateway 8080:8080
```

For `kind`, load both local images before applying the manifests:

```bash
kind load docker-image llm-gateway:local
kind load docker-image llm-worker:local
```

Once the gateway is reachable, the Kubernetes benchmark runner switches strategies by patching the ConfigMap and restarting the gateway Deployment:

```bash
python tests/stress/run_k8s_strategy_benchmark.py \
  --requests 300 \
  --concurrency 64 \
  --workload mixed
```

Detailed setup and failure-test ideas are in [Kubernetes deployment notes](docs/kubernetes.md).

## API surface

| Service | Endpoint | Purpose |
| --- | --- | --- |
| Gateway | `GET /healthz` | Process health |
| Gateway | `POST /infer` | Submit simulated inference work |
| Gateway | `GET /stats` | Queue, lifecycle, and router snapshot |
| Gateway | `GET /metrics` | Prometheus metrics |
| Worker | `GET /healthz` | Worker health |
| Worker | `GET /capacity` | Current load and capacity |
| Worker | `POST /generate` | Run a simulated generation |

## Configuration

| Variable | Default | Description |
| --- | ---: | --- |
| `ROUTING_STRATEGY` | `cost` | `cost` or `round_robin` |
| `QUEUE_CAPACITY` | `256` | Maximum requests waiting at the gateway |
| `DISPATCH_WORKERS` | `32` | Number of concurrent gateway dispatchers |
| `REQUEST_TIMEOUT_SECONDS` | `8` | End-to-end gateway request deadline |
| `WORKER_REFRESH_MS` | `1500` | Capacity-cache refresh interval |
| `WORKER_STALE_AFTER_MS` | `5000` | Age after which a worker snapshot is unusable |
| `WORKER_URLS` | local ports 9001–9003 | Comma-separated worker endpoints |

Worker behavior is controlled with `WORKER_ID`, `BASE_DELAY_MS`, `JITTER_MS`, and `MAX_CONCURRENT`.

## Observability

Prometheus can scrape the gateway directly. An optional local Prometheus/Grafana stack is included:

```bash
docker compose \
  -f deploy/docker/docker-compose.yml \
  -f deploy/docker/docker-compose.observability.yml \
  up --build
```

- Gateway: <http://localhost:8080>
- Prometheus: <http://localhost:9090>
- Grafana: <http://localhost:3000> (default local credentials: `admin` / `admin`)

## Design trade-offs

| Choice | Benefit | Limitation |
| --- | --- | --- |
| Simulated inference | Fast systems experiments without a GPU | Results do not represent real model throughput |
| Bounded in-memory queue | Explicit overload behavior and bounded memory | Queue contents do not survive a gateway restart |
| Cached worker capacity | Avoids probing every worker for every request | Routing decisions can briefly use stale state |
| Cost estimate from input size | Cheap and easy to explain | It is only a proxy for real compute cost |
| Retry on saturation | Lets short bursts drain instead of failing immediately | Can increase tail latency under sustained overload |

More reasoning is documented in [Concepts and engineering notes](docs/concepts.md) and [Architecture notes](docs/architecture.md).

## Repository layout

```text
gateway/          Go API gateway, queue, router, rate limiter, and metrics
worker/           Python/FastAPI inference simulator
deploy/docker/    Container images, Compose topology, and observability stack
deploy/k8s/       Kubernetes Deployments, Services, probes, and ConfigMap
tests/stress/     Burst generator and routing-strategy benchmark runners
docs/             Architecture, concepts, Kubernetes, and benchmark notes
benchmarks/       Locally generated benchmark artifacts (ignored by Git)
```

## Suggested portfolio presentation

The best presentation is a short recorded demo linked near the top of this README, backed by the reproducible local steps above:

1. Show the three workers with different capacities in `/stats`.
2. Run the mixed traffic spike and point out latency and completion counts.
3. Show queue and router state after the run.
4. Run or summarize the round-robin versus cost-aware comparison.
5. End on one trade-off or next experiment instead of claiming unrealistic production performance.

A permanently hosted public cluster is optional. Because the project needs four continuously running services and exposes a load-generating endpoint, a video plus local demo is usually clearer and cheaper. If a live URL is important, deploy one public gateway and three private worker services using a multi-service infrastructure-as-code definition, add strict rate limits, and keep the workload intentionally small.

## Scope

This is an infrastructure simulation, not a production inference platform. It does not load model weights, stream tokens, persist requests, authenticate users, or autoscale from real accelerator telemetry. Those boundaries are intentional: the repository is designed to make gateway and orchestration decisions easy to inspect, run, and discuss.
