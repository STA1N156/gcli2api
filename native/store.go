package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	_ "modernc.org/sqlite"
)

type object map[string]any

func text(v any) string    { s, _ := v.(string); return s }
func number(v any) float64 { n, _ := v.(float64); return n }
func obj(v any) object {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	if m, ok := v.(object); ok {
		return m
	}
	return object{}
}
func list(v any) []any { a, _ := v.([]any); return a }
func clone(m object) object {
	out := object{}
	for k, v := range m {
		out[k] = v
	}
	return out
}
func encoded(v any) []byte { b, _ := json.Marshal(v); return b }
func truth(v any) bool {
	if b, ok := v.(bool); ok {
		return b
	}
	return v == "true" || v == "1" || v == float64(1)
}
func unixNow() float64 { return float64(time.Now().UnixMilli()) / 1000 }

type credential struct {
	name    string
	data    atomic.Pointer[object]
	state   atomic.Pointer[object]
	mu      sync.Mutex
	refresh sync.Mutex
	dirty   atomic.Bool
	calls   atomic.Int64
	success atomic.Int64
}

func newCredential(name string, data, state object) *credential {
	c := &credential{name: name}
	c.data.Store(&data)
	c.state.Store(&state)
	c.calls.Store(int64(number(state["call_count"])))
	c.success.Store(int64(number(state["last_success"]) * 1000))
	return c
}
func (c *credential) change(fn func(object)) {
	c.mu.Lock()
	state := clone(*c.state.Load())
	fn(state)
	c.state.Store(&state)
	c.dirty.Store(true)
	c.mu.Unlock()
}
func (c *credential) available(model string, now float64) bool {
	s := *c.state.Load()
	if truth(s["disabled"]) {
		return false
	}
	for key, v := range obj(s["model_cooldowns"]) {
		if (key == model || key == "*" || key == "all" || (key == "claude" && strings.Contains(model, "claude")) || (key == "gemini" && strings.Contains(model, "gemini"))) && number(v) > now {
			return false
		}
	}
	models := list(s["supported_models"])
	if len(models) == 0 {
		return true
	}
	for _, v := range models {
		if compatibleModel(text(v), model) {
			return true
		}
	}
	return false
}
func compatibleModel(advertised, requested string) bool {
	a, b := modelKey(advertised), modelKey(requested)
	if a == b {
		return true
	}
	for _, level := range []string{"low", "medium", "high", "minimal", "max", "xhigh"} {
		if strings.HasSuffix(b, "-"+level) {
			base := strings.TrimSuffix(b, "-"+level)
			if strings.HasSuffix(base, "-flash-lite") && a == base {
				return true
			}
			if (level == "max" || level == "xhigh") && a == base+"-high" {
				return true
			}
		}
	}
	if b == "gemini-pro-agent" && a == "gemini-3.1-pro-high" {
		return true
	}
	return strings.HasPrefix(a, b+"-") && (strings.HasSuffix(a, "-low") || strings.HasSuffix(a, "-medium") || strings.HasSuffix(a, "-high"))
}
func modelKey(s string) string {
	s = strings.TrimPrefix(strings.ToLower(s), "models/")
	if strings.HasPrefix(s, "claude-") {
		s = strings.TrimSuffix(s, "-thinking")
	}
	return s
}

type accountList struct {
	all    []*credential
	byName map[string]*credential
}
type store struct {
	done      chan struct{}
	db        *sql.DB
	write     sync.Mutex
	accounts  atomic.Pointer[accountList]
	config    atomic.Pointer[object]
	effective atomic.Pointer[object]
	mu        sync.Mutex
	dir       string
}

