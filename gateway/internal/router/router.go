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
	"time"

	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/types"
)

type workerState struct {
	baseURL string
	score   float64
}

type cachedWorker struct {
	baseURL           string
	capacity          types.WorkerCapacity
	lastUpdated       time.Time
	consecutiveErrors int
}

type Router struct {
	client          *http.Client
	workerURLs      []string
	refreshInterval time.Duration
	staleAfter      time.Duration
	mu              sync.RWMutex
	workers         map[string]*cachedWorker
}

func New(workerURLs []string, refreshInterval, staleAfter time.Duration) *Router {
	r := &Router{
		client: &http.Client{
			Timeout: 5 * time.Second,
		},
		workerURLs:      workerURLs,
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
	workers := r.rankWorkers(estimatedCost)
	if len(workers) == 0 {
		refreshCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		r.refreshWorkerStates(refreshCtx)
		workers = r.rankWorkers(estimatedCost)
		if len(workers) == 0 {
			return types.InferenceResponse{}, errors.New("no healthy workers available")
		}
	}

	var lastErr error
	for _, worker := range workers {
		r.reserve(worker.baseURL, estimatedCost)
		resp, dispatchErr := r.dispatch(ctx, worker.baseURL, req)
		r.release(worker.baseURL, estimatedCost, dispatchErr)
		if dispatchErr == nil {
			return types.InferenceResponse(resp), nil
		}
		lastErr = dispatchErr
	}

	if lastErr == nil {
		lastErr = errors.New("failed to dispatch request")
	}

	return types.InferenceResponse{}, lastErr
}

func (r *Router) Stats() types.RouterStats {
	r.mu.RLock()
	defer r.mu.RUnlock()

	snapshots := make([]types.WorkerSnapshot, 0, len(r.workers))
	healthy := 0
	now := time.Now()
	for _, workerURL := range r.workerURLs {
		worker := r.workers[workerURL]
		if worker == nil {
			continue
		}
		isHealthy := r.isHealthy(worker, now)
		if isHealthy {
			healthy++
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
		RefreshIntervalMS: int(r.refreshInterval / time.Millisecond),
		StaleAfterMS:      int(r.staleAfter / time.Millisecond),
		CachedWorkers:     len(snapshots),
		HealthyWorkers:    healthy,
		Workers:           snapshots,
	}
}

func (r *Router) rankWorkers(requestCost int) []workerState {
	r.mu.RLock()
	defer r.mu.RUnlock()

	now := time.Now()
	workers := make([]workerState, 0, len(r.workers))
	for _, workerURL := range r.workerURLs {
		worker := r.workers[workerURL]
		if worker == nil || !r.isHealthy(worker, now) {
			continue
		}

		maxConcurrent := max(worker.capacity.MaxConcurrent, 1)
		projectedActive := worker.capacity.ActiveRequests + 1
		projectedQueued := worker.capacity.QueuedTokens + requestCost
		utilization := float64(projectedActive) / float64(maxConcurrent)
		score := utilization*100000 + float64(projectedQueued)

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
		}
		r.workers[workerURL] = worker
	}
	return worker
}

func (r *Router) isHealthy(worker *cachedWorker, now time.Time) bool {
	if worker == nil || !worker.capacity.Healthy || worker.lastUpdated.IsZero() {
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
		return types.WorkerGenerateResponse{}, fmt.Errorf("worker returned status %d", resp.StatusCode)
	}

	var generateResp types.WorkerGenerateResponse
	if err := json.NewDecoder(resp.Body).Decode(&generateResp); err != nil {
		return types.WorkerGenerateResponse{}, err
	}

	return generateResp, nil
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
