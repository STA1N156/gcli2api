package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"gcli2api/agy-http/agywire/agent"
)

type wireAccount struct {
	once   sync.Once
	engine *agent.Embedded
	err    error
	token  atomic.Pointer[string]
	active atomic.Int64
	last   atomic.Int64
}

type endpointTransport struct {
	base     http.RoundTripper
	endpoint *url.URL
}

func (t endpointTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if t.endpoint.Host != "daily-cloudcode-pa.googleapis.com" {
		r = r.Clone(r.Context())
		u := *r.URL
		u.Scheme = t.endpoint.Scheme
		u.Host = t.endpoint.Host
		r.URL = &u
		r.Host = ""
	}
	return t.base.RoundTrip(r)
}

func (b *bridge) account(r *http.Request, target *url.URL, tr *http.Transport) (*wireAccount, error) {
	key := r.Header.Get("X-Gcli-Account") + "|" + r.Header.Get("X-Gcli-Proxy") + "|" + target.Scheme + "://" + target.Host
	var a *wireAccount
	for {
		value, ok := b.wires.Load(key)
		if !ok {
			value, _ = b.wires.LoadOrStore(key, &wireAccount{})
		}
		a = value.(*wireAccount)
		pinned := false
		for n := a.active.Load(); n >= 0; n = a.active.Load() {
			if a.active.CompareAndSwap(n, n+1) {
				pinned = true
				break
			}
		}
		if pinned {
			break
		}
		b.wires.CompareAndDelete(key, a)
	}
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	a.token.Store(&token)
	a.last.Store(time.Now().UnixNano())
	a.once.Do(func() {
		root := os.Getenv("AGY_WIRE_STATE_DIR")
		if root == "" {
			root = filepath.Join(os.TempDir(), "gcli2api-wire")
		}
		// One server owns the account cache and its snapshots across all requests.
		sum := sha256.Sum256([]byte(key))
		home := filepath.Join(root, hex.EncodeToString(sum[:]), "home")
		ctx := b.ctx
		if ctx == nil {
			ctx = context.Background()
		}
		a.engine, a.err = agent.NewEmbedded(ctx, home, &http.Client{Transport: endpointTransport{tr, target}}, func() (string, error) {
			value := a.token.Load()
			if value == nil || *value == "" {
				return "", agent.ErrAuth
			}
			return *value, nil
		})
	})
	if a.err != nil {
		a.active.Add(-1)
		b.wires.CompareAndDelete(key, a)
		return nil, a.err
	}
	return a, nil
}

func (b *bridge) sweepWires() {
	tick := time.NewTicker(time.Minute)
	defer tick.Stop()
	for {
		select {
		case <-b.ctx.Done():
			b.wires.Range(func(_, v any) bool {
				a := v.(*wireAccount)
				a.once.Do(func() {})
				if a.engine != nil {
					a.engine.Close()
				}
				return true
			})
			return
		case <-tick.C:
			b.wires.Range(func(k, v any) bool {
				a := v.(*wireAccount)
				if time.Since(time.Unix(0, a.last.Load())) > time.Hour && a.active.CompareAndSwap(0, -1) {
					b.wires.CompareAndDelete(k, a)
					if a.engine != nil {
						a.engine.Close()
					}
				}
				return true
			})
		}
	}
}
