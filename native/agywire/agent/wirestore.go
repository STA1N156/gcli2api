package agent

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"

	"gcli2api/agy-http/agywire/schema"
)

const (
	wireConvTTL      = 7 * 24 * time.Hour
	wireMemIdleEvict = time.Hour
)

type wirePending struct {
	ToolUseID string
	Call      FunctionCall
	Internal  bool
	IssuedAt  time.Time
	Monitored bool
}

type wireConv struct {
	InputInTool         bool
	Key                 string
	Cascade, Trajectory string
	Cwd, AppendText     string
	MCPServer, ToolsFP  string
	Tools               []schema.MCPTool

	Steps, ModelCalls, Prompts int
	LastExecID, CurLastExec    string
	AnnouncedModel             string
	Titled                     bool

	Contents []Content
	Traj     []trajStep

	TurnFPs []string

	LastStop       string
	LastToolUseIDs []string
	Pending        []wirePending
	PendingStep    int

	Updated time.Time
}

func (c *wireConv) clone() *wireConv {
	out := *c
	out.Contents = append([]Content(nil), c.Contents...)
	out.Traj = append([]trajStep(nil), c.Traj...)
	out.Pending = append([]wirePending(nil), c.Pending...)
	out.TurnFPs = append([]string(nil), c.TurnFPs...)
	out.LastToolUseIDs = append([]string(nil), c.LastToolUseIDs...)
	return &out
}

type wireStore struct {
	dir string

	mu      sync.Mutex
	locks   map[string]chan struct{}
	cache   map[string]*wireConv
	seen    map[string]time.Time
	cancels map[string]context.CancelFunc

	early map[string]time.Time

	seq      map[string]uint64
	savedSeq map[string]uint64
	saveMus  map[string]*sync.Mutex
}

func newWireStore(home string) *wireStore {
	dir := filepath.Join(home, ".gemini", "antigravity-cli", "conversations", "wire")
	_ = os.MkdirAll(dir, 0o700)
	w := &wireStore{dir: dir, locks: map[string]chan struct{}{}, cache: map[string]*wireConv{},
		seen: map[string]time.Time{}, cancels: map[string]context.CancelFunc{}, early: map[string]time.Time{},
		seq: map[string]uint64{}, savedSeq: map[string]uint64{}, saveMus: map[string]*sync.Mutex{}}
	w.gc()
	return w
}

func (w *wireStore) path(key string) string {
	sum := sha256.Sum256([]byte(key))
	return filepath.Join(w.dir, hex.EncodeToString(sum[:8])+".json")
}

func (w *wireStore) acquire(ctx context.Context, key string) (func(), error) {
	w.mu.Lock()
	l, ok := w.locks[key]
	if !ok {
		l = make(chan struct{}, 1)
		w.locks[key] = l
	}
	w.mu.Unlock()
	select {
	case l <- struct{}{}:
		return func() { <-l }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (w *wireStore) begin(key string) uint64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.seq[key]++
	return w.seq[key]
}

func (w *wireStore) saveIfNewest(c *wireConv, n uint64) bool {
	w.mu.Lock()
	sm, ok := w.saveMus[c.Key]
	if !ok {
		sm = &sync.Mutex{}
		w.saveMus[c.Key] = sm
	}
	w.mu.Unlock()
	sm.Lock()
	defer sm.Unlock()
	w.mu.Lock()
	if n < w.savedSeq[c.Key] {
		w.mu.Unlock()
		return false
	}
	w.savedSeq[c.Key] = n
	w.mu.Unlock()
	w.save(c)
	return true
}

func (w *wireStore) load(key string) *wireConv {
	w.mu.Lock()
	if c, ok := w.cache[key]; ok {
		w.seen[key] = time.Now()
		w.mu.Unlock()
		return c
	}
	w.mu.Unlock()
	b, err := os.ReadFile(w.path(key))
	if err != nil {
		return nil
	}
	var c wireConv
	if json.Unmarshal(b, &c) != nil || c.Key != key {
		return nil
	}
	if time.Since(c.Updated) > wireConvTTL {
		return nil
	}
	w.mu.Lock()
	w.cache[key] = &c
	w.seen[key] = time.Now()
	w.mu.Unlock()
	return &c
}

func (w *wireStore) save(c *wireConv) {
	c.Updated = time.Now()
	b, err := json.Marshal(c)
	if err != nil {
		log.Printf("对话状态序列化失败: %v", err)
		return
	}
	p := w.path(c.Key)
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err == nil {
		if err := os.Rename(tmp, p); err != nil {
			log.Printf("对话状态落盘失败: %v", err)
		}
	} else {
		log.Printf("对话状态落盘失败: %v", err)
	}
	w.mu.Lock()
	w.cache[c.Key] = c
	w.seen[c.Key] = time.Now()
	for k, t := range w.seen {
		if time.Since(t) > wireMemIdleEvict {
			delete(w.cache, k)
			delete(w.seen, k)
		}
	}
	w.mu.Unlock()
}

const earlyCancelTTL = 10 * time.Minute

func (w *wireStore) setCancel(route string, c context.CancelFunc) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if c == nil {
		delete(w.cancels, route)
		return
	}
	if _, ok := w.early[route]; ok {
		delete(w.early, route)
		c()
	}
	w.cancels[route] = c
}

func (w *wireStore) cancel(route string) {
	w.mu.Lock()
	c := w.cancels[route]
	if c == nil {
		now := time.Now()
		for r, t := range w.early {
			if now.Sub(t) > earlyCancelTTL {
				delete(w.early, r)
			}
		}
		w.early[route] = now
	}
	w.mu.Unlock()
	if c != nil {
		c()
	}
}

func (w *wireStore) gc() {
	ents, err := os.ReadDir(w.dir)
	if err != nil {
		return
	}
	for _, e := range ents {
		if info, err := e.Info(); err == nil && time.Since(info.ModTime()) > wireConvTTL {
			_ = os.Remove(filepath.Join(w.dir, e.Name()))
		}
	}
}
