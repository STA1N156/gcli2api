package signature

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
)

type historyKey struct{}

type History struct {
	Payload
	Thinking string
	Text     string
}

func Restored(ctx context.Context) []History {
	history, _ := ctx.Value(historyKey{}).([]History)
	return history
}

// Prepare verifies the account envelope before existing protocol translators run.
// Tool arguments and schemas are opaque user data and must never be rewritten.
func Prepare(ctx context.Context, raw []byte, key [32]byte) (context.Context, []byte) {
	if !bytes.Contains(raw, []byte(Prefix)) {
		return ctx, raw
	}
	var root any
	if json.Unmarshal(raw, &root) != nil {
		return ctx, raw
	}
	var restored []History
	seen := map[string]bool{}
	var walk func(any, string)
	walk = func(node any, text string) {
		switch v := node.(type) {
		case []any:
			for _, child := range v {
				walk(child, text)
			}
		case map[string]any:
			if v["type"] == "tool_result" {
				return
			}
			if v["role"] == "assistant" || v["role"] == "model" {
				var blocks []any
				if b, ok := v["content"].([]any); ok {
					blocks = b
				}
				if b, ok := v["parts"].([]any); ok {
					blocks = b
				}
				text = ""
				if s, ok := v["content"].(string); ok {
					text = s
				}
				for _, block := range blocks {
					if b, ok := block.(map[string]any); ok && b["thought"] != true && b["type"] != "thinking" {
						if s, ok := b["text"].(string); ok {
							text += s
						}
					}
				}
			}
			for k, child := range v {
				switch k {
				case "signature", "thoughtSignature", "thought_signature", "reasoning_signature", "encrypted_content":
					value, ok := child.(string)
					if !ok || !strings.HasPrefix(value, Prefix) {
						continue
					}
					p, valid := Unwrap(key, value)
					v[k] = ""
					if valid {
						v[k] = p.Signature
						thought, _ := v["thinking"].(string)
						if thought == "" {
							thought, _ = v["reasoning_content"].(string)
						}
						if thought == "" {
							thought, _ = v["text"].(string)
						}
						if !seen[value] {
							restored = append(restored, History{Payload: p, Thinking: thought, Text: text})
							seen[value] = true
						}
					}
				case "args", "arguments", "parameters", "parametersJsonSchema", "input_schema", "tools", "functionResponse":
					continue
				case "input":
					if v["type"] == "tool_use" {
						continue
					}
					walk(child, text)
				default:
					walk(child, text)
				}
			}
		}
	}
	walk(root, "")
	encoded, err := json.Marshal(root)
	if err != nil {
		return ctx, raw
	}
	if len(restored) > 0 {
		ctx = context.WithValue(ctx, historyKey{}, restored)
	}
	return ctx, encoded
}
