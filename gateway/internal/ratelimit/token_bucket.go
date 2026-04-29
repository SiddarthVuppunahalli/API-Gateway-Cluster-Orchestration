package ratelimit

import (
	"sync"
	"time"
)

type TokenBucket struct {
	capacity   float64
	tokens     float64
	refillRate float64
	lastRefill time.Time
	mu         sync.Mutex
}

func NewTokenBucket(capacity, refillRate float64) *TokenBucket {
	return &TokenBucket{
		capacity:   capacity,
		tokens:     capacity,
		refillRate: refillRate,
		lastRefill: time.Now(),
	}
}

func (tb *TokenBucket) Allow(tokens float64) bool {
	tb.mu.Lock()
	defer tb.mu.Unlock()

	now := time.Now()
	elapsed := now.Sub(tb.lastRefill).Seconds()
	tb.tokens += elapsed * tb.refillRate
	if tb.tokens > tb.capacity {
		tb.tokens = tb.capacity
	}
	tb.lastRefill = now

	if tb.tokens >= tokens {
		tb.tokens -= tokens
		return true
	}
	return false
}

type Manager struct {
	buckets    map[string]*TokenBucket
	capacity   float64
	refillRate float64
	mu         sync.RWMutex
}

func NewManager(capacity, refillRate float64) *Manager {
	return &Manager{
		buckets:    make(map[string]*TokenBucket),
		capacity:   capacity,
		refillRate: refillRate,
	}
}

func (m *Manager) Allow(apiKey string, tokens float64) bool {
	if apiKey == "" {
		apiKey = "anonymous"
	}

	m.mu.RLock()
	bucket, exists := m.buckets[apiKey]
	m.mu.RUnlock()

	if !exists {
		m.mu.Lock()
		bucket, exists = m.buckets[apiKey]
		if !exists {
			bucket = NewTokenBucket(m.capacity, m.refillRate)
			m.buckets[apiKey] = bucket
		}
		m.mu.Unlock()
	}

	return bucket.Allow(tokens)
}
