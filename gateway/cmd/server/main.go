package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/config"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/metrics"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/queue"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/ratelimit"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/router"
	"github.com/sidda/api-gateway-cluster-orchestration/gateway/internal/types"
)

func main() {
	cfg := config.Load()
	rt := router.New(
		cfg.WorkerURLs,
		cfg.RoutingStrategy,
		time.Duration(cfg.RefreshInterval)*time.Millisecond,
		time.Duration(cfg.WorkerStaleAfter)*time.Millisecond,
	)
	dispatchQueue := queue.New(rt, cfg.QueueCapacity, cfg.DispatchWorkers)
	rateLimiter := ratelimit.NewManager(10000.0, 1000.0) // 10k capacity, 1k tokens/sec refill

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("/stats", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, dispatchQueue.Stats())
	})
	mux.Handle("/metrics", promhttp.Handler())

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

		apiKey := r.Header.Get("X-API-Key")
		requestCost := float64(max(len(req.Prompt)/4, 1) + req.MaxTokens)
		if !rateLimiter.Allow(apiKey, requestCost) {
			metrics.GatewayRequestsTotal.WithLabelValues("429_ratelimit").Inc()
			writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": "rate limit exceeded for API key"})
			return
		}

		ctx, cancel := context.WithTimeout(r.Context(), time.Duration(cfg.RequestTimeout)*time.Second)
		defer cancel()

		started := time.Now()
		resp, err := dispatchQueue.Enqueue(ctx, req)
		duration := time.Since(started).Seconds()
		metrics.GatewayRequestDuration.Observe(duration)

		if err != nil {
			switch {
			case errors.Is(err, queue.ErrQueueFull):
				metrics.GatewayRequestsTotal.WithLabelValues("429").Inc()
				writeJSON(w, http.StatusTooManyRequests, map[string]string{"error": err.Error()})
			case errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled):
				metrics.GatewayRequestsTotal.WithLabelValues("504").Inc()
				writeJSON(w, http.StatusGatewayTimeout, map[string]string{"error": err.Error()})
			default:
				metrics.GatewayRequestsTotal.WithLabelValues("503").Inc()
				writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": err.Error()})
			}
			return
		}

		metrics.GatewayRequestsTotal.WithLabelValues("200").Inc()
		writeJSON(w, http.StatusOK, resp)
	})

	addr := ":" + cfg.Port
	log.Printf(
		"gateway listening on %s with %d workers, queue_capacity=%d, dispatch_workers=%d, routing_strategy=%s",
		addr,
		len(cfg.WorkerURLs),
		cfg.QueueCapacity,
		cfg.DispatchWorkers,
		cfg.RoutingStrategy,
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
