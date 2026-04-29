package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	GatewayQueueDepth = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "gateway_queue_depth",
		Help: "Current depth of the gateway dispatch queue",
	})

	GatewayRequestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gateway_requests_total",
		Help: "Total number of inference requests processed",
	}, []string{"status"})

	GatewayRequestDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "gateway_request_duration_seconds",
		Help:    "Histogram of inference request processing durations",
		Buckets: []float64{0.1, 0.5, 1, 2, 5, 10, 20},
	})
)
