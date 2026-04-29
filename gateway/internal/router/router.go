package router

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/types"
	"github.com/sony/gobreaker/v2"
)

var errWorkerSaturated = errors.New("worker saturated")

type workerState struct {
	baseURL string
	score   float64
}

type cachedWorker struct {
	baseURL           string
	capacity          types.WorkerCapacity
	lastUpdated       time.Time
	consecutiveErrors int
	cb                *gobreaker.CircuitBreaker[types.WorkerGenerateResponse]
}

type Router struct {
	client          *http.Client
	workerURLs      []string
	strategy        string
	refreshInterval time.Duration
	staleAfter      time.Duration
	mu              sync.RWMutex
	workers         map[string]*cachedWorker
	rrCounter       atomic.Uint64
}

func New(workerURLs []string, strategy string, refreshInterval, staleAfter time.Duration) *Router {
	r := &Router{
		client: &http.Client{
			Timeout: 5 * time.Second,
		},
		workerURLs:      workerURLs,
		strategy:        normalizeStrategy(strategy),
		refreshInterval: refreshInterval,
		staleAfter:      staleAfter,
		workers:         make(map[string]*cachedWorker, len(workerURLs)),
	}

	for _, workerURL := range workerURLs {
		r.workers[workerURL] = &cachedWorker{
			baseURL: workerURL,
			capacity: types.WorkerCapacity{
				WorkerID:      workerNameFromURL(workerURL),
				MaxConcurrent: 1,
			},
			cb: gobreaker.NewCircuitBreaker[types.WorkerGenerateResponse](gobreaker.Settings{
				Name:        workerURL,
				MaxRequests: 1,
				Interval:    0,
				Timeout:     10 * time.Second,
				ReadyToTrip: func(counts gobreaker.Counts) bool {
					return counts.ConsecutiveFailures > 3
				},
				IsSuccessful: func(err error) bool {
					return err == nil || errors.Is(err, errWorkerSaturated)
				},
			}),
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	r.refreshWorkerStates(ctx)
	go r.refreshLoop()

	return r
}

func (r *Router) Route(ctx context.Context, req types.InferenceRequest) (types.InferenceResponse, error) {
	estimatedCost := estimateRequestCost(req)
	for {
		workers := r.rankWorkers(estimatedCost)
		if len(workers) == 0 {
			if err := waitForRetry(ctx, 40*time.Millisecond); err != nil {
				return types.InferenceResponse{}, errors.New("cluster remained saturated until request timeout")
			}
			continue
		}

		var lastErr error
		sawSaturation := false
		for _, worker := range workers {
			r.reserve(worker.baseURL, estimatedCost)
			resp, dispatchErr := r.dispatch(ctx, worker.baseURL, req)
			r.release(worker.baseURL, estimatedCost, dispatchErr)
			if dispatchErr == nil {
				return types.InferenceResponse(resp), nil
			}
			if errors.Is(dispatchErr, errWorkerSaturated) {
				sawSaturation = true
				continue
			}
			lastErr = dispatchErr
		}

		if sawSaturation {
			if err := waitForRetry(ctx, 40*time.Millisecond); err != nil {
				return types.InferenceResponse{}, errors.New("cluster remained saturated until request timeout")
			}
			continue
		}

		if lastErr == nil {
			lastErr = errors.New("failed to dispatch request")
		}
		return types.InferenceResponse{}, lastErr
	}
}

func (r *Router) Stats() types.RouterStats {
	r.mu.RLock()
	defer r.mu.RUnlock()

	snapshots := make([]types.WorkerSnapshot, 0, len(r.workers))
	healthy := 0
	available := 0
	saturated := 0
	now := time.Now()
	for _, workerURL := range r.workerURLs {
		worker := r.workers[workerURL]
		if worker == nil {
			continue
		}
		isHealthy := r.isHealthy(worker, now)
		hasCapacity := r.hasCapacity(worker)
		if isHealthy {
			healthy++
		}
		if isHealthy && hasCapacity {
			available++
		}
		if isHealthy && !hasCapacity {
			saturated++
		}
		snapshots = append(snapshots, types.WorkerSnapshot{
			WorkerID:          worker.capacity.WorkerID,
			BaseURL:           worker.baseURL,
			ActiveRequests:    worker.capacity.ActiveRequests,
			QueuedTokens:      worker.capacity.QueuedTokens,
			MaxConcurrent:     worker.capacity.MaxConcurrent,
			Healthy:           isHealthy,
			ConsecutiveErrors: worker.consecutiveErrors,
			LastUpdatedUnixMS: worker.lastUpdated.UnixMilli(),
		})
	}

	return types.RouterStats{
		Strategy:          r.strategy,
		RefreshIntervalMS: int(r.refreshInterval / time.Millisecond),
		StaleAfterMS:      int(r.staleAfter / time.Millisecond),
		CachedWorkers:     len(snapshots),
		HealthyWorkers:    healthy,
		AvailableWorkers:  available,
		SaturatedWorkers:  saturated,
		Workers:           snapshots,
	}
}

func (r *Router) rankWorkers(requestCost int) []workerState {
	if r.strategy == "round_robin" {
		return r.roundRobinWorkers()
	}
	return r.costAwareWorkers(requestCost)
}

func (r *Router) costAwareWorkers(requestCost int) []workerState {
	r.mu.RLock()
	defer r.mu.RUnlock()

	now := time.Now()
	workers := make([]workerState, 0, len(r.workers))
	for _, workerURL := range r.workerURLs {
		worker := r.workers[workerURL]
		if worker == nil || !r.isHealthy(worker, now) || !r.hasCapacity(worker) {
			continue
		}

		maxConcurrent := max(worker.capacity.MaxConcurrent, 1)
		projectedQueued := worker.capacity.QueuedTokens + requestCost
		score := float64(projectedQueued) / float64(maxConcurrent)

		workers = append(workers, workerState{
			baseURL: worker.baseURL,
			score:   score,
		})
	}

	sort.Slice(workers, func(i, j int) bool {
		return workers[i].score < workers[j].score
	})

	return workers
}

func (r *Router) roundRobinWorkers() []workerState {
	r.mu.RLock()
	defer r.mu.RUnlock()

	now := time.Now()
	healthy := make([]workerState, 0, len(r.workers))
	for _, workerURL := range r.workerURLs {
		worker := r.workers[workerURL]
		if worker == nil || !r.isHealthy(worker, now) || !r.hasCapacity(worker) {
			continue
		}
		healthy = append(healthy, workerState{
			baseURL: worker.baseURL,
		})
	}

	if len(healthy) <= 1 {
		return healthy
	}

	start := int(r.rrCounter.Add(1)-1) % len(healthy)
	ordered := make([]workerState, 0, len(healthy))
	for i := 0; i < len(healthy); i++ {
		ordered = append(ordered, healthy[(start+i)%len(healthy)])
	}

	return ordered
}

func (r *Router) refreshLoop() {
	ticker := time.NewTicker(r.refreshInterval)
	defer ticker.Stop()

	for range ticker.C {
		ctx, cancel := context.WithTimeout(context.Background(), r.refreshInterval)
		r.refreshWorkerStates(ctx)
		cancel()
	}
}

func (r *Router) refreshWorkerStates(ctx context.Context) {
	for _, workerURL := range r.workerURLs {
		capacity, err := r.fetchCapacity(ctx, workerURL)
		r.mu.Lock()
		worker := r.ensureWorker(workerURL)
		if err != nil {
			worker.consecutiveErrors++
			if worker.consecutiveErrors >= 2 {
				worker.capacity.Healthy = false
			}
			r.mu.Unlock()
			continue
		}

		worker.capacity = capacity
		worker.baseURL = workerURL
		worker.lastUpdated = time.Now()
		worker.consecutiveErrors = 0
		r.mu.Unlock()
	}
}

func (r *Router) reserve(workerURL string, requestCost int) {
	r.mu.Lock()
	defer r.mu.Unlock()

	worker := r.ensureWorker(workerURL)
	worker.capacity.ActiveRequests++
	worker.capacity.QueuedTokens += requestCost
}

func (r *Router) release(workerURL string, requestCost int, dispatchErr error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	worker := r.ensureWorker(workerURL)
	worker.capacity.ActiveRequests = max(worker.capacity.ActiveRequests-1, 0)
	worker.capacity.QueuedTokens = max(worker.capacity.QueuedTokens-requestCost, 0)

	if dispatchErr != nil {
		if errors.Is(dispatchErr, errWorkerSaturated) {
			worker.capacity.Healthy = true
			worker.lastUpdated = time.Now()
			return
		}
		worker.consecutiveErrors++
		if worker.consecutiveErrors >= 2 {
			worker.capacity.Healthy = false
		}
		return
	}

	worker.capacity.Healthy = true
	worker.consecutiveErrors = 0
	worker.lastUpdated = time.Now()
}

func (r *Router) ensureWorker(workerURL string) *cachedWorker {
	worker, ok := r.workers[workerURL]
	if !ok {
		worker = &cachedWorker{
			baseURL: workerURL,
			capacity: types.WorkerCapacity{
				WorkerID:      workerNameFromURL(workerURL),
				MaxConcurrent: 1,
			},
			cb: gobreaker.NewCircuitBreaker[types.WorkerGenerateResponse](gobreaker.Settings{
				Name:        workerURL,
				MaxRequests: 1,
				Interval:    0,
				Timeout:     10 * time.Second,
				ReadyToTrip: func(counts gobreaker.Counts) bool {
					return counts.ConsecutiveFailures > 3
				},
				IsSuccessful: func(err error) bool {
					return err == nil || errors.Is(err, errWorkerSaturated)
				},
			}),
		}
		r.workers[workerURL] = worker
	}
	return worker
}

func (r *Router) isHealthy(worker *cachedWorker, now time.Time) bool {
	if worker == nil || !worker.capacity.Healthy || worker.lastUpdated.IsZero() {
		return false
	}
	if worker.cb.State() == gobreaker.StateOpen {
		return false
	}
	return now.Sub(worker.lastUpdated) <= r.staleAfter
}

func (r *Router) fetchCapacity(ctx context.Context, workerURL string) (types.WorkerCapacity, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf("%s/capacity", workerURL), nil)
	if err != nil {
		return types.WorkerCapacity{}, err
	}

	resp, err := r.client.Do(req)
	if err != nil {
		return types.WorkerCapacity{}, err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= http.StatusBadRequest {
		return types.WorkerCapacity{}, fmt.Errorf("capacity endpoint returned status %d", resp.StatusCode)
	}

	var capacity types.WorkerCapacity
	if err := json.NewDecoder(resp.Body).Decode(&capacity); err != nil {
		return types.WorkerCapacity{}, err
	}

	return capacity, nil
}

func (r *Router) dispatch(ctx context.Context, workerURL string, payload types.InferenceRequest) (types.WorkerGenerateResponse, error) {
	r.mu.RLock()
	worker := r.workers[workerURL]
	r.mu.RUnlock()
	if worker == nil {
		return types.WorkerGenerateResponse{}, fmt.Errorf("worker not found")
	}

	return worker.cb.Execute(func() (types.WorkerGenerateResponse, error) {
		body, err := json.Marshal(payload)
		if err != nil {
			return types.WorkerGenerateResponse{}, err
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, fmt.Sprintf("%s/generate", workerURL), bytes.NewReader(body))
		if err != nil {
			return types.WorkerGenerateResponse{}, err
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := r.client.Do(req)
		if err != nil {
			return types.WorkerGenerateResponse{}, err
		}
		defer resp.Body.Close()

		if resp.StatusCode >= http.StatusBadRequest {
			if resp.StatusCode == http.StatusTooManyRequests {
				return types.WorkerGenerateResponse{}, errWorkerSaturated
			}
			return types.WorkerGenerateResponse{}, fmt.Errorf("worker returned status %d", resp.StatusCode)
		}

		var generateResp types.WorkerGenerateResponse
		if err := json.NewDecoder(resp.Body).Decode(&generateResp); err != nil {
			return types.WorkerGenerateResponse{}, err
		}

		return generateResp, nil
	})
}

func estimateRequestCost(req types.InferenceRequest) int {
	promptTokens := max(len(req.Prompt)/4, 1)
	return promptTokens + req.MaxTokens
}

func workerNameFromURL(workerURL string) string {
	trimmed := strings.TrimRight(workerURL, "/")
	if idx := strings.LastIndex(trimmed, "/"); idx >= 0 && idx < len(trimmed)-1 {
		return trimmed[idx+1:]
	}
	return trimmed
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func (r *Router) hasCapacity(worker *cachedWorker) bool {
	if worker == nil {
		return false
	}
	maxConcurrent := max(worker.capacity.MaxConcurrent, 1)
	return worker.capacity.ActiveRequests < maxConcurrent
}

func waitForRetry(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()

	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func minDuration(a, b time.Duration) time.Duration {
	if a < b {
		return a
	}
	return b
}

func normalizeStrategy(strategy string) string {
	switch strings.ToLower(strings.TrimSpace(strategy)) {
	case "round_robin", "round-robin", "rr":
		return "round_robin"
	default:
		return "cost"
	}
}
