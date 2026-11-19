package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/gorilla/websocket"
)

func getenv(name string) string { return os.Getenv(name) }
func estimateTokens(raw []byte) int {
	var value any
	_ = json.Unmarshal(raw, &value)
	chars, images := 0, 0
	var walk func(any)
	walk = func(v any) {
		switch x := v.(type) {
		case string:
			chars += utf8.RuneCountInString(x)
		case []any:
			for _, child := range x {
				walk(child)
			}
		case map[string]any:
			if x["type"] == "image" || x["inlineData"] != nil {
				images++
			}
			for _, child := range x {
				walk(child)
			}
		}
	}
	walk(value)
	return max(1, chars/4+images*300)
}

type logBuffer struct {
	mu        sync.Mutex
	lines     []string
	listeners map[chan string]bool
}

func newLogBuffer() *logBuffer { return &logBuffer{listeners: map[chan string]bool{}} }
func (l *logBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, line := range strings.Split(strings.TrimSpace(string(p)), "\n") {
		l.lines = append(l.lines, line)
		for ch := range l.listeners {
			select {
			case ch <- line:
			default:
			}
		}
	}
	if len(l.lines) > 1000 {
		l.lines = append([]string{}, l.lines[len(l.lines)-1000:]...)
	}
	return len(p), nil
}
func (s *server) logHandler(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/logs/download":
		s.logs.mu.Lock()
		data := []byte(strings.Join(s.logs.lines, "\n"))
		s.logs.mu.Unlock()
		download(w, "text/plain", "runtime.log", data)
	case "/logs/clear":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		s.logs.mu.Lock()
		s.logs.lines = nil
		s.logs.mu.Unlock()
		jsonResponse(w, 200, object{"message": "日志已清空"})
	case "/logs/stream":
		upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		ch := make(chan string, 64)
		s.logs.mu.Lock()
		history := append([]string{}, s.logs.lines...)
		s.logs.listeners[ch] = true
		s.logs.mu.Unlock()
		defer func() { s.logs.mu.Lock(); delete(s.logs.listeners, ch); s.logs.mu.Unlock() }()
		done := make(chan struct{})
		go func() {
			defer close(done)
			for {
				if _, _, err := conn.ReadMessage(); err != nil {
					return
				}
			}
		}()
		write := func(line string) error {
			_ = conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			return conn.WriteMessage(websocket.TextMessage, []byte(line))
		}
		for _, line := range history {
			if write(line) != nil {
				return
			}
		}
		for {
			select {
			case <-done:
				return
			case <-s.ctx.Done():
				return
			case line := <-ch:
				if write(line) != nil {
					return
				}
			}
		}
	default:
		failure(w, 404, "Not found")
	}
}

type captureState struct {
	active  atomic.Bool
	mu      sync.Mutex
	kind    string
	target  int
	items   [][]byte
	started float64
}

