package lease

import "time"

// Client simulates one lock user. Many clients can be driven from
// goroutines or separate processes sharing the same Server/Store.
type Client struct {
	ID     string
	server *Server
}

// NewClient returns a client talking to server.
func NewClient(id string, server *Server) *Client {
	return &Client{ID: id, server: server}
}

// Acquire attempts to take the lease on resource.
func (c *Client) Acquire(resource string, ttl time.Duration) (Grant, error) {
	return c.server.Acquire(resource, c.ID, ttl)
}

// Renew extends a previously acquired lease.
func (c *Client) Renew(resource string, token uint64, ttl time.Duration) (time.Time, bool, error) {
	return c.server.Renew(resource, c.ID, token, ttl)
}

// Release gives up a previously acquired lease.
func (c *Client) Release(resource string, token uint64) (bool, error) {
	return c.server.Release(resource, c.ID, token)
}