var defaults = object{
	"credentials_dir": "./creds", "host": "0.0.0.0", "port": float64(7861), "password": "pwd",
	"antigravity_api_url": "https://daily-cloudcode-pa.googleapis.com", "oauth_proxy_url": "https://oauth2.googleapis.com", "googleapis_proxy_url": "https://www.googleapis.com",
	"proxy": "", "credential_retry_limit_enabled": false, "max_retry_credentials": float64(5), "credential_retry_interval": float64(0.1),
	"session_affinity_enabled": true, "session_affinity_ttl_seconds": float64(3600), "return_thoughts_to_frontend": true,
	"empty_output_error_enabled": true, "compatibility_mode_enabled": false, "antigravity_stream2nostream": false,
	"keepalive_url": "", "keepalive_interval": float64(60),
}
var envConfig = map[string]string{
	"CREDENTIALS_DIR": "credentials_dir", "HOST": "host", "PORT": "port", "PASSWORD": "password", "API_PASSWORD": "api_password", "PANEL_PASSWORD": "panel_password",
	"PROXY": "proxy", "ANTIGRAVITY_API_URL": "antigravity_api_url", "OAUTH_PROXY_URL": "oauth_proxy_url", "GOOGLEAPIS_PROXY_URL": "googleapis_proxy_url",
	"CREDENTIAL_RETRY_LIMIT_ENABLED": "credential_retry_limit_enabled", "MAX_RETRY_CREDENTIALS": "max_retry_credentials", "CREDENTIAL_RETRY_INTERVAL": "credential_retry_interval",
	"SESSION_AFFINITY_ENABLED": "session_affinity_enabled", "SESSION_AFFINITY_TTL_SECONDS": "session_affinity_ttl_seconds",
	"RETURN_THOUGHTS_TO_FRONTEND": "return_thoughts_to_frontend", "EMPTY_OUTPUT_ERROR_ENABLED": "empty_output_error_enabled", "COMPATIBILITY_MODE": "compatibility_mode_enabled",
	"ANTIGRAVITY_STREAM2NOSTREAM": "antigravity_stream2nostream", "KEEPALIVE_URL": "keepalive_url", "KEEPALIVE_INTERVAL": "keepalive_interval",
}