func (c *captureState) status() object {
	c.mu.Lock()
	defer c.mu.Unlock()
	return object{"active": c.active.Load(), "running": c.active.Load(), "capture_type": c.kind, "target_count": c.target, "captured_count": len(c.items), "started_at": c.started}
}
func (c *captureState) recordResult(r *http.Request, raw []byte, status int) {
	if !c.active.Load() {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.active.Load() || (c.kind != "all" && c.kind != fmt.Sprint(status)) {
		return
	}
	var request any
	_ = json.Unmarshal(raw, &request)
	c.items = append(c.items, encoded(object{"time": time.Now().UTC().Format(time.RFC3339Nano), "path": r.URL.Path, "status": status, "request": request}))
	if len(c.items) >= c.target {
		c.active.Store(false)
	}
}
func (s *server) captureHandler(w http.ResponseWriter, r *http.Request) {
	c := &s.capture
	switch strings.TrimPrefix(r.URL.Path, "/config/request-capture/") {
	case "status":
		jsonResponse(w, 200, object{"status": c.status()})
	case "start":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		body, ok := readObject(w, r)
		if !ok {
			return
		}
		kind := text(body["capture_type"])
		if kind == "" {
			kind = "400"
		}
		if kind != "400" && kind != "429" && kind != "all" {
			failure(w, 400, "无效的捕捉类型")
			return
		}
		count := int(number(body["target_count"]))
		if count < 1 || count > 100 {
			failure(w, 400, "捕捉数量应为 1 到 100")
			return
		}
		c.mu.Lock()
		c.kind = kind
		c.target = count
		c.items = nil
		c.started = unixNow()
		c.active.Store(true)
		c.mu.Unlock()
		jsonResponse(w, 200, object{"status": c.status()})
	case "stop":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		c.active.Store(false)
		jsonResponse(w, 200, object{"status": c.status()})
	case "download":
		c.mu.Lock()
		items := append([][]byte{}, c.items...)
		c.mu.Unlock()
		if len(items) == 0 {
			failure(w, 404, "还没有捕捉到请求")
			return
		}
		var b bytes.Buffer
		z := zip.NewWriter(&b)
		for i, item := range items {
			f, _ := z.Create(fmt.Sprintf("request-%03d.json", i+1))
			_, _ = f.Write(item)
		}
		_ = z.Close()
		download(w, "application/zip", "request-capture.zip", b.Bytes())
	default:
		failure(w, 404, "Not found")
	}
}

type oauthState struct {
	mu                        sync.Mutex
	state, verifier, redirect string
	expires                   time.Time
	result                    object
	listener                  net.Listener
}

func randomID() string {
	var b [32]byte
	_, _ = rand.Read(b[:])
	return base64.RawURLEncoding.EncodeToString(b[:])
}
func (s *server) oauthHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" && !strings.HasPrefix(r.URL.Path, "/auth/status/") {
		failure(w, 405, "Method not allowed")
		return
	}
	switch r.URL.Path {
	case "/auth/start":
		s.oauth.mu.Lock()
		if s.oauth.listener != nil {
			_ = s.oauth.listener.Close()
		}
		listener, err := net.Listen("tcp", "0.0.0.0:0")
		if err != nil {
			s.oauth.mu.Unlock()
			failure(w, 500, err.Error())
			return
		}
		s.oauth.listener = listener
		s.oauth.state = randomID()
		s.oauth.verifier = randomID()
		s.oauth.redirect = fmt.Sprintf("http://localhost:%d/oauth-callback", listener.Addr().(*net.TCPAddr).Port)
		s.oauth.expires = time.Now().Add(10 * time.Minute)
		s.oauth.result = nil
		state, verifier, redirect := s.oauth.state, s.oauth.verifier, s.oauth.redirect
		s.oauth.mu.Unlock()
		callback := &http.Server{Handler: http.HandlerFunc(s.oauthCallback), ReadHeaderTimeout: 10 * time.Second}
		go func() { _ = callback.Serve(listener) }()
		go func() {
			select {
			case <-time.After(10 * time.Minute):
			case <-s.ctx.Done():
			}
			_ = callback.Close()
		}()
		hash := sha256.Sum256([]byte(verifier))
		values := url.Values{"client_id": {oauthClient}, "redirect_uri": {redirect}, "response_type": {"code"}, "access_type": {"offline"}, "prompt": {"consent"}, "state": {state}, "code_challenge": {base64.RawURLEncoding.EncodeToString(hash[:])}, "code_challenge_method": {"S256"}, "scope": {"https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/aicode https://www.googleapis.com/auth/cclog https://www.googleapis.com/auth/experimentsandconfigs"}}
		jsonResponse(w, 200, object{"auth_url": "https://accounts.google.com/o/oauth2/v2/auth?" + values.Encode(), "state": state, "auto_project_detection": true})
	case "/auth/callback-url":
		body, ok := readObject(w, r)
		if !ok {
			return
		}
		u, err := url.Parse(text(body["callback_url"]))
		if err != nil {
			failure(w, 400, "回调地址无效")
			return
		}
		result, err := s.completeOAuth(r.Context(), u.Query())
		if err != nil {
			failure(w, 400, err.Error())
			return
		}
		jsonResponse(w, 200, result)
	case "/auth/callback":
		for {
			s.oauth.mu.Lock()
			result, expires := s.oauth.result, s.oauth.expires
			s.oauth.mu.Unlock()
			if result != nil {
				jsonResponse(w, 200, result)
				return
			}
			if time.Now().After(expires) {
				failure(w, 400, "授权已过期，请重新开始")
				return
			}
			select {
			case <-r.Context().Done():
				return
			case <-time.After(250 * time.Millisecond):
			}
		}
	default:
		s.oauth.mu.Lock()
		result := s.oauth.result
		s.oauth.mu.Unlock()
		if result == nil {
			result = object{"status": "pending"}
		}
		jsonResponse(w, 200, result)
	}
}
func (s *server) oauthCallback(w http.ResponseWriter, r *http.Request) {
	result, err := s.completeOAuth(r.Context(), r.URL.Query())
	if err != nil {
		failure(w, 400, err.Error())
		return
	}
	jsonResponse(w, 200, object{"message": "授权成功，可以关闭这个页面", "file_path": result["file_path"]})
}
func (s *server) completeOAuth(ctx context.Context, query url.Values) (object, error) {
	s.oauth.mu.Lock()
	defer s.oauth.mu.Unlock()
	if s.oauth.state == "" || query.Get("state") != s.oauth.state || time.Now().After(s.oauth.expires) {
		return nil, fmt.Errorf("授权状态不匹配或已过期")
	}
	if s.oauth.result != nil {
		return s.oauth.result, nil
	}
	if query.Get("code") == "" {
		return nil, fmt.Errorf("回调地址缺少授权码")
	}
	form := url.Values{"client_id": {oauthClient}, "client_secret": {oauthSecret}, "code": {query.Get("code")}, "redirect_uri": {s.oauth.redirect}, "grant_type": {"authorization_code"}, "code_verifier": {s.oauth.verifier}}
	c := newCredential("", object{}, object{})
	raw, err := s.httpCall(ctx, c, "POST", strings.TrimRight(text(s.store.value("oauth_proxy_url")), "/")+"/token", []byte(form.Encode()), true)
	if err != nil {
		return nil, redactError(err)
	}
	var data object
	if err = json.Unmarshal(raw, &data); err != nil {
		return nil, err
	}
	data["expiry"] = time.Now().Add(time.Duration(number(data["expires_in"])) * time.Second).UTC().Format(time.RFC3339Nano)
	data["type"] = "antigravity"
	c.data.Store(&data)
	email, err := s.email(ctx, c)
	if err != nil {
		return nil, redactError(err)
	}
	if err = s.refreshInfo(ctx, c); err != nil {
		return nil, redactError(err)
	}
	name := email + ".json"
	stored, err := s.store.put(name, *c.data.Load())
	if err != nil {
		return nil, err
	}
	state := clone(*stored.state.Load())
	for key, value := range *c.state.Load() {
		state[key] = value
	}
	stored.state.Store(&state)
	stored.dirty.Store(true)
	s.store.flush()
	s.oauth.result = object{"success": true, "credentials": *stored.data.Load(), "file_path": name, "message": "认证成功，凭证已保存", "auto_detected_project": true}
	return s.oauth.result, nil
}

