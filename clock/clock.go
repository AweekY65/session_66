// Package clock provides an injectable time source so that lease
// expiry logic can be tested without relying on real sleeps.
package clock

import (
	"sync"
	"time"
)

// Clock is the time source used by the lease server.
type Clock interface {
	Now() time.Time
}

// Real is a Clock backed by the wall clock.
type Real struct{}

// Now returns the current wall-clock time.
func (Real) Now() time.Time { return time.Now() }

// Manual is a Clock that only moves when Advance is called.
// It is safe for concurrent use.
type Manual struct {
	mu  sync.Mutex
	now time.Time
}

// NewManual returns a Manual clock pinned at start.
func NewManual(start time.Time) *Manual {
	return &Manual{now: start}
}

// Now returns the current manual time.
func (m *Manual) Now() time.Time {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.now
}

// Advance moves the manual clock forward by d.
func (m *Manual) Advance(d time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.now = m.now.Add(d)
}
