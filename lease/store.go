package lease

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Record is the persisted state of a single lease.
type Record struct {
	Holder    string    `json:"holder"`
	Token     uint64    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
}

// state is the full persisted state of the lock server.
type state struct {
	// LastToken is the most recently issued fencing token. It is
	// persisted so tokens stay strictly increasing across restarts.
	LastToken uint64            `json:"last_token"`
	Leases    map[string]Record `json:"leases"`
}

// LogEntry is one line of the local operation log.
type LogEntry struct {
	Time     time.Time `json:"time"`
	Op       string    `json:"op"`
	Resource string    `json:"resource"`
	Holder   string    `json:"holder"`
	Token    uint64    `json:"token,omitempty"`
	Success  bool      `json:"success"`
	Detail   string    `json:"detail,omitempty"`
}

// Store persists lease state and the operation log locally.
type Store interface {
	Load() (state, error)
	Save(s state) error
	AppendLog(e LogEntry) error
}

// MemStore keeps everything in memory.
type MemStore struct {
	mu   sync.Mutex
	s    state
	logs []LogEntry
}

// NewMemStore returns an empty in-memory store.
func NewMemStore() *MemStore {
	return &MemStore{s: state{Leases: map[string]Record{}}}
}

// Load implements Store.
func (m *MemStore) Load() (state, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return cloneState(m.s), nil
}

// Save implements Store.
func (m *MemStore) Save(s state) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.s = cloneState(s)
	return nil
}

// AppendLog implements Store.
func (m *MemStore) AppendLog(e LogEntry) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.logs = append(m.logs, e)
	return nil
}

// Logs returns a copy of the in-memory operation log.
func (m *MemStore) Logs() []LogEntry {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]LogEntry, len(m.logs))
	copy(out, m.logs)
	return out
}

// FileStore persists state as JSON and appends to a JSONL log file
// inside a local directory. Nothing ever leaves the local disk.
type FileStore struct {
	mu        sync.Mutex
	dir       string
	statePath string
	logPath   string
}

// NewFileStore creates (if needed) a store rooted at dir.
func NewFileStore(dir string) (*FileStore, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	return &FileStore{
		dir:       dir,
		statePath: filepath.Join(dir, "state.json"),
		logPath:   filepath.Join(dir, "oplog.jsonl"),
	}, nil
}

// Load implements Store. A missing state file yields an empty state.
func (f *FileStore) Load() (state, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s := state{Leases: map[string]Record{}}
	data, err := os.ReadFile(f.statePath)
	if os.IsNotExist(err) {
		return s, nil
	}
	if err != nil {
		return s, err
	}
	if err := json.Unmarshal(data, &s); err != nil {
		return s, fmt.Errorf("corrupt state file %s: %w", f.statePath, err)
	}
	if s.Leases == nil {
		s.Leases = map[string]Record{}
	}
	return s, nil
}

// Save implements Store, writing atomically via rename.
func (f *FileStore) Save(s state) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	tmp := f.statePath + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, f.statePath)
}

// AppendLog implements Store.
func (f *FileStore) AppendLog(e LogEntry) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	data, err := json.Marshal(e)
	if err != nil {
		return err
	}
	fh, err := os.OpenFile(f.logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer fh.Close()
	_, err = fh.Write(append(data, '\n'))
	return err
}

func cloneState(s state) state {
	out := state{LastToken: s.LastToken, Leases: make(map[string]Record, len(s.Leases))}
	for k, v := range s.Leases {
		out.Leases[k] = v
	}
	return out
}
