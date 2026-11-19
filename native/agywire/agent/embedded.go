package agent

import (
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync"

	"gcli2api/agy-http/agywire/ids"
	"gcli2api/agy-http/agywire/schema"
	wiresig "gcli2api/agy-http/agywire/signature"
	"gcli2api/agy-http/agywire/util"
)

//go:embed assets/default_workspace_hygiene.txt
var workspaceHygiene string

//go:embed assets/default_tool_guard.txt
var clientToolGuard string

//go:embed assets/default_no_tools_guard.txt
var noToolsGuard string

//go:embed assets/host_slash_commands_override.txt
var slashCommandsOverride string

//go:embed assets/client_system_close.txt
var clientSystemClose string

type Embedded struct {
	d         *Daemon
	callbacks sync.Map
	cancel    context.CancelFunc
}

func NewEmbedded(ctx context.Context, home string, client *http.Client, token func() (string, error)) (*Embedded, error) {
	if err := os.MkdirAll(home, 0700); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(ctx)
	e := &Embedded{cancel: cancel}
	e.d = &Daemon{ctx: ctx, home: home, http: client, tokens: &TokenSource{source: token}, sessions: map[string]*Session{}, mcpServers: map[string]int{}, wire: newWireStore(home)}
	e.d.httpOnce.Do(func() {})
	e.d.notify = func(route string, update map[string]any) {
		if callback, ok := e.callbacks.Load(route); ok {
			callback.(func(map[string]any))(update)
		}
	}
	e.d.startControlPlane(ctx)
	return e, nil
}

func (e *Embedded) Close() { e.cancel() }

func (e *Embedded) Stream(ctx context.Context, payload []byte, model, conversation string, emit func([]byte) error, keys ...[32]byte) error {
	if strings.Contains(model, "image") {
		return e.imageStream(ctx, payload, model, emit)
	}
	var key [32]byte
	if len(keys) > 0 {
		key = keys[0]
	}
	ctx, payload = wiresig.Prepare(ctx, payload, key)
	p, err := wireInput(payload, model, conversation, e.d.home, wiresig.Restored(ctx)...)
	if err != nil {
		return err
	}
	var emitErr error
	send := func(parts []any, finish string, usage any) {
		if emitErr != nil {
			return
		}
		candidate := map[string]any{"index": 0, "content": map[string]any{"role": "model", "parts": parts}}
		if finish != "" {
			candidate["finishReason"] = finish
		}
		response := map[string]any{"candidates": []any{candidate}, "modelVersion": model}
		if usage != nil {
			response["usageMetadata"] = usage
		}
		raw, marshalErr := json.Marshal(map[string]any{"response": response})
		if marshalErr != nil {
			emitErr = marshalErr
			return
		}
		emitErr = emit(raw)
	}
	e.callbacks.Store(p.RouteID, func(update map[string]any) {
		kind, _ := update["sessionUpdate"].(string)
		if kind != "agent_thought_chunk" && kind != "agent_message_chunk" {
			return
		}
		content, _ := update["content"].(map[string]any)
		text, _ := content["text"].(string)

		if text == "" || strings.HasPrefix(text, "\n⚠ API error (attempt ") {
			return
		}
		part := map[string]any{"text": text}
		if kind == "agent_thought_chunk" {
			part["thought"] = true
		}
		send([]any{part}, "", nil)
	})
	defer e.callbacks.Delete(p.RouteID)
	stop := context.AfterFunc(ctx, func() { e.d.wire.cancel(p.RouteID) })
	defer stop()
	raw, _ := json.Marshal(p)
	result, err := e.d.wireChatContext(ctx, raw)
	if err != nil {
		var wrapped *wireError
		if errors.As(err, &wrapped) {
			return wrapped.err
		}
		return err
	}
	if emitErr != nil {
		return emitErr
	}
	out := result.(map[string]any)
	var parts []any
	signature, _ := out["signature"].(string)
	meta, _ := out["callMeta"].(map[string]wireCallMeta)
	if signature != "" || len(meta) > 0 {
		signature = wiresig.Wrap(key, wiresig.Payload{Signature: signature, Calls: meta})
	}
	if signature != "" {
		parts = append(parts, map[string]any{"text": "", "thought": true, "thoughtSignature": signature})
	}
	if calls, ok := out["toolCalls"].([]wireToolCallOut); ok {
		for i, call := range calls {
			if original := p.originalNames[call.Name]; original != "" {
				call.Name = original
			}
			var args any
			_ = json.Unmarshal(call.Input, &args)
			part := map[string]any{"functionCall": map[string]any{"id": call.ID, "name": call.Name, "args": args}}
			if i == 0 && signature != "" {
				part["thoughtSignature"] = signature
			}
			parts = append(parts, part)
		}
	}
	finish := "STOP"
	if out["stopReason"] == "max_tokens" {
		finish = "MAX_TOKENS"
	}
	usage, _ := out["usage"].(map[string]any)
	input, _ := usage["inputTokens"].(int64)
	cached, _ := usage["cachedReadTokens"].(int64)
	output, _ := usage["outputTokens"].(int64)
	thought, _ := usage["thoughtTokens"].(int64)
	u := map[string]any{"promptTokenCount": input + cached, "candidatesTokenCount": max(int64(0), output-thought), "cachedContentTokenCount": cached, "thoughtsTokenCount": thought, "totalTokenCount": usage["totalTokens"]}
	send(parts, finish, u)
	return emitErr
}

