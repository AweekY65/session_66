package lease_test

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"leasesim/clock"
	"leasesim/fenced"
	"leasesim/lease"
)

var t0 = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

const ttl = 10 * time.Second

func newServer(t *testing.T, clk clock.Clock, store lease.Store) *lease.Server {
	t.Helper()
	s, err := lease.NewServer(clk, store)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	return s
}

// TestCompetingAcquire: many goroutines race for one resource; exactly
// one wins, and every winner's token is unique and increasing.
func TestCompetingAcquire(t *testing.T) {
	clk := clock.NewManual(t0)
	srv := newServer(t, clk, lease.NewMemStore())

	const n = 32
	grants := make([]lease.Grant, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			g, err := srv.Acquire("res", fmt.Sprintf("client-%d", i), ttl)
			if err != nil {
				t.Errorf("acquire: %v", err)
			}
			grants[i] = g
		}(i)
	}
	wg.Wait()

	winners := 0
	for _, g := range grants {
		if g.Acquired {
			winners++
		}
	}
	if winners != 1 {
		t.Fatalf("expected exactly 1 winner, got %d", winners)
	}
}

// TestRenew: the holder can extend its lease before expiry.
func TestRenew(t *testing.T) {
	clk := clock.NewManual(t0)
	srv := newServer(t, clk, lease.NewMemStore())
	c := lease.NewClient("a", srv)

	g, err := c.Acquire("res", ttl)
	if err != nil || !g.Acquired {
		t.Fatalf("acquire: %+v %v", g, err)
	}

	clk.Advance(6 * time.Second) // 4s left
	exp, ok, err := c.Renew("res", g.Token, ttl)
	if err != nil || !ok {
		t.Fatalf("renew failed: ok=%v err=%v", ok, err)
	}
	if want := t0.Add(16 * time.Second); !exp.Equal(want) {
		t.Fatalf("expiry = %v, want %v", exp, want)
	}

	// Still held at t0+15s thanks to the renewal.
	clk.Advance(9 * time.Second)
	other := lease.NewClient("b", srv)
	g2, _ := other.Acquire("res", ttl)
	if g2.Acquired {
		t.Fatal("renewed lease should still block other acquirers")
	}
}

// TestLeaseExpiry: after the TTL passes, an expired lease cannot be
// renewed and another client can take over.
func TestLeaseExpiry(t *testing.T) {
	clk := clock.NewManual(t0)
	srv := newServer(t, clk, lease.NewMemStore())
	a := lease.NewClient("a", srv)

	g, _ := a.Acquire("res", ttl)
	clk.Advance(ttl + time.Nanosecond) // lease is now expired

	if _, ok, _ := a.Renew("res", g.Token, ttl); ok {
		t.Fatal("renew of expired lease must fail")
	}
	if ok, _ := a.Release("res", g.Token); ok {
		t.Fatal("release of expired lease must be a no-op")
	}

	b := lease.NewClient("b", srv)
	g2, _ := b.Acquire("res", ttl)
	if !g2.Acquired {
		t.Fatal("new holder must be able to acquire after expiry")
	}
	if g2.Token <= g.Token {
		t.Fatalf("token not increasing: %d -> %d", g.Token, g2.Token)
	}
}

// TestStaleClientRecovery: client A pauses longer than its lease, B
// takes over, and A's late operations are rejected both by the lock
// server and by the fencing-token check on the guarded resource.
func TestStaleClientRecovery(t *testing.T) {
	clk := clock.NewManual(t0)
	srv := newServer(t, clk, lease.NewMemStore())
	kv := fenced.NewKV()
	a := lease.NewClient("a", srv)
	b := lease.NewClient("b", srv)

	ga, _ := a.Acquire("res", ttl)
	if err := kv.Write("res", "k", "from-a", ga.Token); err != nil {
		t.Fatalf("write by valid holder rejected: %v", err)
	}

	// A "pauses": it simply stops calling Renew while time advances.
	clk.Advance(ttl + time.Second)

	gb, _ := b.Acquire("res", ttl)
	if !gb.Acquired {
		t.Fatal("B must acquire after A's lease expired")
	}
	if gb.Token <= ga.Token {
		t.Fatalf("fencing token not increasing: %d -> %d", ga.Token, gb.Token)
	}

	// A wakes up and still believes it holds the lock.
	if _, ok, _ := a.Renew("res", ga.Token, ttl); ok {
		t.Fatal("stale A must not renew")
	}
	if ok, _ := a.Release("res", ga.Token); ok {
		t.Fatal("stale A must not release B's lease")
	}
	err := kv.Write("res", "k", "stale-write", ga.Token)
	if !errors.Is(err, fenced.ErrStaleToken) {
		t.Fatalf("stale write must be fenced, got %v", err)
	}

	// B's write with the newer token goes through.
	if err := kv.Write("res", "k", "from-b", gb.Token); err != nil {
		t.Fatalf("write by current holder rejected: %v", err)
	}
	if v, _ := kv.Read("k"); v != "from-b" {
		t.Fatalf("kv = %q, want from-b", v)
	}
}

