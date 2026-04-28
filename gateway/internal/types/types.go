package types

type InferenceRequest struct {
	Prompt    string `json:"prompt"`
	MaxTokens int    `json:"max_tokens"`
}

type InferenceResponse struct {
	WorkerID  string `json:"worker_id"`
	Output    string `json:"output"`
	LatencyMS int64  `json:"latency_ms"`
}

type GatewayStats struct {
	QueueDepth        int `json:"queue_depth"`
	QueueCapacity     int `json:"queue_capacity"`
	DispatchWorkers   int `json:"dispatch_workers"`
	InFlight          int `json:"in_flight"`
	AcceptedRequests  int `json:"accepted_requests"`
	CompletedRequests int `json:"completed_requests"`
	RejectedRequests  int `json:"rejected_requests"`
	FailedRequests    int `json:"failed_requests"`
	Router            RouterStats `json:"router"`
}

type WorkerCapacity struct {
	WorkerID       string `json:"worker_id"`
	ActiveRequests int    `json:"active_requests"`
	QueuedTokens   int    `json:"queued_tokens"`
	MaxConcurrent  int    `json:"max_concurrent"`
	Healthy        bool   `json:"healthy"`
}

type WorkerGenerateResponse struct {
	WorkerID  string `json:"worker_id"`
	Output    string `json:"output"`
	LatencyMS int64  `json:"latency_ms"`
}

type WorkerSnapshot struct {
	WorkerID          string `json:"worker_id"`
	BaseURL           string `json:"base_url"`
	ActiveRequests    int    `json:"active_requests"`
	QueuedTokens      int    `json:"queued_tokens"`
	MaxConcurrent     int    `json:"max_concurrent"`
	Healthy           bool   `json:"healthy"`
	ConsecutiveErrors int    `json:"consecutive_errors"`
	LastUpdatedUnixMS int64  `json:"last_updated_unix_ms"`
}

type RouterStats struct {
	RefreshIntervalMS int              `json:"refresh_interval_ms"`
	StaleAfterMS      int              `json:"stale_after_ms"`
	CachedWorkers     int              `json:"cached_workers"`
	HealthyWorkers    int              `json:"healthy_workers"`
	Workers           []WorkerSnapshot `json:"workers"`
}
