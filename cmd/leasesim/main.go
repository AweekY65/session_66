// Command leasesim runs a fully local lease-lock simulation with real
// time: two clients contend for a resource, one pauses past its lease,
// and a fenced KV store rejects the stale writer. State and logs are
// written to ./leasesim-data only.
package main

import (
	"fmt"
	"log"
	"time"

	"leasesim/clock"
	"leasesim/fenced"
	"leasesim/lease"
)

const ttl = 2 * time.Second

func main() {
	dir := "leasesim-data"
	store, err := lease.NewFileStore(dir)
	if err != nil {
		log.Fatal(err)
	}
	srv, err := lease.NewServer(clock.Real{}, store)
	if err != nil {
		log.Fatal(err)
	}
	kv := fenced.NewKV()

	alice := lease.NewClient("alice", srv)
	bob := lease.NewClient("bob", srv)

	ga, _ := alice.Acquire("inventory", ttl)
	fmt.Printf("alice acquired: token=%d expires=%s\n", ga.Token, ga.ExpiresAt.Format(time.RFC3339))
	must(kv.Write("inventory", "count", "alice-was-here", ga.Token))

	// Alice pauses (GC stall / network partition): no more renews.
	fmt.Println("alice pauses...")
	time.Sleep(ttl + 500*time.Millisecond)

	gb, _ := bob.Acquire("inventory", ttl)
	fmt.Printf("bob acquired after expiry: token=%d\n", gb.Token)
	must(kv.Write("inventory", "count", "bob-was-here", gb.Token))

	// Alice wakes up and acts as if she still held the lock.
	if _, ok, _ := alice.Renew("inventory", ga.Token, ttl); !ok {
		fmt.Println("alice renew rejected: lease expired and re-granted")
	}
	if err := kv.Write("inventory", "count", "stale-alice", ga.Token); err != nil {
		fmt.Printf("alice write fenced: %v\n", err)
	}
	v, _ := kv.Read("count")
	fmt.Printf("final value: %q (stale write rejected)\n", v)

	// Simulate a restart over the same state directory.
	srv2, err := lease.NewServer(clock.Real{}, store)
	if err != nil {
		log.Fatal(err)
	}
	rec, valid := srv2.Snapshot("inventory")
	fmt.Printf("after restart: lease valid=%v holder=%s token=%d lastToken=%d\n",
		valid, rec.Holder, rec.Token, srv2.LastToken())
	fmt.Printf("state and op log written under %s/\n", dir)
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}
