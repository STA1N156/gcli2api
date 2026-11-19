package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"gcli2api/agy-http/agywire/agent"
	"gcli2api/agy-http/agywire/transport"
)

const oauthClient = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
const oauthSecret = "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"

func accountID(data object) string {
	identity := text(data["refresh_token"])
	if identity == "" {
		identity = text(data["email"])
	}
	if identity == "" {
		identity = text(data["access_token"])
	}
	h := sha256.Sum256([]byte(identity))
	return hex.EncodeToString(h[:])
}
func (s *server) proxy(data object) string {
	p := text(data["proxy_url"])
	if p == "" {
		p = text(data["proxy-url"])
	}
	if p == "" {
		p = text(s.store.value("proxy"))
	}
	return p
}
func (s *server) httpCall(ctx context.Context, c *credential, method, endpoint string, body []byte, form bool) ([]byte, error) {
	data := *c.data.Load()
	tr, err := s.bridge.transport(accountID(data), s.proxy(data))
	if err != nil {
		return nil, err
	}
	mode := transport.BodyLength
	if body == nil {
		mode = transport.BodyNone
	}
	spec := transport.Spec{Method: method, URL: endpoint, Body: body, Mode: mode, UserAgent: transport.UserAgent}
	if form {
		spec.ContentType = "application/x-www-form-urlencoded"
	} else {
		spec.BearerToken = text(data["access_token"])
		if spec.BearerToken == "" {
			spec.BearerToken = text(data["token"])
		}
		if body != nil {
			spec.ContentType = "application/json"
		}
	}
	req, err := transport.Build(ctx, spec)
	if err != nil {
		return nil, err
	}
	resp, err := tr.RoundTrip(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != 200 {
		return nil, &agent.APIError{HTTPStatus: resp.StatusCode, Message: fmt.Sprintf("Google returned HTTP %d", resp.StatusCode), Raw: string(raw)}
	}
	return raw, nil
}
func (s *server) refreshToken(ctx context.Context, c *credential, force bool) error {
	needs := func(data object) bool {
		token := text(data["access_token"])
		if token == "" {
			token = text(data["token"])
		}
		if token == "" {
			return true
		}
		expiry := text(data["expiry"])
		if expiry == "" {
			expiry = text(data["expired"])
		}
		if expiry == "" {
			expiry = text(data["expires_at"])
		}
		if expiry != "" {
			if t, e := time.Parse(time.RFC3339Nano, expiry); e == nil {
				return time.Until(t) < 5*time.Minute
			}
		}
		return false
	}
	if !force && !needs(*c.data.Load()) {
		return nil
	}
	c.refresh.Lock()
	defer c.refresh.Unlock()
	data := *c.data.Load()
	if !force && !needs(data) {
		return nil
	}
	rt := text(data["refresh_token"])
	if rt == "" {
		return fmt.Errorf("凭证没有刷新令牌")
	}
	form := url.Values{"client_id": {oauthClient}, "client_secret": {oauthSecret}, "refresh_token": {rt}, "grant_type": {"refresh_token"}}
	endpoint := strings.TrimRight(text(s.store.value("oauth_proxy_url")), "/") + "/token"
	raw, err := s.httpCall(ctx, c, "POST", endpoint, []byte(form.Encode()), true)
	if err != nil {
		return err
	}
	var result object
	if err = json.Unmarshal(raw, &result); err != nil {
		return err
	}
	if text(result["access_token"]) == "" {
		return fmt.Errorf("刷新响应缺少 access_token")
	}
	next := clone(data)
	next["access_token"] = result["access_token"]
	if _, ok := next["token"]; ok {
		next["token"] = result["access_token"]
	}
	expiry := time.Now().Add(time.Duration(number(result["expires_in"])) * time.Second).UTC().Format(time.RFC3339Nano)
	next["expiry"] = expiry
	next["expired"] = expiry
	if text(result["refresh_token"]) != "" {
		next["refresh_token"] = result["refresh_token"]
	}
	c.data.Store(&next)
	c.dirty.Store(true)
	return nil
}
func (s *server) cloudCall(ctx context.Context, c *credential, method string, payload object) (object, error) {
	if err := s.refreshToken(ctx, c, false); err != nil {
		return nil, err
	}
	endpoint := strings.TrimRight(text(s.store.value("antigravity_api_url")), "/") + "/v1internal:" + method
	raw, err := s.httpCall(ctx, c, "POST", endpoint, encoded(payload), false)
	if err != nil {
		return nil, err
	}
	var out object
	err = json.Unmarshal(raw, &out)
	return out, err
}
func (s *server) engine(ctx context.Context, c *credential) (*wireAccount, error) {
	if err := s.refreshToken(ctx, c, false); err != nil {
		return nil, err
	}
	data := *c.data.Load()
	token := text(data["access_token"])
	if token == "" {
		token = text(data["token"])
	}
	r := &http.Request{Header: make(http.Header)}
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("X-Gcli-Account", accountID(data))
	r.Header.Set("X-Gcli-Proxy", s.proxy(data))
	target, err := url.Parse(text(s.store.value("antigravity_api_url")))
	if err != nil {
		return nil, err
	}
	if target.Scheme != "http" && target.Scheme != "https" {
		return nil, fmt.Errorf("无效的 Antigravity 地址")
	}
	tr, err := s.bridge.transport(accountID(data), s.proxy(data))
	if err != nil {
		return nil, err
	}
	return s.bridge.account(r, target, tr)
}
func (s *server) accountModels(ctx context.Context, c *credential) ([]string, error) {
	a, err := s.engine(ctx, c)
	if err != nil {
		return nil, err
	}
	defer a.active.Add(-1)
	cat, err := a.engine.Catalog(ctx)
	if err != nil {
		return nil, err
	}
	models := make([]string, 0, len(cat.Models))
	values := []any{}
	for id := range cat.Models {
		models = append(models, id)
		values = append(values, id)
	}
	c.change(func(state object) { state["supported_models"] = values })
	return models, nil
}
func (s *server) refreshInfo(ctx context.Context, c *credential) error {
	data, err := s.cloudCall(ctx, c, "loadCodeAssist", object{"metadata": object{"ideType": "ANTIGRAVITY"}})
	if err != nil {
		return err
	}
	project := data["cloudaicompanionProject"]
	if p, ok := project.(map[string]any); ok {
		project = p["id"]
	}
	if text(project) != "" {
		next := clone(*c.data.Load())
		next["project_id"] = project
		c.data.Store(&next)
		c.dirty.Store(true)
	}
	tier := text(obj(data["paidTier"])["id"])
	if tier == "" {
		tier = text(obj(data["currentTier"])["id"])
	}
	if tier != "" {
		t := "free"
		if strings.Contains(strings.ToLower(tier), "ultra") {
			t = "ultra"
		} else if strings.Contains(strings.ToLower(tier), "pro") || strings.Contains(tier, "standard") {
			t = "pro"
		}
		c.change(func(state object) { state["tier"] = t })
	}
	return nil
}
func (s *server) email(ctx context.Context, c *credential) (string, error) {
	if err := s.refreshToken(ctx, c, false); err != nil {
		return "", err
	}
	raw, err := s.httpCall(ctx, c, "GET", strings.TrimRight(text(s.store.value("googleapis_proxy_url")), "/")+"/oauth2/v2/userinfo", nil, false)
	if err != nil {
		return "", err
	}
	var data object
	if err = json.Unmarshal(raw, &data); err != nil {
		return "", err
	}
	email := text(data["email"])
	if email == "" {
		return "", fmt.Errorf("Google 没有返回邮箱")
	}
	next := clone(*c.data.Load())
	next["email"] = email
	c.data.Store(&next)
	c.change(func(state object) { state["user_email"] = email })
	return email, nil
}

func moveSystem(raw []byte) ([]byte, bool) {
	var payload object
	if json.Unmarshal(raw, &payload) != nil {
		return raw, false
	}
	inner := obj(payload["request"])
	parts := list(obj(inner["systemInstruction"])["parts"])
	if len(parts) == 0 {
		return raw, false
	}
	for _, v := range list(inner["contents"]) {
		content := obj(v)
		if content["role"] == "user" {
			content["parts"] = append(append(append([]any{}, parts...), object{"text": "\n\n"}), list(content["parts"])...)
			delete(inner, "systemInstruction")
			return encoded(payload), true
		}
	}
	return raw, false
}
func errorStatus(err error) (int, []byte) {
	var config *agent.ConfigurationError
	if errors.As(err, &config) {
		return http.StatusNotImplemented, encoded(object{"error": object{"code": http.StatusNotImplemented, "message": config.Error()}})
	}
	if errors.Is(err, agent.ErrAuth) {
		return 401, encoded(object{"error": object{"code": 401, "message": "Google authentication required"}})
	}
	if strings.HasPrefix(err.Error(), "unknown model:") {
		return 404, encoded(object{"error": object{"code": 404, "message": err.Error()}})
	}
	if api, ok := err.(*agent.APIError); ok {
		if api.Raw != "" {
			return api.HTTPStatus, []byte(api.Raw)
		}
		return api.HTTPStatus, encoded(object{"error": object{"code": api.HTTPStatus, "message": api.Message}})
	}
	return 502, encoded(object{"error": object{"code": 502, "message": err.Error()}})
}
func precise429(err error) bool {
	code, raw := errorStatus(err)
	if code != 429 {
		return false
	}
	var data object
	_ = json.Unmarshal(raw, &data)
	e := obj(data["error"])
	if e["message"] != "Resource has been exhausted (e.g. check quota)." || e["status"] != "RESOURCE_EXHAUSTED" {
		return false
	}
	for _, v := range list(e["details"]) {
		if text(obj(obj(v)["metadata"])["quotaResetTimeStamp"]) != "" {
			return false
		}
	}
	return true
}
func recordFailure(c *credential, model string, err error) {
	var config *agent.ConfigurationError
	if errors.As(err, &config) {
		return
	}
	if precise429(err) {
		return
	}
	code, raw := errorStatus(err)
	var parsed object
	_ = json.Unmarshal(raw, &parsed)
	c.change(func(state object) {
		codes := append([]any{}, list(state["error_codes"])...)
		found := false
		for _, v := range codes {
			if number(v) == float64(code) {
				found = true
			}
		}
		if !found {
			codes = append(codes, float64(code))
		}
		state["error_codes"] = codes
		messages := clone(obj(state["error_messages"]))
		messages[fmt.Sprint(code)] = object{"message": string(raw), "timestamp": unixNow(), "model": model}
		state["error_messages"] = messages
		seconds := map[int]float64{401: 86400, 402: 43200, 403: 43200, 404: 43200, 408: 60, 429: 5, 500: 5, 502: 60, 503: 5, 504: 60}[code]
		if seconds > 0 {
			until := unixNow() + seconds
			for _, v := range list(obj(parsed["error"])["details"]) {
				d := obj(v)
				if t, e := time.Parse(time.RFC3339Nano, text(obj(d["metadata"])["quotaResetTimeStamp"])); e == nil {
					until = float64(t.Unix())
				}
				if delay, e := time.ParseDuration(text(d["retryDelay"])); e == nil && until < unixNow()+delay.Seconds() {
					until = unixNow() + delay.Seconds()
				}
			}
			cd := clone(obj(state["model_cooldowns"]))
			cd[model] = until
			state["model_cooldowns"] = cd
		}
		if code == 404 {
			var models []any
			for _, v := range list(state["supported_models"]) {
				if modelKey(text(v)) != modelKey(model) {
					models = append(models, v)
				}
			}
			if len(models) > 0 {
				state["supported_models"] = models
			}
		}
	})
}

// Keep application errors free of the OAuth request, which contains secrets.
func redactError(err error) error {
	if api, ok := err.(*agent.APIError); ok {
		return api
	}
	if u, ok := err.(*url.Error); ok {
		return fmt.Errorf("Google connection failed: %v", u.Err)
	}
	return err
}
