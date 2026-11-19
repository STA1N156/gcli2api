package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"

	"gcli2api/agy-http/agywire/util"
)

func sessionText(value any, separator string) string {
	if s, ok := value.(string); ok {
		return s
	}
	var parts []string
	for _, value := range list(value) {
		part := obj(value)
		if !truth(part["thought"]) && (part["type"] == nil || part["type"] == "text") {
			if s, ok := part["text"].(string); ok {
				parts = append(parts, s)
			}
		}
	}
	return strings.Join(parts, separator)
}

func sessionKey(raw object, header http.Header) string {
	for _, name := range []string{"X-Session-Id", "X-Conversation-Id", "X-Chat-Id"} {
		if value := strings.TrimSpace(header.Get(name)); value != "" {
			return value
		}
	}
	for _, name := range []string{"cache_session_key", "session_id", "prompt_cache_key"} {
		if value := strings.TrimSpace(text(raw[name])); value != "" {
			return value
		}
	}
	metadata := obj(raw["metadata"])
	for _, name := range []string{"session_id", "conversation_id"} {
		if value := strings.TrimSpace(text(metadata[name])); value != "" {
			return "agy-meta:" + value
		}
	}
	if _, value, ok := strings.Cut(text(metadata["user_id"]), "_session_"); ok && strings.TrimSpace(value) != "" {
		return "agy-cc-sess:" + strings.TrimSpace(value)
	}
	if value := strings.TrimSpace(text(raw["conversation_id"])); value != "" {
		return "agy-cid:" + value
	}
	clean := func(s string) string { return strings.TrimSpace(util.StripAntigravityBillingHeader(s)) }
	var system []string
	if value := clean(sessionText(raw["system"], "\n\n")); value != "" {
		system = append(system, value)
	}
	for _, name := range []string{"systemInstruction", "system_instruction"} {
		if value := clean(sessionText(obj(raw[name])["parts"], "\n\n")); value != "" {
			system = append(system, value)
		}
	}
	firstUser := ""
	foundUser := false
	for _, name := range []string{"messages", "contents"} {
		for _, value := range list(raw[name]) {
			message := obj(value)
			content := message["content"]
			if name == "contents" {
				content = message["parts"]
			}
			if role := text(message["role"]); role == "system" || role == "developer" {
				if value := clean(sessionText(content, "\n\n")); value != "" {
					system = append(system, value)
				}
			} else if !foundUser && (role == "user" || role == "") {
				firstUser = clean(sessionText(content, "\n"))
				foundUser = true
			}
		}
	}
	systemText := strings.Join(system, "\n\n")
	if systemText == "" && firstUser == "" {
		return ""
	}
	hash := sha256.New()
	hash.Write([]byte(systemText))
	hash.Write([]byte{0})
	hash.Write([]byte(firstUser))
	return "agy-conv:" + hex.EncodeToString(hash.Sum(nil)[:12])
}