func openStore(ctx context.Context) (*store, error) {
	dir := os.Getenv("CREDENTIALS_DIR")
	if dir == "" {
		dir = "./creds"
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", filepath.Join(dir, "credentials.db"))
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	s := &store{db: db, dir: dir, done: make(chan struct{})}
	ok := false
	defer func() {
		if !ok {
			db.Close()
		}
	}()
	_, err = db.Exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=10000;
	CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at REAL DEFAULT (unixepoch()));
	CREATE TABLE IF NOT EXISTS antigravity_credentials(id INTEGER PRIMARY KEY AUTOINCREMENT,filename TEXT UNIQUE NOT NULL,credential_data TEXT NOT NULL,disabled INTEGER DEFAULT 0,error_codes TEXT DEFAULT '[]',error_messages TEXT DEFAULT '[]',last_success REAL,user_email TEXT,model_cooldowns TEXT DEFAULT '{}',tier TEXT DEFAULT 'pro',enable_credit INTEGER DEFAULT 0,supported_models TEXT,rotation_order INTEGER DEFAULT 0,call_count INTEGER DEFAULT 0,created_at REAL DEFAULT (unixepoch()),updated_at REAL DEFAULT (unixepoch()));`)
	if err != nil {
		return nil, err
	}
	rows, err := db.Query("PRAGMA table_info(antigravity_credentials)")
	if err != nil {
		return nil, err
	}
	columns := map[string]bool{}
	for rows.Next() {
		var cid, notnull, pk int
		var name, typ string
		var def any
		if err = rows.Scan(&cid, &name, &typ, &notnull, &def, &pk); err != nil {
			rows.Close()
			return nil, err
		}
		columns[name] = true
	}
	rows.Close()
	for key, typ := range map[string]string{"supported_models": "TEXT", "enable_credit": "INTEGER DEFAULT 0"} {
		if !columns[key] {
			if _, err = db.Exec("ALTER TABLE antigravity_credentials ADD COLUMN " + key + " " + typ); err != nil {
				return nil, err
			}
		}
	}
	cfg := clone(defaults)
	rows, err = db.Query("SELECT key,value FROM config")
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var k, v string
		if err = rows.Scan(&k, &v); err != nil {
			rows.Close()
			return nil, err
		}
		var val any
		if json.Unmarshal([]byte(v), &val) == nil {
			cfg[k] = val
		}
	}
	rows.Close()
	s.config.Store(&cfg)
	s.applyConfig(cfg)
	accounts := &accountList{byName: map[string]*credential{}}
	rows, err = db.Query(`SELECT filename,credential_data,disabled,COALESCE(error_codes,'[]'),COALESCE(error_messages,'[]'),COALESCE(last_success,0),COALESCE(user_email,''),COALESCE(model_cooldowns,'{}'),COALESCE(tier,'pro'),enable_credit,COALESCE(supported_models,'null'),call_count FROM antigravity_credentials ORDER BY rotation_order,id`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var name, raw, codes, msgs, email, cools, tier, models string
		var disabled, credit int
		var success float64
		var calls int64
		if err = rows.Scan(&name, &raw, &disabled, &codes, &msgs, &success, &email, &cools, &tier, &credit, &models, &calls); err != nil {
			rows.Close()
			return nil, err
		}
		var data object
		if json.Unmarshal([]byte(raw), &data) != nil {
			continue
		}
		state := object{"disabled": disabled != 0, "user_email": email, "tier": tier, "enable_credit": credit != 0, "last_success": success, "call_count": float64(calls)}
		for k, v := range map[string]string{"error_codes": codes, "error_messages": msgs, "model_cooldowns": cools, "supported_models": models} {
			var x any
			_ = json.Unmarshal([]byte(v), &x)
			state[k] = x
		}
		c := newCredential(name, data, state)
		if clearLegacyConfigurationFailure(state) {
			c.dirty.Store(true)
		}
		accounts.all = append(accounts.all, c)
		accounts.byName[name] = c
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	s.accounts.Store(accounts)
	go func() {
		defer close(s.done)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				s.flush()
				return
			case <-ticker.C:
				s.flush()
			}
		}
	}()
	ok = true
	return s, nil
}

// Repair only the model state produced by the old local configuration gate.
func clearLegacyConfigurationFailure(state object) bool {
	messages := obj(state["error_messages"])
	detail := obj(messages["404"])
	message, model := text(detail["message"]), text(detail["model"])
	if model == "" || !strings.Contains(message, "unknown model:") ||
		(!strings.Contains(message, "目录里的安全监控配置未核对") && !strings.Contains(message, "目录里的系统提示模板未核对")) {
		return false
	}
	delete(messages, "404")
	delete(obj(state["model_cooldowns"]), model)
	codes := []any{}
	for _, code := range list(state["error_codes"]) {
		if number(code) != 404 {
			codes = append(codes, code)
		}
	}
	state["error_codes"] = codes
	models := list(state["supported_models"])
	if len(models) > 0 {
		found := false
		for _, value := range models {
			found = found || text(value) == model
		}
		if !found {
			state["supported_models"] = append(models, model)
		}
	}
	return true
}
func (s *store) value(key string) any {
	return (*s.effective.Load())[key]
}
func (s *store) applyConfig(cfg object) {
	next := clone(cfg)
	for env, key := range envConfig {
		if v := os.Getenv(env); v != "" {
			var value any = v
			switch defaults[key].(type) {
			case bool:
				value = truth(v)
			case float64:
				value, _ = strconv.ParseFloat(v, 64)
			}
			next[key] = value
		}
	}
	s.effective.Store(&next)
}
func (s *store) password(panel bool) string {
	k := "api_password"
	if panel {
		k = "panel_password"
	}
	if p := text(s.value(k)); p != "" {
		return p
	}
	return text(s.value("password"))
}
func (s *store) saveConfig(updates object) error {
	s.write.Lock()
	defer s.write.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	next := clone(*s.config.Load())
	for k, v := range updates {
		locked := false
		for env, key := range envConfig {
			if key == k && os.Getenv(env) != "" {
				locked = true
				break
			}
		}
		if locked {
			continue
		}
		if _, err = tx.Exec("INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=unixepoch()", k, string(encoded(v))); err != nil {
			return err
		}
		next[k] = v
	}
	if err = tx.Commit(); err == nil {
		s.config.Store(&next)
		s.applyConfig(next)
	}
	return err
}
func writeCredential(tx *sql.Tx, c *credential) error {
	state := *c.state.Load()
	_, err := tx.Exec(`INSERT INTO antigravity_credentials(filename,credential_data,disabled,error_codes,error_messages,last_success,user_email,model_cooldowns,tier,enable_credit,supported_models,call_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(filename) DO UPDATE SET credential_data=excluded.credential_data,disabled=excluded.disabled,error_codes=excluded.error_codes,error_messages=excluded.error_messages,last_success=excluded.last_success,user_email=excluded.user_email,model_cooldowns=excluded.model_cooldowns,tier=excluded.tier,enable_credit=excluded.enable_credit,supported_models=excluded.supported_models,call_count=excluded.call_count,updated_at=unixepoch()`, c.name, string(encoded(*c.data.Load())), truth(state["disabled"]), string(encoded(state["error_codes"])), string(encoded(state["error_messages"])), float64(c.success.Load())/1000, text(state["user_email"]), string(encoded(state["model_cooldowns"])), text(state["tier"]), truth(state["enable_credit"]), string(encoded(state["supported_models"])), c.calls.Load())
	return err
}
func (s *store) flush() {
	s.write.Lock()
	defer s.write.Unlock()
	var pending []*credential
	for _, c := range s.accounts.Load().all {
		if c.dirty.Swap(false) {
			pending = append(pending, c)
		}
	}
	if len(pending) == 0 {
		return
	}
	tx, err := s.db.Begin()
	if err == nil {
		for _, c := range pending {
			if err = writeCredential(tx, c); err != nil {
				break
			}
		}
		if err == nil {
			err = tx.Commit()
		} else {
			tx.Rollback()
		}
	}
	if err != nil {
		for _, c := range pending {
			c.dirty.Store(true)
		}
		log.Printf("credential state persistence: %v", err)
	}
}
func (s *store) put(name string, data object) (*credential, error) {
	if name != filepath.Base(name) || !strings.HasSuffix(strings.ToLower(name), ".json") {
		return nil, fmt.Errorf("无效的凭证文件名")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	current := s.accounts.Load()
	c := current.byName[name]
	if c == nil {
		c = newCredential(name, data, object{"disabled": truth(data["disabled"]), "error_codes": []any{}, "error_messages": []any{}, "model_cooldowns": object{}, "tier": "pro", "user_email": text(data["email"]), "enable_credit": false})
	} else {
		c.data.Store(&data)
	}
	c.dirty.Store(true)
	if current.byName[name] == nil {
		next := &accountList{all: append(append([]*credential{}, current.all...), c), byName: map[string]*credential{}}
		for _, a := range next.all {
			next.byName[a.name] = a
		}
		s.accounts.Store(next)
	}
	return c, nil
}
func (s *store) remove(name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.write.Lock()
	defer s.write.Unlock()
	if _, err := s.db.Exec("DELETE FROM antigravity_credentials WHERE filename=?", name); err != nil {
		return err
	}
	next := &accountList{byName: map[string]*credential{}}
	for _, c := range s.accounts.Load().all {
		if c.name != name {
			next.all = append(next.all, c)
			next.byName[c.name] = c
		}
	}
	s.accounts.Store(next)
	return nil
}
func (s *store) names() []string {
	names := make([]string, 0, len(s.accounts.Load().all))
	for _, c := range s.accounts.Load().all {
		names = append(names, c.name)
	}
	sort.Strings(names)
	return names
}
