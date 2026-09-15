package ratelimit

import "testing"

func TestTokenBucketEnforcesCapacity(t *testing.T) {
	bucket := NewTokenBucket(10, 0)

	if !bucket.Allow(7) {
		t.Fatal("first request should be allowed")
	}
	if bucket.Allow(4) {
		t.Fatal("request exceeding the remaining capacity should be rejected")
	}
	if !bucket.Allow(3) {
		t.Fatal("request matching the remaining capacity should be allowed")
	}
}

func TestManagerKeepsIndependentBuckets(t *testing.T) {
	manager := NewManager(5, 0)

	if !manager.Allow("key-a", 5) {
		t.Fatal("key-a should receive its initial capacity")
	}
	if manager.Allow("key-a", 1) {
		t.Fatal("key-a should be exhausted")
	}
	if !manager.Allow("key-b", 5) {
		t.Fatal("key-b should have an independent bucket")
	}
}
