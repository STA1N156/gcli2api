package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"gcli2api/agy-http/agywire/agent"
)

func TestGoBackendMigration(t *testing.T) {
	var mu sync.Mutex
	var captured []object
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body object
		_ = json.NewDecoder(r.Body).Decode(&body)
		if strings.HasSuffix(r.URL.Path, ":fetchAvailableModels") {
			jsonResponse(w, 200, object{"models": object{"gemini-wire-test": object{"model": "MODEL_TEST", "maxTokens": 100000, "maxOutputTokens": 8192}, "gemini-test-image": object{"model": "IMAGE_TEST", "maxTokens": 100000, "maxOutputTokens": 8192}}})
			return
		}
		if !strings.Contains(r.URL.Path, ":streamGenerateContent") {
			jsonResponse(w, 200, object{})
			return
		}
		parts := []any{object{"text": "plain reply", "thoughtSignature": "original_signature"}}
		if body["requestType"] == "chat" || body["requestType"] == "image_gen" {
			mu.Lock()
			captured = append(captured, body)
			mu.Unlock()
			if body["requestType"] == "image_gen" {
				parts = []any{object{"inlineData": object{"mimeType": "image/png", "data": "aGVsbG8="}}}
			} else {
				for _, tool := range list(obj(body["request"])["tools"]) {
					for _, decl := range list(obj(tool)["functionDeclarations"]) {
						name := text(obj(decl)["name"])
						if strings.HasSuffix(name, "_output_reply") {
							parts = []any{object{"functionCall": object{"id": "reply1", "name": name, "args": object{"content": "complete reply", "toolSummary": "Reply"}}, "thoughtSignature": "original_signature"}}
						}
						if strings.HasSuffix(name, "_lookup") {
							parts = []any{object{"functionCall": object{"id": "call1", "name": name, "args": object{"q": "one", "toolSummary": "Lookup one"}}, "thoughtSignature": "original_signature"}, object{"functionCall": object{"id": "call2", "name": name, "args": object{"q": "two", "toolSummary": "Lookup two"}}}}
						}
					}
				}
			}
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write(append(append([]byte("data: "), encoded(object{"response": object{"candidates": []any{object{"content": object{"role": "model", "parts": parts}, "finishReason": "STOP"}}, "usageMetadata": object{"promptTokenCount": 12, "candidatesTokenCount": 3, "totalTokenCount": 15}}})...), []byte("\n\n")...))
	}))
	defer upstream.Close()
	t.Setenv("CREDENTIALS_DIR", t.TempDir())
	t.Setenv("AGY_WIRE_STATE_DIR", t.TempDir())
	t.Setenv("PASSWORD", "test-password")
	t.Setenv("API_PASSWORD", "test-password")
	t.Setenv("PANEL_PASSWORD", "test-password")
	t.Setenv("ANTIGRAVITY_API_URL", upstream.URL)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store, err := openStore(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { cancel(); <-store.done; store.db.Close() }()
	_, err = store.put("test.json", object{"email": "local@example.test", "access_token": "local-token", "refresh_token": "local-refresh", "project_id": "aicode-consumers"})
	if err != nil {
		t.Fatal(err)
	}
	s := &server{store: store, bridge: &bridge{ctx: ctx}, ctx: ctx, logs: newLogBuffer()}
	api := httptest.NewServer(s)
	defer api.Close()
	send := func(path string, payload object, stream bool) []byte {
		t.Helper()
		req, _ := http.NewRequest("POST", api.URL+path, bytes.NewReader(encoded(payload)))
		req.Header.Set("Authorization", "Bearer test-password")
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Session-Id", "test-"+text(payload["model"]))
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		if resp.StatusCode != 200 {
			t.Fatalf("%s status=%d: %s", path, resp.StatusCode, body)
		}
		if !stream && !json.Valid(body) {
			t.Fatalf("invalid JSON: %s", body)
		}
		return body
	}
	for _, stream := range []bool{false, true} {
		body := send("/antigravity/v1/chat/completions", object{"model": "gemini-wire-test", "stream": stream, "messages": []any{object{"role": "user", "content": "hello"}}}, stream)
		if !bytes.Contains(body, []byte("plain reply")) {
			t.Fatalf("missing text: %s", body)
		}
		if stream && !bytes.Contains(body, []byte("[DONE]")) {
			t.Fatal("missing OpenAI terminator")
		}
	}
	body := send("/antigravity/v1/messages", object{"model": "gemini-wire-test", "stream": true, "max_tokens": 100, "messages": []any{object{"role": "user", "content": "claude request"}}}, true)
	if !bytes.Contains(body, []byte("message_stop")) {
		t.Fatalf("Claude stream did not finish: %s", body)
	}
	for turn := 0; turn < 2; turn++ {
		messages := []any{object{"role": "user", "content": "first question"}}
		if turn == 1 {
			messages = append(messages, object{"role": "assistant", "content": "complete reply"}, object{"role": "user", "content": "second question"})
		}
		body = send("/antigravity/v1/chat/completions", object{"model": "抗截断/gemini-wire-test", "messages": messages}, false)
		if !bytes.Contains(body, []byte("complete reply")) || bytes.Contains(body, []byte(`"name":"output_reply"`)) {
			t.Fatalf("anti-truncation reply leaked: %s", body)
		}
	}
	body = send("/antigravity/v1/chat/completions", object{"model": "gemini-wire-test", "messages": []any{object{"role": "user", "content": "two tools"}}, "tools": []any{object{"type": "function", "function": object{"name": "lookup", "parameters": object{"type": "object", "properties": object{"q": object{"type": "string"}}}}}}}, false)
	var toolResult object
	_ = json.Unmarshal(body, &toolResult)
	calls := list(obj(obj(first(toolResult["choices"]))["message"])["tool_calls"])
	if len(calls) != 2 {
		t.Fatalf("parallel calls lost: %s", body)
	}
	body = send("/antigravity/v1beta/models/gemini-test-image:generateContent", object{"contents": []any{object{"role": "user", "parts": []any{object{"text": "draw"}}}}}, false)
	if !bytes.Contains(body, []byte("inlineData")) {
		t.Fatalf("image missing: %s", body)
	}
	body = send("/config/save", object{"config": object{"session_affinity_ttl_seconds": 1234}}, false)
	if !bytes.Contains(body, []byte("message")) {
		t.Fatal(string(body))
	}
	store.flush()
	var count int
	if err := store.db.QueryRow("SELECT count(*) FROM antigravity_credentials WHERE filename='test.json'").Scan(&count); err != nil || count != 1 {
		t.Fatalf("SQLite persistence: count=%d error=%v", count, err)
	}
	mu.Lock()
	defer mu.Unlock()
	var cascades []string
	antiTurns := 0
	image := false
	for _, p := range captured {
		image = image || p["requestType"] == "image_gen"
		for _, tool := range list(obj(p["request"])["tools"]) {
			for _, decl := range list(obj(tool)["functionDeclarations"]) {
				if strings.HasSuffix(text(obj(decl)["name"]), "_output_reply") {
					cascades = append(cascades, text(obj(obj(p["request"])["labels"])["cascade_id"]))
					want := "first question"
					if antiTurns > 0 {
						want = "second question"
					}
					wrapped := false
					for _, content := range list(obj(p["request"])["contents"]) {
						for _, value := range list(obj(content)["parts"]) {
							part := obj(value)
							result := obj(part["functionResponse"])
							if result["name"] == "read_user_input" && strings.Contains(text(obj(result["response"])["output"]), want) {
								wrapped = true
							}
							if strings.Contains(text(part["text"]), want) {
								t.Fatal("anti-truncation input remained in a plain-text part")
							}
						}
					}
					if !wrapped {
						t.Fatalf("anti-truncation round %d: input not inside read_user_input", antiTurns+1)
					}
					t.Logf("anti-truncation round %d: input inside read_user_input; output_reply converted to reply text", antiTurns+1)
					antiTurns++
				}
			}
		}
	}
	if !image {
		t.Fatal("image did not use wire engine")
	}
	if len(cascades) != 2 || cascades[0] != cascades[1] {
		t.Fatalf("shared conversation changed: %v", cascades)
	}
}