type quotaItem struct {
	result object
	at     time.Time
	mu     sync.Mutex
}
type quotaState struct {
	cache    sync.Map
	mu       sync.Mutex
	snapshot object
	running  bool
}

func quotaWindows(data object) object {
	buckets := map[string]object{}
	for _, g := range list(data["groups"]) {
		for _, v := range list(obj(g)["buckets"]) {
			b := obj(v)
			buckets[text(b["bucketId"])] = b
		}
	}
	windows := object{}
	for family, prefix := range map[string]string{"gemini": "gemini", "claude": "3p"} {
		periods := object{}
		for period, suffix := range map[string]string{"5h": "5h", "7d": "weekly"} {
			b, ok := buckets[prefix+"-"+suffix]
			if !ok {
				continue
			}
			var fraction any
			if v, ok := b["remainingFraction"].(float64); ok {
				fraction = math.Max(0, math.Min(1, v))
			}
			raw := fraction
			if truth(b["disabled"]) {
				fraction = float64(0)
			}
			periods[period] = object{"remaining_fraction": fraction, "raw_remaining_fraction": raw, "reset_time": b["resetTime"], "disabled": truth(b["disabled"])}
		}
		five, week := obj(periods["5h"]), obj(periods["7d"])
		if five["remaining_fraction"] != nil && week["remaining_fraction"] != nil {
			ratio := 6.0
			if family == "claude" {
				ratio = 2
			}
			v := number(week["remaining_fraction"]) * ratio
			five["weekly_limited"] = v < number(five["remaining_fraction"])
			five["remaining_fraction"] = math.Min(number(five["remaining_fraction"]), v)
		}
		for _, v := range periods {
			p := obj(v)
			if p["remaining_fraction"] != nil {
				p["remaining_percent"] = math.Round(number(p["remaining_fraction"])*10000) / 100
			}
		}
		if len(periods) > 0 {
			windows[family] = periods
		}
	}
	return object{"success": true, "windows": windows, "supported": len(windows) > 0}
}
func (s *server) accountQuota(ctx context.Context, c *credential, force bool) (object, error) {
	v, _ := s.quota.cache.LoadOrStore(c.name, &quotaItem{})
	entry := v.(*quotaItem)
	entry.mu.Lock()
	defer entry.mu.Unlock()
	if !force && entry.result != nil && time.Since(entry.at) < time.Minute {
		return entry.result, nil
	}
	data, err := s.cloudCall(ctx, c, "retrieveUserQuotaSummary", object{"project": (*c.data.Load())["project_id"]})
	if err != nil {
		return nil, err
	}
	result := quotaWindows(data)
	result["filename"] = c.name
	entry.result = result
	entry.at = time.Now()
	return result, nil
}
func (s *server) quotaSummary(w http.ResponseWriter, r *http.Request, start bool) {
	s.quota.mu.Lock()
	snapshot := clone(s.quota.snapshot)
	if start && !s.quota.running {
		s.quota.running = true
		snapshot = object{"success": true, "running": true, "total": len(s.store.accounts.Load().all), "completed": 0, "failed": 0, "started_at": unixNow()}
		s.quota.snapshot = snapshot
		go s.runQuotaSummary(r.URL.Query().Get("force") == "true")
	}
	running := s.quota.running
	s.quota.mu.Unlock()
	status := 200
	if start && running {
		status = 202
	}
	jsonResponse(w, status, snapshot)
}
func (s *server) runQuotaSummary(force bool) {
	var mu sync.Mutex
	results := []object{}
	sem := make(chan struct{}, 8)
	var wg sync.WaitGroup
	for _, c := range s.store.accounts.Load().all {
		if truth((*c.state.Load())["disabled"]) {
			continue
		}
		select {
		case sem <- struct{}{}:
		case <-s.ctx.Done():
			return
		}
		wg.Add(1)
		go func(c *credential) {
			defer wg.Done()
			defer func() { <-sem }()
			ctx, cancel := context.WithTimeout(s.ctx, 40*time.Second)
			defer cancel()
			result, err := s.accountQuota(ctx, c, force)
			if err != nil {
				result = object{"success": false, "filename": c.name, "error": redactError(err).Error()}
			}
			mu.Lock()
			results = append(results, result)
			completed := len(results)
			mu.Unlock()
			s.quota.mu.Lock()
			next := clone(s.quota.snapshot)
			next["completed"] = completed
			if err != nil {
				next["failed"] = number(next["failed"]) + 1
			}
			s.quota.snapshot = next
			s.quota.mu.Unlock()
		}(c)
	}
	wg.Wait()
	summaries := []any{}
	failures := []any{}
	for _, result := range results {
		if !truth(result["success"]) {
			failures = append(failures, result)
		}
	}
	for _, family := range []string{"gemini", "claude"} {
		for _, period := range []string{"5h", "7d"} {
			sum := 0.0
			count, exhausted := 0, 0
			for _, result := range results {
				item := obj(obj(obj(result["windows"])[family])[period])
				if v := item["remaining_fraction"]; v != nil {
					n := number(v)
					sum += n
					count++
					if n <= 0.0002 {
						exhausted++
					}
				}
			}
			var percent any
			if count > 0 {
				percent = math.Round(sum/float64(count)*10000) / 100
			}
			summaries = append(summaries, object{"family": family, "period": period, "account_count": count, "exhausted_accounts": exhausted, "remaining_percent": percent})
		}
	}
	result := object{"success": true, "enabled_accounts": len(results), "checked_accounts": len(results), "failed_accounts": len(failures), "model_summaries": summaries, "updated_at": unixNow(), "errors": failures}
	s.quota.mu.Lock()
	s.quota.running = false
	s.quota.snapshot = object{"success": true, "running": false, "completed": len(results), "total": len(results), "result": result}
	s.quota.mu.Unlock()
}
