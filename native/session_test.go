package main

import (
	"encoding/json"
	"net/http"
	"testing"

	"gcli2api/agy-http/agywire/agent"
)

func TestThoughtsAlwaysIncluded(t *testing.T) {
	for _, config := range []string{"", `{"thinkingLevel":"LOW"}`, `{"thinkingLevel":"MEDIUM","includeThoughts":false}`, `{"thinkingBudget":1024}`} {
		raw, err := agent.RenderAgent(agent.AgentRequest{Model: agent.ModelInfo{ID: "gemini-test", ThinkingLevel: 2}, ThinkingConfig: json.RawMessage(config)})
		if err != nil {
			t.Fatal(err)
		}
		var body object
		if err = json.Unmarshal(raw, &body); err != nil {
			t.Fatal(err)
		}
		thinking := obj(obj(obj(body["request"])["generationConfig"])["thinkingConfig"])
		if thinking["includeThoughts"] != true {
			t.Fatalf("config %s: %s", config, raw)
		}
		if config != "" {
			var supplied object
			_ = json.Unmarshal([]byte(config), &supplied)
			for _, key := range []string{"thinkingLevel", "thinkingBudget"} {
				if expected, ok := supplied[key]; ok && thinking[key] != expected {
					t.Fatalf("%s was changed: %v", key, thinking)
				}
			}
		}
	}
}

func TestSessionKeyReference(t *testing.T) {
	header := make(http.Header)
	if got := sessionKey(object{"metadata": object{"user_id": "user_device_account_example_session_abc-123"}}, header); got != "agy-cc-sess:abc-123" {
		t.Fatal(got)
	}
	if got := sessionKey(object{"metadata": object{"conversation_id": "thread-1", "user_id": "x_session_other"}}, header); got != "agy-meta:thread-1" {
		t.Fatal(got)
	}
	base := object{"system": "system A", "messages": []any{object{"role": "user", "content": "hello"}}}
	key := sessionKey(base, header)
	changed := clone(base)
	changed["system"] = "system B"
	if sessionKey(changed, header) == key {
		t.Fatal("different system prompts shared a fallback key")
	}
	changed = clone(base)
	changed["system"] = "x-anthropic-billing-header: changing\nsystem A"
	if sessionKey(changed, header) != key {
		t.Fatal("billing header changed the fallback key")
	}
	changed = clone(base)
	changed["messages"] = []any{object{"role": "user", "content": "hello"}, object{"role": "assistant", "content": "answer"}, object{"role": "user", "content": "next turn"}}
	if sessionKey(changed, header) != key {
		t.Fatal("later turns changed the fallback key")
	}
	openai := object{"messages": []any{object{"role": "system", "content": "system A"}, object{"role": "user", "content": "hello"}}}
	gemini := object{"systemInstruction": object{"parts": []any{object{"text": "system A"}}}, "contents": []any{object{"role": "user", "parts": []any{object{"text": "hello"}}}}}
	if sessionKey(openai, header) != key || sessionKey(gemini, header) != key {
		t.Fatal("equivalent API formats produced different fallback keys")
	}
	header.Set("X-Session-Id", "explicit")
	if got := sessionKey(base, header); got != "explicit" {
		t.Fatal(got)
	}
}
