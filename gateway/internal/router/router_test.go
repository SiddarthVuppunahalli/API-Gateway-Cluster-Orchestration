package router

import (
	"testing"
	"time"

	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/types"
	"github.com/sony/gobreaker/v2"
)

func TestEstimateRequestCost(t *testing.T) {
	req := types.InferenceRequest{Prompt: "12345678", MaxTokens: 10}
	if got, want := estimateRequestCost(req), 12; got != want {
		t.Fatalf("estimateRequestCost() = %d, want %d", got, want)
	}
}

func TestNormalizeStrategy(t *testing.T) {
	tests := map[string]string{
		"round_robin": "round_robin",
		"round-robin": "round_robin",
		"RR":          "round_robin",
		"cost":        "cost",
		"unknown":     "cost",
	}

	for input, want := range tests {
		if got := normalizeStrategy(input); got != want {
			t.Errorf("normalizeStrategy(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestCostAwareWorkersPrefersCapacityAdjustedLoad(t *testing.T) {
	now := time.Now()
	r := &Router{
		workerURLs: []string{"worker-small", "worker-large"},
		staleAfter: time.Second,
		workers: map[string]*cachedWorker{
			"worker-small": testWorker("worker-small", 2, 0, now),
			"worker-large": testWorker("worker-large", 10, 100, now),
		},
	}

	ranked := r.costAwareWorkers(100)
	if len(ranked) != 2 {
		t.Fatalf("ranked worker count = %d, want 2", len(ranked))
	}
	if ranked[0].baseURL != "worker-large" {
		t.Fatalf("first worker = %q, want worker-large", ranked[0].baseURL)
	}
}

func TestCostAwareWorkersOmitsStaleWorkers(t *testing.T) {
	now := time.Now()
	r := &Router{
		workerURLs: []string{"fresh", "stale"},
		staleAfter: time.Second,
		workers: map[string]*cachedWorker{
			"fresh": testWorker("fresh", 4, 0, now),
			"stale": testWorker("stale", 4, 0, now.Add(-2*time.Second)),
		},
	}

	ranked := r.costAwareWorkers(10)
	if len(ranked) != 1 || ranked[0].baseURL != "fresh" {
		t.Fatalf("ranked workers = %#v, want only fresh", ranked)
	}
}

func testWorker(url string, maxConcurrent, queuedTokens int, updated time.Time) *cachedWorker {
	return &cachedWorker{
		baseURL: url,
		capacity: types.WorkerCapacity{
			WorkerID:      url,
			QueuedTokens:  queuedTokens,
			MaxConcurrent: maxConcurrent,
			Healthy:       true,
		},
		lastUpdated: updated,
		cb: gobreaker.NewCircuitBreaker[types.WorkerGenerateResponse](gobreaker.Settings{
			Name: url,
		}),
	}
}
