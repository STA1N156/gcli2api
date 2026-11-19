package main

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	translator "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
)

func (s *server) panel(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	if path == "/auth/login" {
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		p, ok := readObject(w, r)
		if !ok {
			return
		}
		request := r.Clone(r.Context())
		request.Header = r.Header.Clone()
		request.Header.Set("Authorization", "Bearer "+text(p["password"]))
		if !s.authorized(request, true) {
			failure(w, 401, "密码错误")
			return
		}
		jsonResponse(w, 200, object{"token": p["password"], "message": "登录成功"})
		return
	}
	if strings.HasPrefix(path, "/auth/") {
		s.oauthHandler(w, r)
		return
	}
	if path == "/version/info" {
		s.versionInfo(w, r)
		return
	}
	if strings.HasPrefix(path, "/logs/") {
		s.logHandler(w, r)
		return
	}
	if strings.HasPrefix(path, "/config/request-capture/") {
		s.captureHandler(w, r)
		return
	}
	if path == "/config/get" {
		cfg := clone(*s.store.config.Load())
		locked := []string{}
		for key := range cfg {
			cfg[key] = s.store.value(key)
		}
		for env, key := range envConfig {
			if value := getenv(env); value != "" {
				locked = append(locked, key)
				cfg[key] = s.store.value(key)
			}
		}
		cfg["api_password"] = s.store.password(false)
		cfg["panel_password"] = s.store.password(true)
		jsonResponse(w, 200, object{"config": cfg, "env_locked": locked})
		return
	}
	if path == "/config/save" && r.Method == "POST" {
		body, ok := readObject(w, r)
		if !ok {
			return
		}
		cfg := obj(body["config"])
		for _, key := range []string{"max_retry_credentials", "session_affinity_ttl_seconds", "port", "keepalive_interval"} {
			if val, ok := cfg[key]; ok && number(val) < 1 {
				failure(w, 400, key+" 必须大于零")
				return
			}
		}
		if err := s.store.saveConfig(cfg); err != nil {
			failure(w, 500, err.Error())
			return
		}
		jsonResponse(w, 200, object{"message": "配置已保存"})
		return
	}
	if !strings.HasPrefix(path, "/creds/") {
		failure(w, 404, "Not found")
		return
	}
	if mode := r.URL.Query().Get("mode"); mode != "" && mode != "antigravity" {
		failure(w, 400, "仅支持 antigravity")
		return
	}
	operation := strings.TrimPrefix(path, "/creds/")
	switch operation {
	case "status":
		s.credentialStatus(w, r)
		return
	case "upload":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		s.upload(w, r)
		return
	case "download-all":
		s.downloadAll(w, r)
		return
	case "action", "batch-action":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		body, ok := readObject(w, r)
		if !ok {
			return
		}
		names := list(body["filenames"])
		if operation == "action" {
			names = []any{body["filename"]}
		}
		failures := []string{}
		success := 0
		for _, v := range names {
			if err := s.credentialAction(text(v), text(body["action"])); err != nil {
				failures = append(failures, fmt.Sprintf("%s: %v", text(v), err))
			} else {
				success++
			}
		}
		s.store.flush()
		jsonResponse(w, 200, object{"message": fmt.Sprintf("已处理 %d/%d 个凭证", success, len(names)), "success_count": success, "total_count": len(names), "errors": failures})
		return
	case "clear-all-abnormal-status":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		for _, c := range s.store.accounts.Load().all {
			clearCredential(c)
		}
		s.store.flush()
		jsonResponse(w, 200, object{"success": true, "message": "异常状态和模型冷却已清理", "cleared_count": len(s.store.accounts.Load().all)})
		return
	case "antigravity-credit-summary", "antigravity-credit-summary-progress":
		s.quotaSummary(w, r, operation == "antigravity-credit-summary")
		return
	case "refresh-all-emails":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		success := 0
		failures := []string{}
		for _, c := range s.store.accounts.Load().all {
			if _, err := s.email(r.Context(), c); err != nil {
				failures = append(failures, c.name+": "+redactError(err).Error())
			} else {
				success++
			}
		}
		s.store.flush()
		jsonResponse(w, 200, object{"success_count": success, "errors": failures, "message": fmt.Sprintf("已更新 %d 个邮箱", success)})
		return
	case "deduplicate-by-email":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		seen := map[string]bool{}
		removed := 0
		for _, c := range s.store.accounts.Load().all {
			email := strings.ToLower(text((*c.state.Load())["user_email"]))
			if email == "" {
				continue
			}
			if seen[email] {
				if err := s.store.remove(c.name); err != nil {
					failure(w, 500, err.Error())
					return
				}
				removed++
			} else {
				seen[email] = true
			}
		}
		jsonResponse(w, 200, object{"success": true, "deleted_count": removed, "message": fmt.Sprintf("已移除 %d 个重复凭证", removed)})
		return
	}
	action, name, ok := strings.Cut(operation, "/")
	if !ok {
		failure(w, 404, "Not found")
		return
	}
	name, err := url.PathUnescape(name)
	if err != nil || name != filepath.Base(name) {
		failure(w, 400, "无效的凭证名称")
		return
	}
	c := s.store.accounts.Load().byName[name]
	if c == nil {
		failure(w, 404, "凭证不存在")
		return
	}
	switch action {
	case "detail":
		state := clone(*c.state.Load())
		state["last_success"] = float64(c.success.Load()) / 1000
		jsonResponse(w, 200, object{"filename": name, "content": *c.data.Load(), "status": state, "backend_type": "sqlite", "user_email": state["user_email"], "model_cooldowns": state["model_cooldowns"], "enable_credit": state["enable_credit"]})
	case "proxy":
		if r.Method == "PUT" {
			body, ok := readObject(w, r)
			if !ok {
				return
			}
			p := text(body["proxy_url"])
			if err := validProxy(p); err != nil {
				failure(w, 400, err.Error())
				return
			}
			next := clone(*c.data.Load())
			delete(next, "proxy-url")
			next["proxy_url"] = p
			c.data.Store(&next)
			c.dirty.Store(true)
			s.store.flush()
			jsonResponse(w, 200, object{"message": "账号代理已保存"})
			return
		}
		data := *c.data.Load()
		p := text(data["proxy_url"])
		if p == "" {
			p = text(data["proxy-url"])
		}
		jsonResponse(w, 200, object{"proxy_url": p})
	case "download":
		download(w, "application/json", name, encoded(*c.data.Load()))
	case "errors":
		state := *c.state.Load()
		jsonResponse(w, 200, object{"filename": name, "error_codes": state["error_codes"], "error_messages": state["error_messages"]})
	case "fetch-email":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		email, err := s.email(r.Context(), c)
		if err != nil {
			failure(w, 502, redactError(err).Error())
			return
		}
		s.store.flush()
		jsonResponse(w, 200, object{"success": true, "user_email": email, "message": "邮箱已更新"})
	case "models", "refresh-models":
		var models []string
		if action == "models" {
			for _, v := range list((*c.state.Load())["supported_models"]) {
				models = append(models, text(v))
			}
		}
		if models == nil {
			models, err = s.accountModels(r.Context(), c)
		}
		if err != nil {
			writeUpstreamError(w, err)
			return
		}
		sort.Strings(models)
		s.store.flush()
		jsonResponse(w, 200, object{"success": true, "models": models, "count": len(models)})
	case "verify-project":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		if err = s.refreshInfo(r.Context(), c); err != nil {
			writeUpstreamError(w, err)
			return
		}
		s.store.flush()
		jsonResponse(w, 200, object{"success": true, "project_id": (*c.data.Load())["project_id"], "tier": (*c.state.Load())["tier"], "message": "项目和订阅等级已更新"})
	case "quota":
		result, err := s.accountQuota(r.Context(), c, r.URL.Query().Get("force") == "true")
		if err != nil {
			writeUpstreamError(w, err)
			return
		}
		jsonResponse(w, 200, result)
	case "test":
		if r.Method != "POST" {
			failure(w, 405, "Method not allowed")
			return
		}
		s.testCredential(w, r, c)
	default:
		failure(w, 404, "Not found")
	}
}
func clearCredential(c *credential) {
	c.change(func(state object) {
		state["error_codes"] = []any{}
		state["error_messages"] = object{}
		state["model_cooldowns"] = object{}
	})
}
func (s *server) credentialAction(name, action string) error {
	c := s.store.accounts.Load().byName[name]
	if c == nil {
		return fmt.Errorf("凭证不存在")
	}
	switch action {
	case "delete":
		return s.store.remove(name)
	case "enable", "disable":
		c.change(func(state object) { state["disabled"] = action == "disable" })
		if action == "enable" {
			clearCredential(c)
		}
	case "enable_credit", "disable_credit":
		c.change(func(state object) {
			state["enable_credit"] = action == "enable_credit"
			state["model_cooldowns"] = object{}
		})
	default:
		return fmt.Errorf("无效操作")
	}
	return nil
}
func (s *server) credentialStatus(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	offset, _ := strconv.Atoi(q.Get("offset"))
	limit, _ := strconv.Atoi(q.Get("limit"))
	if limit == 0 {
		limit = 50
	}
	if offset < 0 || limit < 1 || limit > 1000 {
		failure(w, 400, "无效的分页参数")
		return
	}
	stats := object{"total": 0, "normal": 0, "abnormal": 0, "claude_abnormal": 0, "gemini_abnormal": 0}
	items := []any{}
	for _, c := range s.store.accounts.Load().all {
		state := *c.state.Load()
		cool := object{}
		claude, gemini := false, false
		for model, value := range obj(state["model_cooldowns"]) {
			if number(value) > unixNow() {
				cool[model] = value
				claude = claude || strings.Contains(model, "claude")
				gemini = gemini || strings.Contains(model, "gemini")
			}
		}
		abnormal := truth(state["disabled"]) || len(list(state["error_codes"])) > 0 || len(cool) > 0
		unscoped := truth(state["disabled"]) || (len(list(state["error_codes"])) > 0 && !claude && !gemini)
		flags := map[string]bool{"normal": !abnormal, "abnormal": abnormal, "claude_abnormal": claude || unscoped, "gemini_abnormal": gemini || unscoped}
		stats["total"] = stats["total"].(int) + 1
		for k, v := range flags {
			if v {
				stats[k] = stats[k].(int) + 1
			}
		}
		if filter := q.Get("status_filter"); filter != "" && filter != "all" && !flags[filter] {
			continue
		}
		if tier := q.Get("tier_filter"); tier != "" && tier != "all" && tier != text(state["tier"]) {
			continue
		}
		match := true
		switch q.Get("cooldown_filter") {
		case "in_cooldown":
			match = len(cool) > 0
		case "no_cooldown":
			match = len(cool) == 0
		case "claude_cooldown":
			match = claude
		case "gemini_cooldown":
			match = gemini
		}
		if !match {
			continue
		}
		if code := q.Get("error_code_filter"); code != "" && code != "all" {
			found := false
			for _, v := range list(state["error_codes"]) {
				found = found || fmt.Sprint(v) == code
			}
			if !found {
				continue
			}
		}
		items = append(items, object{"filename": c.name, "user_email": state["user_email"], "disabled": state["disabled"], "error_codes": state["error_codes"], "last_success": float64(c.success.Load()) / 1000, "backend_type": "sqlite", "model_cooldowns": cool, "tier": state["tier"], "enable_credit": state["enable_credit"]})
	}
	total := len(items)
	start := min(offset, total)
	end := min(start+limit, total)
	jsonResponse(w, 200, object{"items": items[start:end], "total": total, "offset": offset, "limit": limit, "has_more": end < total, "stats": stats})
}
func validProxy(p string) error {
	if p == "" || p == "direct" {
		return nil
	}
	u, err := url.Parse(p)
	if err != nil || u.Hostname() == "" || u.Port() == "" || (u.Scheme != "http" && u.Scheme != "https" && u.Scheme != "socks5" && u.Scheme != "socks5h") {
		return fmt.Errorf("代理格式应为 http(s):// 或 socks5(h)://主机:端口")
	}
	return nil
}
func download(w http.ResponseWriter, typ, name string, data []byte) {
	w.Header().Set("Content-Type", typ)
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", name))
	_, _ = w.Write(data)
}
func writeUpstreamError(w http.ResponseWriter, err error) {
	status, body := errorStatus(redactError(err))
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}
func (s *server) upload(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 128<<20)
	if err := r.ParseMultipartForm(8 << 20); err != nil {
		failure(w, 400, "上传数据无效")
		return
	}
	defer r.MultipartForm.RemoveAll()
	created, updated := 0, 0
	failures := []string{}
	consume := func(name string, reader io.Reader) {
		raw, err := io.ReadAll(io.LimitReader(reader, 32<<20))
		if err != nil {
			failures = append(failures, name+": 读取失败")
			return
		}
		var data object
		if json.Unmarshal(raw, &data) != nil || data == nil {
			failures = append(failures, name+": JSON 格式无效")
			return
		}
		if text(data["refresh_token"]) == "" && text(data["access_token"]) == "" && text(data["token"]) == "" {
			failures = append(failures, name+": 缺少令牌")
			return
		}
		name = filepath.Base(name)
		existing := s.store.accounts.Load().byName[name] != nil
		if _, err = s.store.put(name, data); err != nil {
			failures = append(failures, name+": "+err.Error())
			return
		}
		if existing {
			updated++
		} else {
			created++
		}
	}
	for _, files := range r.MultipartForm.File {
		for _, header := range files {
			f, err := header.Open()
			if err != nil {
				continue
			}
			if strings.HasSuffix(strings.ToLower(header.Filename), ".zip") {
				data, err := io.ReadAll(io.LimitReader(f, 128<<20))
				f.Close()
				if err != nil {
					continue
				}
				z, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
				if err != nil {
					failures = append(failures, header.Filename+": ZIP 无效")
					continue
				}
				for _, entry := range z.File {
					if entry.FileInfo().IsDir() || !strings.HasSuffix(strings.ToLower(entry.Name), ".json") {
						continue
					}
					reader, err := entry.Open()
					if err == nil {
						consume(filepath.Base(entry.Name), reader)
						reader.Close()
					}
				}
			} else {
				consume(header.Filename, f)
				f.Close()
			}
		}
	}
	s.store.flush()
	jsonResponse(w, 200, object{"success_count": created + updated, "created_count": created, "updated_count": updated, "errors": failures, "message": fmt.Sprintf("新增 %d 个，更新 %d 个，失败 %d 个", created, updated, len(failures))})
}
func (s *server) downloadAll(w http.ResponseWriter, r *http.Request) {
	var buffer bytes.Buffer
	z := zip.NewWriter(&buffer)
	for _, c := range s.store.accounts.Load().all {
		file, err := z.Create(c.name)
		if err == nil {
			_, _ = file.Write(encoded(*c.data.Load()))
		}
	}
	if err := z.Close(); err != nil {
		failure(w, 500, err.Error())
		return
	}
	download(w, "application/zip", "antigravity-credentials.zip", buffer.Bytes())
}
func (s *server) modelList(w http.ResponseWriter, r *http.Request, gemini bool) {
	c := s.pick("", "", map[string]bool{})
	if c == nil {
		jsonResponse(w, 200, object{"object": "list", "data": []any{}, "models": []any{}})
		return
	}
	models, err := s.accountModels(r.Context(), c)
	if err != nil {
		writeUpstreamError(w, err)
		return
	}
	sort.Strings(models)
	result := []any{}
	for _, id := range models {
		for _, name := range []string{id, "抗截断/" + id} {
			if gemini {
				result = append(result, object{"name": "models/" + name, "displayName": name, "supportedGenerationMethods": []string{"generateContent", "streamGenerateContent", "countTokens"}})
			} else {
				result = append(result, object{"id": name, "object": "model", "created": 0, "owned_by": "google"})
			}
		}
	}
	if gemini {
		jsonResponse(w, 200, object{"models": result})
	} else {
		jsonResponse(w, 200, object{"object": "list", "data": result})
	}
}
func (s *server) countTokens(w http.ResponseWriter, r *http.Request, raw []byte, format translator.Format, model string) {
	if format == translator.FormatClaude {
		jsonResponse(w, 200, object{"input_tokens": estimateTokens(raw)})
		return
	}
	c := s.pick(model, "", map[string]bool{})
	if c == nil {
		failure(w, 503, "当前无可用凭证")
		return
	}
	_, payload, _, err := prepareInput(r.Context(), raw, format, model, [32]byte{}, false)
	if err != nil {
		failure(w, 400, err.Error())
		return
	}
	var body object
	_ = json.Unmarshal(payload, &body)
	body["project"] = (*c.data.Load())["project_id"]
	result, err := s.cloudCall(r.Context(), c, "countTokens", body)
	if err != nil {
		writeUpstreamError(w, err)
		return
	}
	jsonResponse(w, 200, result)
}
func (s *server) testCredential(w http.ResponseWriter, r *http.Request, c *credential) {
	a, err := s.engine(r.Context(), c)
	if err != nil {
		writeUpstreamError(w, err)
		return
	}
	defer a.active.Add(-1)
	payload := encoded(object{"request": object{"contents": []any{object{"role": "user", "parts": []any{object{"text": "hi"}}}}}})
	ctx, cancel := context.WithTimeout(r.Context(), 40*time.Second)
	defer cancel()
	err = a.engine.Stream(ctx, payload, "gemini-3.5-flash-lite", "", func([]byte) error { return nil })
	if err != nil {
		recordFailure(c, "gemini-3.5-flash-lite", err)
		status, raw := errorStatus(err)
		jsonResponse(w, status, object{"success": false, "status_code": status, "response": string(raw)})
		return
	}
	clearCredential(c)
	if r.URL.Query().Get("enable_on_success") == "true" {
		c.change(func(state object) { state["disabled"] = false })
	}
	c.success.Store(time.Now().UnixMilli())
	c.dirty.Store(true)
	s.store.flush()
	jsonResponse(w, 200, object{"success": true, "status_code": 200, "message": "凭证可用"})
}
