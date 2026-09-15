package queue

import (
	"context"
	"errors"
	"testing"

	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/types"
)

func TestEnqueueRejectsWhenQueueIsFull(t *testing.T) {
	q := &GatewayQueue{jobs: make(chan job, 1)}
	q.jobs <- job{}

	_, err := q.Enqueue(context.Background(), types.InferenceRequest{
		Prompt:    "test",
		MaxTokens: 16,
	})

	if !errors.Is(err, ErrQueueFull) {
		t.Fatalf("Enqueue() error = %v, want %v", err, ErrQueueFull)
	}
	if got := q.rejected.Load(); got != 1 {
		t.Fatalf("rejected requests = %d, want 1", got)
	}
	if got := q.accepted.Load(); got != 0 {
		t.Fatalf("accepted requests = %d, want 0", got)
	}
}
