package agent

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"gcli2api/agy-http/agywire/schema"
)

func forcedClaudeTools(model string, raw json.RawMessage) bool {
	if !strings.Contains(model, "claude") {
		return false
	}
	var config struct {
		Function struct {
			Mode string `json:"mode"`
		} `json:"functionCallingConfig"`
	}
	_ = json.Unmarshal(raw, &config)
	return config.Function.Mode == "ANY"
}

// Retain caller-owned tool choice while translating names to registered MCP names.
func wireToolConfig(raw json.RawMessage, server string, tools []schema.MCPTool) json.RawMessage {
	if len(raw) == 0 {
		return nil
	}
	var config map[string]any
	if json.Unmarshal(raw, &config) != nil {
		return nil
	}
	f, ok := config["functionCallingConfig"].(map[string]any)
	if !ok {
		return raw
	}
	if names, ok := f["allowedFunctionNames"].([]any); ok {
		for i, name := range names {
			if s, ok := name.(string); ok {
				f["allowedFunctionNames"].([]any)[i] = schema.DeclName(server, s)
			}
		}
	} else if f["mode"] == "ANY" {
		names := make([]string, 0, len(tools))
		for _, t := range tools {
			names = append(names, schema.DeclName(server, t.Name))
		}
		f["allowedFunctionNames"] = names
	}
	out, _ := json.Marshal(config)
	return out
}

// Canonicalize the hidden input wrapper for stable multi-turn fingerprints.
// The native history reinstates this wrapper in wrapWireUserInput.
func normalizeWrappedInput(raw []byte) []byte {
	var root map[string]any
	if json.Unmarshal(raw, &root) != nil {
		return raw
	}
	contents, ok := root["contents"].([]any)
	if !ok {
		return raw
	}
	var out []any
	for i := 0; i < len(contents); i++ {
		current, ok := contents[i].(map[string]any)
		if !ok {
			out = append(out, contents[i])
			continue
		}
		parts, _ := current["parts"].([]any)
		if current["role"] == "user" && i+2 < len(contents) {
			assistant, _ := contents[i+1].(map[string]any)
			result, _ := contents[i+2].(map[string]any)
			calls, _ := assistant["parts"].([]any)
			replies, _ := result["parts"].([]any)
			if len(calls) == 1 && len(replies) == 1 {
				cp, _ := calls[0].(map[string]any)
				rp, _ := replies[0].(map[string]any)
				call, _ := cp["functionCall"].(map[string]any)
				reply, _ := rp["functionResponse"].(map[string]any)
				data, _ := reply["response"].(map[string]any)
				if text, ok := data["output"].(string); ok {
					var nested map[string]any
					if json.Unmarshal([]byte(text), &nested) == nil {
						data = nested
					}
				}
				if call["name"] == "read_user_input" && call["id"] == reply["id"] && data["source"] == "user_input" {
					var restored []any
					for _, p := range parts {
						part, _ := p.(map[string]any)
						if part["text"] == "读取 read_user_input 的返回内容，作为本轮用户输入继续回复。" {
							restored = append(restored, map[string]any{"text": data["text"]})
						} else {
							restored = append(restored, p)
						}
					}
					current["parts"] = restored
					i += 2
				}
			}
		}
		if ps, ok := current["parts"].([]any); ok {
			clean := make([]any, 0, len(ps))
			for _, part := range ps {
				p, _ := part.(map[string]any)
				text, _ := p["text"].(string)
				if !strings.HasPrefix(text, "（本次回复的全部内容必须通过 output_reply 工具的 content 字段提交，") {
					clean = append(clean, part)
				}
			}
			current["parts"] = clean
		}
		out = append(out, current)
	}
	root["contents"] = out
	outRaw, _ := json.Marshal(root)
	return outRaw
}

func (d *Daemon) wrapWireUserInput(cv *wireConv) {
	last := len(cv.Contents) - 1
	if last < 0 {
		return
	}
	user := cv.Contents[last]
	if len(user.Parts) == 0 || user.Parts[0].Text == nil {
		return
	}
	text := *user.Parts[0].Text
	call := FunctionCall{ID: randCallID(), Name: "read_user_input", Args: json.RawMessage(`{}`)}
	cv.Contents[last].Parts = append([]Part{{Text: strp("Read the read_user_input result as the user's request.")}}, user.Parts[1:]...)
	cv.Contents = append(cv.Contents,
		Content{Role: "model", Parts: []Part{{Call: &call, Signature: "skip_thought_signature_validator"}}},
		Content{Role: "user", Parts: []Part{{Resp: &FunctionResponse{ID: call.ID, Name: call.Name, Output: &text}}}},
	)
	cv.wireRecord(trajStep{Kind: trajModel, Calls: []FunctionCall{call}})
	cv.Steps++
	cv.wireRecord(trajStep{Kind: trajEnv, Call: call, Output: &text})
	cv.Steps++
}

// A reply tool is consumed by the response adapter, rather than executed
// by the client. Acknowledge it so the next user turn can reuse the conversation.
func (d *Daemon) acknowledgeReply(ctx context.Context, cv *wireConv, p *wireChatParams, model ModelInfo, out map[string]any) {
	if !p.AutoReply {
		return
	}
	calls, ok := out["toolCalls"].([]wireToolCallOut)
	if !ok || len(calls) != 1 || calls[0].Name != "output_reply" {
		return
	}
	var args struct {
		Content string `json:"content"`
	}
	if json.Unmarshal(calls[0].Input, &args) != nil || strings.TrimSpace(args.Content) == "" {
		return
	}
	d.wireResolvePending(ctx, cv, model, map[string]wireResult{calls[0].ID: {ID: calls[0].ID, Content: []wireContentItem{{Type: "text", Text: "Displayed to the user."}}}})
	cv.LastStop = "end_turn"
	cv.LastToolUseIDs = nil
	cv.Updated = time.Now()
}
