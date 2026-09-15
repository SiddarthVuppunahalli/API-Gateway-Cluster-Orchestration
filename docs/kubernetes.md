# Kubernetes Deployment Notes

This phase introduces a Kubernetes-ready deployment layout for the simulated inference cluster. The goal is to make the system schedulable and observable as a small multi-service cluster before adding failure drills.

## What This Phase Adds

- a dedicated namespace: `llm-sim`
- separate worker Deployments so heterogeneous worker behavior is preserved
- ClusterIP Services for worker discovery
- a ConfigMap-driven gateway Deployment
- readiness and liveness probes for both gateway and workers

## Why Separate Worker Deployments

The worker nodes intentionally have different capacity and latency profiles:

- `worker-a`: medium latency, medium concurrency
- `worker-b`: slower, lower concurrency
- `worker-c`: faster, higher concurrency

Keeping them separate makes it easier to:

- preserve heterogeneous scheduling behavior
- simulate targeted failure of a single worker class
- observe how the gateway reacts to losing one node

## Local Cluster Workflow

These manifests are written for local clusters such as:

- `kind`
- `minikube`
- Docker Desktop Kubernetes

### 1. Build Images

```bash
docker build -f deploy/docker/Dockerfile.gateway -t llm-gateway:local .
docker build -f deploy/docker/Dockerfile.worker -t llm-worker:local .
```

### 2. Load Images Into The Cluster

For `kind`:

```bash
kind load docker-image llm-gateway:local
kind load docker-image llm-worker:local
```

For `minikube`, use the local daemon or `minikube image load`.

### 3. Apply Manifests

```bash
kubectl apply -k deploy/k8s
```

### 4. Check Health

```bash
kubectl get pods -n llm-sim
kubectl get svc -n llm-sim
```

### 5. Reach The Gateway

```bash
kubectl port-forward -n llm-sim svc/gateway 8080:8080
```

Then access:

- `http://localhost:8080/healthz`
- `http://localhost:8080/stats`
- `http://localhost:8080/infer`

## Worker Failure Test

Run the automated drill against an already deployed local cluster:

```bash
python tests/stress/run_k8s_failure_drill.py \
  --output docs/benchmark-results/k8s-failure-drill.json
```

The runner deliberately deletes the current `worker-b` pod while sending traffic,
waits for the Deployment replacement to become ready, and records:

- whether traffic keeps flowing through the remaining workers
- how quickly the gateway stops relying on the missing worker
- whether request failures remain bounded during recovery
- how cluster recovery interacts with cached worker state

The generated Markdown and JSON are suitable for committing as evidence. Do not
claim measured failure recovery until a report from a real run is present.

## What Success Looks Like

For this phase, success means:

- the gateway and workers deploy cleanly in Kubernetes
- probes reflect actual service availability
- service discovery works through Kubernetes Services
- the project is ready for cluster-level failure injection
