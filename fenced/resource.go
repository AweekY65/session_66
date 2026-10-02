// Package fenced models a downstream resource that only accepts
// writes carrying a fresh fencing token, so a stale lock holder is
// detected even if it still believes it owns the lease.
package fenced

import (
	"errors"
	"fmt"
	"sync"
)

// ErrStaleToken is returned when a write carries a fencing token that
// is not greater than every token seen before for that resource.
var ErrStaleToken = errors.New("fenced: stale fencing token")

// KV is a tiny in-memory key/value store guarded by fencing tokens.
type KV struct {
	mu        sync.Mutex
	data      map[string]string
	maxToken  map[string]uint64
}

// NewKV returns an empty fenced store.
func NewKV() *KV {
	return &KV{data: map[string]string{}, maxToken: map[string]uint64{}}
}

// Write stores value under key only if token is strictly greater than
// any token previously accepted for resource.
func (kv *KV) Write(resource, key, value string, token uint64) error {
	kv.mu.Lock()
	defer kv.mu.Unlock()
	if token <= kv.maxToken[resource] {
		return fmt.Errorf("%w: token %d <= last accepted %d",
			ErrStaleToken, token, kv.maxToken[resource])
	}
	kv.maxToken[resource] = token
	kv.data[key] = value
	return nil
}

// Read returns the value stored under key.
func (kv *KV) Read(key string) (string, bool) {
	kv.mu.Lock()
	defer kv.mu.Unlock()
	v, ok := kv.data[key]
	return v, ok
}
