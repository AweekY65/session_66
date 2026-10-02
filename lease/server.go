// Package lease implements a fully local lease-based distributed-lock
// simulator. Lock state, fencing tokens and logs live only in memory
// or in local files; no external service is involved.
package lease

import (
	"errors"
	"fmt"
	"sync"
	"time"

	"leasesim/clock"
)

// ErrInvalidTTL is returned when a non-positive TTL is requested.
var ErrInvalidTTL = errors.New("lease: ttl must be positive")

// Grant is the result of an Acquire call.
type Grant struct {
	Acquired  bool
	Token     uint64
	ExpiresAt time.Time
	// HeldBy names the current holder when Acquired is false.
	HeldBy string
}

// Server is the lock server. All expiry checks are lazy: the injected
// clock is consulted at operation time, so no background timers exist
// and tests never need to sleep.
type Server struct {
	mu    sync.Mutex
	clock clock.Clock
	store Store
	st    state
}

// NewServer loads persisted state from the store. Leases that are
// already expired at load time are discarded, so a restart never
// resurrects an expired lease.
func NewServer(clk clock.Clock, store Store) (*Server, error) {
	st, err := store.Load()
	if err != nil {
		return nil, err
	}
	s := &Server{clock: clk, store: store, st: st}
	now := clk.Now()
	for name, rec := range s.st.Leases {
		if !now.Before(rec.ExpiresAt) {
			delete(s.st.Leases, name)
			s.log(LogEntry{Time: now, Op: "expire_on_load", Resource: name,
				Holder: rec.Holder, Token: rec.Token, Success: true,
				Detail: "lease expired before restart, discarded"})
		}
	}
	if err := s.store.Save(s.st); err != nil {
		return nil, err
	}
	return s, nil
}

// Acquire tries to take the lease on resource for holder with the
// given TTL. On success it issues a strictly increasing fencing token.
func (s *Server) Acquire(resource, holder string, ttl time.Duration) (Grant, error) {
	if ttl <= 0 {
		return Grant{}, ErrInvalidTTL
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.clock.Now()
	if rec, ok := s.st.Leases[resource]; ok && now.Before(rec.ExpiresAt) {
		g := Grant{Acquired: false, HeldBy: rec.Holder}
		s.log(LogEntry{Time: now, Op: "acquire", Resource: resource, Holder: holder,
			Success: false, Detail: "held by " + rec.Holder})
		return g, nil
	}
	s.st.LastToken++
	rec := Record{Holder: holder, Token: s.st.LastToken, ExpiresAt: now.Add(ttl)}
	s.st.Leases[resource] = rec
	if err := s.store.Save(s.st); err != nil {
		return Grant{}, err
	}
	s.log(LogEntry{Time: now, Op: "acquire", Resource: resource, Holder: holder,
		Token: rec.Token, Success: true})
	return Grant{Acquired: true, Token: rec.Token, ExpiresAt: rec.ExpiresAt}, nil
}

// Renew extends the lease held by holder. It fails if the lease does
// not exist, belongs to someone else, carries a different token, or
// has already expired.
func (s *Server) Renew(resource, holder string, token uint64, ttl time.Duration) (time.Time, bool, error) {
	if ttl <= 0 {
		return time.Time{}, false, ErrInvalidTTL
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.clock.Now()
	rec, ok := s.st.Leases[resource]
	if !ok || rec.Holder != holder || rec.Token != token || !now.Before(rec.ExpiresAt) {
		detail := rejectReason(rec, ok, holder, token, now)
		s.log(LogEntry{Time: now, Op: "renew", Resource: resource, Holder: holder,
			Token: token, Success: false, Detail: detail})
		return time.Time{}, false, nil
	}
	rec.ExpiresAt = now.Add(ttl)
	s.st.Leases[resource] = rec
	if err := s.store.Save(s.st); err != nil {
		return time.Time{}, false, err
	}
	s.log(LogEntry{Time: now, Op: "renew", Resource: resource, Holder: holder,
		Token: token, Success: true})
	return rec.ExpiresAt, true, nil
}

// Release frees the lease if holder and token match and the lease is
// still valid. Releasing an already-expired lease is a no-op.
func (s *Server) Release(resource, holder string, token uint64) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.clock.Now()
	rec, ok := s.st.Leases[resource]
	if !ok || rec.Holder != holder || rec.Token != token || !now.Before(rec.ExpiresAt) {
		s.log(LogEntry{Time: now, Op: "release", Resource: resource, Holder: holder,
			Token: token, Success: false, Detail: rejectReason(rec, ok, holder, token, now)})
		return false, nil
	}
	delete(s.st.Leases, resource)
	if err := s.store.Save(s.st); err != nil {
		return false, err
	}
	s.log(LogEntry{Time: now, Op: "release", Resource: resource, Holder: holder,
		Token: token, Success: true})
	return true, nil
}

// Snapshot returns the lease record for resource, if any, together
// with whether it is currently valid.
func (s *Server) Snapshot(resource string) (Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec, ok := s.st.Leases[resource]
	if !ok || !s.clock.Now().Before(rec.ExpiresAt) {
		return Record{}, false
	}
	return rec, true
}

// LastToken returns the highest fencing token issued so far.
func (s *Server) LastToken() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.st.LastToken
}

func rejectReason(rec Record, ok bool, holder string, token uint64, now time.Time) string {
	switch {
	case !ok:
		return "no lease"
	case rec.Holder != holder:
		return fmt.Sprintf("held by %s", rec.Holder)
	case rec.Token != token:
		return fmt.Sprintf("stale token %d, current %d", token, rec.Token)
	case !now.Before(rec.ExpiresAt):
		return "lease expired"
	default:
		return ""
	}
}

func (s *Server) log(e LogEntry) {
	// Logging is best-effort: it must never mask the lock decision
	// that has already been made and persisted.
	_ = s.store.AppendLog(e)
}
