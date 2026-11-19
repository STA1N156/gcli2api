package agent

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"time"
)

var ErrAuth = errors.New("Authentication required")

type TokenSource struct {
	source func() (string, error)
	path   string
	mu     sync.Mutex
	tok    string
	exp    time.Time
	mod    time.Time
}

func NewTokenSource(home string) *TokenSource {
	return &TokenSource{path: filepath.Join(home, ".gemini", "antigravity-cli", "antigravity-oauth-token")}
}

type storedToken struct {
	Token struct {
		AccessToken string `json:"access_token"`
		Expiry      string `json:"expiry"`
	} `json:"token"`
}

func (t *TokenSource) AccessToken() (string, error) {
	if t.source != nil {
		return t.source()
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	st, err := os.Stat(t.path)
	if err != nil {
		return "", ErrAuth
	}
	if !st.ModTime().Equal(t.mod) || t.tok == "" {
		raw, err := os.ReadFile(t.path)
		if err != nil {
			return "", ErrAuth
		}
		var s storedToken
		if err := json.Unmarshal(raw, &s); err != nil || s.Token.AccessToken == "" {
			return "", ErrAuth
		}
		t.tok = s.Token.AccessToken
		t.exp, _ = time.Parse(time.RFC3339Nano, s.Token.Expiry)
		t.mod = st.ModTime()
	}
	if !t.exp.IsZero() && time.Now().After(t.exp) {
		return "", ErrAuth
	}
	return t.tok, nil
}

func (t *TokenSource) invalidate() {
	if t == nil {
		return
	}
	t.mu.Lock()
	t.tok, t.mod = "", time.Time{}
	t.mu.Unlock()
}
