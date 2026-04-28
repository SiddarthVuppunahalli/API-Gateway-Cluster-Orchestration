package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/config"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/queue"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/router"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/types"
)

func main() {
	cfg := config.Load()
	rt := router.New(
		cfg.WorkerURLs,
		time.Duration(cfg.RefreshInterval)*time.Millisecond,
		time.Duration(cfg.WorkerStaleAfter)*time.Millisecond,
	)
	dispatchQueue := queue.New(rt, cfg.QueueCapacity, cfg.DispatchWorkers)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("/stats", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, dispatchQueue.Stats())
	})

	mux.HandleFunc("/infer", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
			return
		}

		var req types.InferenceRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON payload"})
			return
		}

		if req.Prompt == "" || req.MaxTokens <= 0 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "prompt and positive max_tokens are required"})
			return
		}

		ctx, cancel := context.WithTimeout(r.Context(), time.Duration(cfg.RequestTimeout)*time.Second)
		defer cancel()

		resp, err := dispatchQueue.Enqueue(ctx, req)
		if err != nil {
			switch {
			case errors.Is(err, queue.ErrQueueFull):
				writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": err.Error()})
			case errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled):
				writeJSON(w, http.StatusGatewayTimeout, map[string]string{"error": err.Error()})
			default:
				writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": err.Error()})
			}
			return
		}

		writeJSON(w, http.StatusOK, resp)
	})

	addr := ":" + cfg.Port
	log.Printf(
		"gateway listening on %s with %d workers, queue_capacity=%d, dispatch_workers=%d",
		addr,
		len(cfg.WorkerURLs),
		cfg.QueueCapacity,
		cfg.DispatchWorkers,
	)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatal(err)
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		log.Printf("failed to write response: %v", err)
	}
}