// TestTokenMonotonic: tokens strictly increase across every successful
// acquire, including across release/reacquire cycles.
func TestTokenMonotonic(t *testing.T) {
	clk := clock.NewManual(t0)
	srv := newServer(t, clk, lease.NewMemStore())

	var last uint64
	for i := 0; i < 5; i++ {
		c := lease.NewClient(fmt.Sprintf("c%d", i), srv)
		g, _ := c.Acquire("res", ttl)
		if !g.Acquired {
			t.Fatalf("round %d: acquire failed", i)
		}
		if g.Token <= last {
			t.Fatalf("round %d: token %d not > %d", i, g.Token, last)
		}
		last = g.Token
		if ok, _ := c.Release("res", g.Token); !ok {
			t.Fatalf("round %d: release failed", i)
		}
	}
}

// TestReleaseAndReacquire: a released lease can be taken immediately.
func TestReleaseAndReacquire(t *testing.T) {
	clk := clock.NewManual(t0)
	srv := newServer(t, clk, lease.NewMemStore())
	a := lease.NewClient("a", srv)
	b := lease.NewClient("b", srv)

	ga, _ := a.Acquire("res", ttl)
	if g, _ := b.Acquire("res", ttl); g.Acquired {
		t.Fatal("B must not acquire while A holds the lease")
	}
	if ok, _ := a.Release("res", ga.Token); !ok {
		t.Fatal("release failed")
	}
	gb, _ := b.Acquire("res", ttl)
	if !gb.Acquired {
		t.Fatal("B must acquire after release")
	}
	if gb.Token <= ga.Token {
		t.Fatal("token must increase after release/reacquire")
	}
}

// TestRestartPersistsValidLease: a lease that is still valid at
// shutdown survives a restart and keeps blocking other clients.
func TestRestartPersistsValidLease(t *testing.T) {
	dir := t.TempDir()
	clk := clock.NewManual(t0)

	store, err := lease.NewFileStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	srv := newServer(t, clk, store)
	a := lease.NewClient("a", srv)
	ga, _ := a.Acquire("res", ttl)

	// "Restart": build a brand-new server over the same files.
	clk.Advance(3 * time.Second)
	store2, _ := lease.NewFileStore(dir)
	srv2 := newServer(t, clk, store2)

	if g, _ := lease.NewClient("b", srv2).Acquire("res", ttl); g.Acquired {
		t.Fatal("valid lease must survive restart and block others")
	}
	if _, ok, _ := lease.NewClient("a", srv2).Renew("res", ga.Token, ttl); !ok {
		t.Fatal("original holder must still renew after restart")
	}
}

// TestRestartDropsExpiredLease: an expired lease is never resurrected,
// and the token counter keeps increasing across the restart.
func TestRestartDropsExpiredLease(t *testing.T) {
	dir := t.TempDir()
	clk := clock.NewManual(t0)

	store, _ := lease.NewFileStore(dir)
	srv := newServer(t, clk, store)
	ga, _ := lease.NewClient("a", srv).Acquire("res", ttl)

	// Time passes beyond the TTL, then the process "restarts".
	clk.Advance(ttl + time.Hour)
	store2, _ := lease.NewFileStore(dir)
	srv2 := newServer(t, clk, store2)

	if _, valid := srv2.Snapshot("res"); valid {
		t.Fatal("expired lease must not be restored after restart")
	}
	gb, _ := lease.NewClient("b", srv2).Acquire("res", ttl)
	if !gb.Acquired {
		t.Fatal("new holder must acquire after expired lease was dropped")
	}
	if gb.Token <= ga.Token {
		t.Fatalf("token counter regressed across restart: %d -> %d", ga.Token, gb.Token)
	}
}

// TestFencingTokenRequired: the guarded resource rejects writes with
// reused or out-of-order tokens.
func TestFencingTokenRequired(t *testing.T) {
	kv := fenced.NewKV()
	if err := kv.Write("res", "k", "v1", 5); err != nil {
		t.Fatal(err)
	}
	if err := kv.Write("res", "k", "v2", 5); !errors.Is(err, fenced.ErrStaleToken) {
		t.Fatalf("equal token must be rejected: %v", err)
	}
	if err := kv.Write("res", "k", "v3", 3); !errors.Is(err, fenced.ErrStaleToken) {
		t.Fatalf("older token must be rejected: %v", err)
	}
	if err := kv.Write("res", "k", "v4", 6); err != nil {
		t.Fatal(err)
	}
	if v, _ := kv.Read("k"); v != "v4" {
		t.Fatalf("kv = %q, want v4", v)
	}
}