func TestManagementSQLiteAndRefresh(t *testing.T) {
	google := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/token":
			jsonResponse(w, 200, object{"access_token": "fresh-local-token", "refresh_token": "local-refresh", "expires_in": 3600})
		case "/oauth2/v2/userinfo":
			jsonResponse(w, 200, object{"email": "local@example.test"})
		case "/v1internal:loadCodeAssist":
			jsonResponse(w, 200, object{"cloudaicompanionProject": "aicode-consumers", "paidTier": object{"id": "g1-pro-tier"}})
		case "/v1internal:retrieveUserQuotaSummary":
			jsonResponse(w, 200, object{"groups": []any{object{"buckets": []any{object{"bucketId": "gemini-5h", "remainingFraction": 0.4}, object{"bucketId": "gemini-weekly", "remainingFraction": 0.1}}}}})
		default:
			jsonResponse(w, 200, object{})
		}
	}))
	defer google.Close()
	t.Setenv("CREDENTIALS_DIR", t.TempDir())
	t.Setenv("PASSWORD", "management-test")
	t.Setenv("API_PASSWORD", "management-test")
	t.Setenv("PANEL_PASSWORD", "management-test")
	t.Setenv("OAUTH_PROXY_URL", google.URL)
	t.Setenv("GOOGLEAPIS_PROXY_URL", google.URL)
	t.Setenv("ANTIGRAVITY_API_URL", google.URL)
	ctx, cancel := context.WithCancel(context.Background())
	store, err := openStore(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { cancel(); <-store.done; store.db.Close() }()
	s := &server{store: store, bridge: &bridge{ctx: ctx}, ctx: ctx, logs: newLogBuffer()}
	request := func(method, path string, body []byte, contentType string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, bytes.NewReader(body))
		req.Header.Set("Authorization", "Bearer management-test")
		req.Header.Set("Content-Type", contentType)
		rec := httptest.NewRecorder()
		s.ServeHTTP(rec, req)
		if rec.Code != 200 {
			t.Fatalf("%s %d: %s", path, rec.Code, rec.Body.String())
		}
		return rec
	}
	request("POST", "/auth/login", encoded(object{"password": "management-test"}), "application/json")
	var upload bytes.Buffer
	form := multipart.NewWriter(&upload)
	file, _ := form.CreateFormFile("files", "local.json")
	_, _ = file.Write(encoded(object{"refresh_token": "local-refresh", "access_token": "expired-local-token", "expiry": "2020-01-01T00:00:00Z", "project_id": "aicode-consumers"}))
	_ = form.Close()
	request("POST", "/creds/upload", upload.Bytes(), form.FormDataContentType())
	request("PUT", "/creds/proxy/local.json", encoded(object{"proxy_url": "direct"}), "application/json")
	quota := request("GET", "/creds/quota/local.json", nil, "")
	if !strings.Contains(quota.Body.String(), `"remaining_percent":40`) {
		t.Fatal(quota.Body.String())
	}
	c := store.accounts.Load().byName["local.json"]
	if (*c.data.Load())["access_token"] != "fresh-local-token" {
		t.Fatal("token was not refreshed")
	}
	exact := &agent.APIError{HTTPStatus: 429, Raw: `{"error":{"status":"RESOURCE_EXHAUSTED","message":"Resource has been exhausted (e.g. check quota)."}}`}
	recordFailure(c, "gemini-wire-test", exact)
	if len(obj((*c.state.Load())["model_cooldowns"])) != 0 {
		t.Fatal("precise 429 was cooled down")
	}
	request("POST", "/creds/action", encoded(object{"filename": "local.json", "action": "disable"}), "application/json")
	if !truth((*c.state.Load())["disabled"]) {
		t.Fatal("disable was not applied")
	}
	s.oauth.state = "local-state"
	s.oauth.verifier = "local-verifier"
	s.oauth.redirect = "http://localhost/callback"
	s.oauth.expires = time.Now().Add(time.Minute)
	request("POST", "/auth/callback-url", encoded(object{"callback_url": "http://localhost/callback?state=local-state&code=local-code"}), "application/json")
	imported := store.accounts.Load().byName["local@example.test.json"]
	if imported == nil || (*imported.state.Load())["tier"] != "pro" {
		t.Fatal("OAuth account state was not saved")
	}
	var n int
	if err := store.db.QueryRow("SELECT count(*) FROM antigravity_credentials").Scan(&n); err != nil || n != 2 {
		t.Fatalf("persisted credentials: %d %v", n, err)
	}
}