func wireInput(payload []byte, model, conversation, home string, restored ...wiresig.History) (*wireChatParams, error) {
	var outer map[string]json.RawMessage
	if err := json.Unmarshal(payload, &outer); err != nil {
		return nil, err
	}
	inner := payload
	if v := outer["request"]; len(v) > 0 {
		inner = v
	}
	inputInTool := string(outer["_input_in_tool"]) == "true"
	if inputInTool {
		inner = normalizeWrappedInput(inner)
	}
	var input struct {
		ToolConfig json.RawMessage `json:"toolConfig"`
		Generation struct {
			ThinkingConfig json.RawMessage `json:"thinkingConfig"`
		} `json:"generationConfig"`
		Contents []struct {
			Role  string            `json:"role"`
			Parts []json.RawMessage `json:"parts"`
		} `json:"contents"`
		System struct {
			Parts []struct {
				Text string `json:"text"`
			} `json:"parts"`
		} `json:"systemInstruction"`
		Tools []struct {
			Declarations []struct {
				Name        string          `json:"name"`
				Description string          `json:"description"`
				Parameters  json.RawMessage `json:"parameters"`
				JSONSchema  json.RawMessage `json:"parametersJsonSchema"`
			} `json:"functionDeclarations"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(inner, &input); err != nil {
		return nil, err
	}
	if strings.HasPrefix(conversation, "ctx:") {
		conversation = ""
	}
	p := &wireChatParams{RouteID: ids.UUID(), Conv: conversation, Model: model, Cwd: home, originalNames: map[string]string{}}
	p.ToolConfig = input.ToolConfig
	p.ThinkingConfig = input.Generation.ThinkingConfig
	p.InputInTool = inputInTool
	p.AutoReply = string(outer["_reply_tool_owned"]) == "true"
	var system []string
	for _, part := range input.System.Parts {
		if text := strings.TrimSpace(util.StripAntigravityBillingHeader(part.Text)); text != "" {
			system = append(system, text)
		}
	}
	clientSystem := strings.Join(system, "\n\n")
	usedNames := map[string]bool{}
	toMCP := map[string]string{}
	for _, group := range input.Tools {
		for _, decl := range group.Declarations {
			if strings.TrimSpace(decl.Name) == "" {
				continue
			}
			name := safeToolName(decl.Name, usedNames)
			p.originalNames[name] = decl.Name
			toMCP[decl.Name] = name
			params := decl.Parameters
			if len(params) == 0 {
				params = decl.JSONSchema
			}
			var raw any
			_ = json.Unmarshal(params, &raw)
			params, _ = json.Marshal(cleanClientSchema(raw, true))
			p.Tools = append(p.Tools, schema.MCPTool{Name: name, Description: decl.Description, InputSchema: params})
		}
	}
	toolRaw, _ := json.Marshal(p.Tools)
	if len(p.ToolConfig) > 0 {
		var config map[string]any
		if json.Unmarshal(p.ToolConfig, &config) == nil {
			if f, ok := config["functionCallingConfig"].(map[string]any); ok {
				if names, ok := f["allowedFunctionNames"].([]any); ok {
					for i, value := range names {
						if name, ok := value.(string); ok && toMCP[name] != "" {
							names[i] = toMCP[name]
						}
					}
				}
			}
			p.ToolConfig, _ = json.Marshal(config)
		}
	}
	sum := sha256.Sum256(toolRaw)
	p.ToolsFP = hex.EncodeToString(sum[:])
	firstUser := true
	usedHistory := make([]bool, len(restored))
	for _, content := range input.Contents {
		t := wireTurn{Kind: "user"}
		if content.Role == "model" || content.Role == "assistant" {
			t.Kind = "assistant"
		}
		var texts, thoughts []string
		for _, raw := range content.Parts {
			var part struct {
				Text      string `json:"text"`
				Thought   bool   `json:"thought"`
				Signature string `json:"thoughtSignature"`
				Call      *struct {
					ID   string          `json:"id"`
					Name string          `json:"name"`
					Args json.RawMessage `json:"args"`
				} `json:"functionCall"`
				Result *struct {
					ID       string          `json:"id"`
					Name     string          `json:"name"`
					Response json.RawMessage `json:"response"`
				} `json:"functionResponse"`
				Inline *struct {
					MIME string `json:"mimeType"`
					Data string `json:"data"`
				} `json:"inlineData"`
			}
			if err := json.Unmarshal(raw, &part); err != nil {
				return nil, err
			}
			part.Text = util.StripAntigravityBillingHeader(part.Text)
			if part.Thought {
				thoughts = append(thoughts, part.Text)
			} else {
				texts = append(texts, part.Text)
			}
			if part.Signature != "" {
				t.Signature = part.Signature
			}
			if part.Inline != nil {
				t.Images = append(t.Images, InlineData{MimeType: part.Inline.MIME, Data: part.Inline.Data})
			}
			if part.Call != nil {
				if name := toMCP[part.Call.Name]; name != "" {
					part.Call.Name = name
				}
				t.ToolUses = append(t.ToolUses, wireToolUse{ID: part.Call.ID, Name: part.Call.Name, Input: part.Call.Args})
			}
			if part.Result != nil {
				t.Kind = "results"
				id := part.Result.ID
				if id == "" {
					id = part.Result.Name
				}
				text, isError := wireResultText(part.Result.Response)
				t.Results = append(t.Results, wireResult{ID: id, IsError: isError, Content: []wireContentItem{{Type: "text", Text: text}}})
			}
		}
		t.Text = strings.Join(texts, "")
		t.Thinking = strings.Join(thoughts, "")
		if t.Kind == "assistant" {
			for index, history := range restored {
				if usedHistory[index] {
					continue
				}
				matched := history.Signature != "" && t.Signature == history.Signature || history.Thinking != "" && t.Thinking == history.Thinking || history.Text != "" && t.Text == history.Text
				for _, call := range t.ToolUses {
					if _, ok := history.Calls[call.ID]; ok {
						matched = true
						break
					}
				}
				if matched {
					usedHistory[index] = true
					t.Signature = history.Signature
					if t.Thinking == "" {
						t.Thinking = history.Thinking
					}
					if t.CallMeta == nil {
						t.CallMeta = map[string]wireCallMeta{}
					}
					for id, meta := range history.Calls {
						t.CallMeta[id] = meta
					}
					break
				}
			}
		}
		if t.Kind == "user" && strings.TrimSpace(t.Text) == "" {
			t.Text = "(empty)"
			if len(t.Images) > 0 {
				t.Text = "(请查看所附图片)"
			}
		}
		if t.Kind == "user" && firstUser {
			if clientSystem != "" {
				t.Text = "<client_system_instructions>\n" + clientSystem + clientSystemClose + t.Text
			}
			firstUser = false
		}
		if n := len(p.Turns); n > 0 && p.Turns[n-1].Kind == t.Kind {
			prior := &p.Turns[n-1]
			if prior.Text != "" && t.Text != "" {
				prior.Text += "\n\n"
			}
			prior.Text += t.Text
			prior.Thinking += t.Thinking
			prior.Images = append(prior.Images, t.Images...)
			prior.Results = append(prior.Results, t.Results...)
			prior.ToolUses = append(prior.ToolUses, t.ToolUses...)
			if len(t.CallMeta) > 0 {
				if prior.CallMeta == nil {
					prior.CallMeta = map[string]wireCallMeta{}
				}
				for id, meta := range t.CallMeta {
					prior.CallMeta[id] = meta
				}
			}
			if t.Signature != "" {
				prior.Signature = t.Signature
			}
			prior.FP = ""
			raw, _ := json.Marshal(prior)
			sum := sha256.Sum256(raw)
			prior.FP = hex.EncodeToString(sum[:])
		} else {
			raw, _ := json.Marshal(t)
			fingerprint := sha256.Sum256(raw)
			t.FP = hex.EncodeToString(fingerprint[:])
			p.Turns = append(p.Turns, t)
		}
	}
	if len(p.Turns) >= 2 {
		last := p.Turns[len(p.Turns)-1]
		previous := &p.Turns[len(p.Turns)-2]
		if last.Kind == "assistant" && previous.Kind == "user" {
			previous.Text += "\n\n[Assistant prefill]: " + last.Text
			raw, _ := json.Marshal(previous)
			sum := sha256.Sum256(raw)
			previous.FP = hex.EncodeToString(sum[:])
			p.Turns = p.Turns[:len(p.Turns)-1]
		}
	}
	if len(p.Turns) == 0 {
		return nil, fmt.Errorf("antigravity: empty conversation")
	}
	if p.Conv == "" {
		sum := sha256.Sum256([]byte(p.Append + "\x1e" + p.Turns[0].FP))
		p.Conv = hex.EncodeToString(sum[:])
	}
	guard := clientToolGuard
	if len(p.Tools) == 0 {
		guard = noToolsGuard
		p.DenyTools = true
		p.DenyReply = "The caller did not provide this tool. Answer directly."
	}
	p.Append = workspaceHygiene + "\n\n" + slashCommandsOverride + "\n\n" + guard
	return p, nil
}

func wireResultText(raw json.RawMessage) (string, bool) {
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return string(raw), false
	}
	isError := false
	if object, ok := value.(map[string]any); ok {
		for _, key := range []string{"error", "result", "output", "content"} {
			if v, exists := object[key]; exists {
				value = v
				isError = key == "error"
				break
			}
		}
	}
	if text, ok := value.(string); ok {
		return text, isError
	}
	text, _ := json.Marshal(value)
	return string(text), isError
}
