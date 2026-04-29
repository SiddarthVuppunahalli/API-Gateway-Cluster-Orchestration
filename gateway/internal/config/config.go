package config

import (
	"os"
	"strconv"
	"strings"
)

type Config struct {
	Port             string
	WorkerURLs       []string
	QueueCapacity    int
	DispatchWorkers  int
	RequestTimeout   int
	RefreshInterval  int
	WorkerStaleAfter int
	RoutingStrategy  string
}

func Load() Config {
	port := os.Getenv("GATEWAY_PORT")
	if port == "" {
		port = "8080"
	}

	rawWorkers := os.Getenv("WORKER_URLS")
	if rawWorkers == "" {
		rawWorkers = "http://localhost:9001,http://localhost:9002,http://localhost:9003"
	}

	workers := make([]string, 0)
	for _, worker := range strings.Split(rawWorkers, ",") {
		trimmed := strings.TrimSpace(worker)
		if trimmed != "" {
			workers = append(workers, trimmed)
		}
	}

	queueCapacity := readIntEnv("QUEUE_CAPACITY", 256)
	dispatchWorkers := readIntEnv("DISPATCH_WORKERS", 32)
	requestTimeout := readIntEnv("REQUEST_TIMEOUT_SECONDS", 8)
	refreshInterval := readIntEnv("WORKER_REFRESH_MS", 1500)
	workerStaleAfter := readIntEnv("WORKER_STALE_AFTER_MS", 5000)
	routingStrategy := readStringEnv("ROUTING_STRATEGY", "cost")

	return Config{
		Port:             port,
		WorkerURLs:       workers,
		QueueCapacity:    queueCapacity,
		DispatchWorkers:  dispatchWorkers,
		RequestTimeout:   requestTimeout,
		RefreshInterval:  refreshInterval,
		WorkerStaleAfter: workerStaleAfter,
		RoutingStrategy:  routingStrategy,
	}
}

func readIntEnv(key string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}

	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return fallback
	}

	return value
}

func readStringEnv(key, fallback string) string {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	return strings.ToLower(raw)
}
