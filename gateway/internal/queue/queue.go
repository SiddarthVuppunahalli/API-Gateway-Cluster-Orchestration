package queue

import (
	"context"
	"errors"
	"sync/atomic"

	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/metrics"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/router"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/types"
)

var ErrQueueFull = errors.New("gateway queue is full")

type jobResult struct {
	response types.InferenceResponse
	err      error
}

type job struct {
	ctx    context.Context
	req    types.InferenceRequest
	result chan jobResult
}

type GatewayQueue struct {
	router          *router.Router
	jobs            chan job
	dispatchWorkers int
	accepted        atomic.Int64
	completed       atomic.Int64
	rejected        atomic.Int64
	failed          atomic.Int64
	inFlight        atomic.Int64
}

func New(rt *router.Router, queueCapacity, dispatchWorkers int) *GatewayQueue {
	q := &GatewayQueue{
		router:          rt,
		jobs:            make(chan job, queueCapacity),
		dispatchWorkers: dispatchWorkers,
	}

	for i := 0; i < dispatchWorkers; i++ {
		go q.runWorker()
	}

	return q
}

func (q *GatewayQueue) Enqueue(ctx context.Context, req types.InferenceRequest) (types.InferenceResponse, error) {
	resultCh := make(chan jobResult, 1)
	work := job{
		ctx:    ctx,
		req:    req,
		result: resultCh,
	}

	select {
	case q.jobs <- work:
		q.accepted.Add(1)
		metrics.GatewayQueueDepth.Set(float64(len(q.jobs)))
	case <-ctx.Done():
		return types.InferenceResponse{}, ctx.Err()
	default:
		q.rejected.Add(1)
		return types.InferenceResponse{}, ErrQueueFull
	}

	select {
	case result := <-resultCh:
		return result.response, result.err
	case <-ctx.Done():
		return types.InferenceResponse{}, ctx.Err()
	}
}

func (q *GatewayQueue) Stats() types.GatewayStats {
	return types.GatewayStats{
		QueueDepth:        len(q.jobs),
		QueueCapacity:     cap(q.jobs),
		DispatchWorkers:   q.dispatchWorkers,
		InFlight:          int(q.inFlight.Load()),
		AcceptedRequests:  int(q.accepted.Load()),
		CompletedRequests: int(q.completed.Load()),
		RejectedRequests:  int(q.rejected.Load()),
		FailedRequests:    int(q.failed.Load()),
		Router:            q.router.Stats(),
	}
}

func (q *GatewayQueue) runWorker() {
	for work := range q.jobs {
		q.inFlight.Add(1)
		response, err := q.router.Route(work.ctx, work.req)
		if err != nil {
			q.failed.Add(1)
		} else {
			q.completed.Add(1)
		}
		q.inFlight.Add(-1)
		metrics.GatewayQueueDepth.Set(float64(len(q.jobs)))

		select {
		case work.result <- jobResult{response: response, err: err}:
		case <-work.ctx.Done():
		}
	}
}
