package agent

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"gcli2api/agy-http/agywire/ids"
	"gcli2api/agy-http/agywire/schema"
	wiresig "gcli2api/agy-http/agywire/signature"
)

type wireContentItem struct {
	Type     string `json:"type"`
	Text     string `json:"text,omitempty"`
	Data     string `json:"data,omitempty"`
	MimeType string `json:"mimeType,omitempty"`
}

type wireToolUse struct {
	ID    string          `json:"id"`
	Name  string          `json:"name"`
	Input json.RawMessage `json:"input"`
}

type wireResult struct {
	ID      string            `json:"id"`
	Content []wireContentItem `json:"content"`
	IsError bool              `json:"isError"`
}

type wireCallMeta = wiresig.CallMeta

type wireTurn struct {
	Kind      string                  `json:"kind"`
	FP        string                  `json:"fp"`
	Text      string                  `json:"text"`
	Images    []InlineData            `json:"images"`
	Thinking  string                  `json:"thinking"`
	Signature string                  `json:"signature"`
	CallMeta  map[string]wireCallMeta `json:"callMeta"`
	ToolUses  []wireToolUse           `json:"toolUses"`
	Results   []wireResult            `json:"results"`
}

type wireChatParams struct {
	ToolConfig     json.RawMessage `json:"toolConfig,omitempty"`
	ThinkingConfig json.RawMessage `json:"thinkingConfig,omitempty"`
	InputInTool    bool            `json:"inputInTool,omitempty"`
	AutoReply      bool            `json:"autoReply,omitempty"`
	originalNames  map[string]string
	RouteID        string           `json:"routeId"`
	Conv           string           `json:"conv"`
	Model          string           `json:"model"`
	Cwd            string           `json:"cwd"`
	Append         string           `json:"append"`
	Tools          []schema.MCPTool `json:"tools"`
	ToolsFP        string           `json:"toolsFp"`
	Turns          []wireTurn       `json:"turns"`
	DenyTools      bool             `json:"denyTools"`
	DenyReply      string           `json:"denyReply"`
}

type wireToolCallOut struct {
	ID      string          `json:"id"`
	Name    string          `json:"name"`
	Input   json.RawMessage `json:"input"`
	Builtin bool            `json:"builtin"`
}

const wireMaxDeniedRounds = 6

type wireError struct {
	err      error
	attempts int
}

func (e *wireError) Error() string {
	return fmt.Sprintf("%s [attempts=%d]", e.err.Error(), e.attempts)
}

var wireQueueWait = 10 * time.Second

func (d *Daemon) wireChat(raw json.RawMessage) (any, error) {
	return d.wireChatContext(d.ctx, raw)
}

func (d *Daemon) wireChatContext(parent context.Context, raw json.RawMessage) (any, error) {
	var p wireChatParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, err
	}
	if p.Conv == "" || p.RouteID == "" || len(p.Turns) == 0 {
		return nil, errors.New("_wire/chat 参数不全")
	}

	ctx, cancel := context.WithCancel(parent)
	d.wire.setCancel(p.RouteID, cancel)
	defer func() { cancel(); d.wire.setCancel(p.RouteID, nil) }()

	cat, err := d.Catalog(ctx)
	if err != nil {
		if ctx.Err() != nil {
			return nil, &wireError{err: context.Canceled, attempts: 1}
		}
		return nil, err
	}
	d.touchCatalog()
	model, ok := resolveClientModel(cat, &p)
	if !ok {
		return nil, fmt.Errorf("unknown model: %s", p.Model)
	}
	if !Supported(model) {
		return nil, unsupportedError(p.Model, model)
	}

	n := d.wire.begin(p.Conv)
	waitStart := time.Now()
	acqCtx, acqCancel := context.WithTimeout(ctx, wireQueueWait)
	release, err := d.wire.acquire(acqCtx, p.Conv)
	acqCancel()
	detached := false
	if err != nil {
		if ctx.Err() != nil {
			log.Printf("协议模式对话: 排队等同会话上一轮时客户端已取消,放弃 waited_ms=%d", time.Since(waitStart).Milliseconds())
			return nil, &wireError{err: context.Canceled, attempts: 1}
		}
		detached = true
	} else {
		defer release()
		if w := time.Since(waitStart); w > time.Second {
			log.Printf("协议模式对话: 等同会话上一轮 waited_ms=%d", w.Milliseconds())
		}
	}

	var cv *wireConv
	mode := "continue"
	why := ""
	if detached {
		mode, why = "detached", fmt.Sprintf("queue_busy(waited_ms=%d)", time.Since(waitStart).Milliseconds())
		cv = d.wireRebuild(&p, model)
	} else {
		if st := d.wire.load(p.Conv); st != nil {
			if why = wireContinueBlocker(st, &p); why == "" {
				resolveWireResultIDs(st, &p)
				cv = st.clone()
			}
		}
		if cv != nil {
			d.markMCPRegistered(cv.MCPServer)
			if err := d.wireApplyContinue(ctx, cv, &p, model); err != nil {
				return nil, err
			}
		} else {
			mode = "rebuild"
			if d.wire.load(p.Conv) == nil {
				mode = "new"
			}
			cv = d.wireRebuild(&p, model)
		}
	}
	log.Printf("协议模式对话: mode=%s turns=%d steps=%d why=%s", mode, len(p.Turns), cv.Steps, why)

	res, err := d.wireLoop(ctx, cv, &p, model)
	if err != nil {
		return nil, err
	}
	d.acknowledgeReply(ctx, cv, &p, model, res)
	cv.TurnFPs = make([]string, len(p.Turns))
	for i, t := range p.Turns {
		cv.TurnFPs[i] = t.FP
	}
	if !d.wire.saveIfNewest(cv, n) {
		log.Printf("协议模式对话: 同会话更晚的一轮已落盘,本轮结果不覆盖 mode=%s", mode)
	}
	res["mode"] = mode
	return res, nil
}

// Resolve the new result tail only, after acquiring the conversation lock.
func resolveWireResultIDs(st *wireConv, p *wireChatParams) {
	used := make(map[string]bool, len(st.Pending))
	for i := len(st.TurnFPs) + 1; i < len(p.Turns); i++ {
		for j := range p.Turns[i].Results {
			r := &p.Turns[i].Results[j]
			if used[r.ID] {
				continue
			}
			for _, pending := range st.Pending {
				if !used[pending.ToolUseID] && r.ID == pending.ToolUseID {
					used[pending.ToolUseID] = true
					break
				}
			}
		}
	}
	for i := len(st.TurnFPs) + 1; i < len(p.Turns); i++ {
		for j := range p.Turns[i].Results {
			r := &p.Turns[i].Results[j]
			if used[r.ID] {
				continue
			}
			for _, pending := range st.Pending {
				if used[pending.ToolUseID] {
					continue
				}
				if r.ID == pending.Call.Name || strings.HasSuffix(pending.Call.Name, "_"+r.ID) {
					r.ID = pending.ToolUseID
					used[pending.ToolUseID] = true
					break
				}
			}
		}
	}
}

func wireCanContinue(st *wireConv, p *wireChatParams) bool {
	return wireContinueBlocker(st, p) == ""
}

func wireContinueBlocker(st *wireConv, p *wireChatParams) string {
	if st.ToolsFP != p.ToolsFP {
		return "tools_changed"
	}
	if st.Cwd != p.Cwd {
		return "cwd_changed"
	}
	m, n := len(st.TurnFPs), len(p.Turns)
	if m == 0 {
		return "empty_record"
	}
	if n < m+2 {
		return fmt.Sprintf("history_shorter(record=%d,req=%d)", m, n)
	}
	for i := 0; i < m; i++ {
		if p.Turns[i].FP != st.TurnFPs[i] {
			return fmt.Sprintf("turn_changed(at=%d/%d,kind=%s)", i, m, p.Turns[i].Kind)
		}
	}
	a := p.Turns[m]
	if a.Kind != "assistant" {
		return "no_assistant_after_record(kind=" + a.Kind + ")"
	}
	if st.LastStop == "tool_use" {
		if len(a.ToolUses) != len(st.LastToolUseIDs) {
			return fmt.Sprintf("tool_use_count(ours=%d,client=%d)", len(st.LastToolUseIDs), len(a.ToolUses))
		}
		for i, tu := range a.ToolUses {
			if tu.ID != st.LastToolUseIDs[i] {
				return "tool_use_id_mismatch"
			}
		}
	} else if len(a.ToolUses) > 0 {
		return "unexpected_tool_use"
	}
	for _, t := range p.Turns[m+1:] {
		if t.Kind == "assistant" {
			return "extra_assistant_turn"
		}
		if t.Kind == "results" && len(st.Pending) == 0 {
			return "results_without_pending"
		}
	}
	return ""
}

func (d *Daemon) markMCPRegistered(server string) {
	if server == "" {
		return
	}
	d.mu.Lock()
	if d.mcpServers["c"] == 0 {
		d.mcpServers["c"] = 1
	}
	d.mu.Unlock()
}

func (d *Daemon) wireApplyContinue(ctx context.Context, cv *wireConv, p *wireChatParams, model ModelInfo) error {
	cv.InputInTool = p.InputInTool
	tail := p.Turns[len(cv.TurnFPs)+1:]
	results := map[string]wireResult{}
	var userText []string
	var images []InlineData
	for _, t := range tail {
		switch t.Kind {
		case "results":
			for _, r := range t.Results {
				results[r.ID] = r
			}
		case "user":
			if strings.TrimSpace(t.Text) != "" {
				userText = append(userText, t.Text)
			}
			images = append(images, t.Images...)
		}
	}
	if len(cv.Pending) > 0 {
		d.wireResolvePending(ctx, cv, model, results)
	}
	if len(userText) > 0 || len(images) > 0 {
		d.wireUserStep(cv, strings.Join(userText, "\n\n"), images, model, p.RouteID)
	}
	return nil
}

func (d *Daemon) wireResolvePending(ctx context.Context, cv *wireConv, model ModelInfo, results map[string]wireResult) {
	now := time.Now()
	var parts []Part
	for i, pc := range cv.Pending {
		if !pc.Monitored {
			d.runPreToolMonitors(ctx, cv.Traj, model, cv.PendingStep, i, pc.Call)
		}
		fr := &FunctionResponse{ID: pc.Call.ID, Name: pc.Call.Name}
		step := cv.Steps
		switch r, ok := results[pc.ToolUseID]; {
		case pc.Internal:
			d.readFile(fr, pc.Call, now)
			if fr.Output != nil && fr.Parts == nil {
				out := d.finishToolOutput(cv.Cascade, step, *fr.Output)
				fr.Output = &out
			}
		case !ok:
			fr.Error = strp("Encountered error in tool execution: User denied permission for " + pc.Call.Name + ".")
		case r.IsError:
			fr.Error = strp("Encountered error in tool execution: " + wireJoinText(r.Content))
		default:
			out := d.finishToolOutput(cv.Cascade, step, "Created At: "+utcStamp(pc.IssuedAt)+"\nCompleted At: "+utcStamp(now)+"\n"+wireJoinText(r.Content))
			fr.Output = &out
			for _, c := range r.Content {
				if c.Type == "image" && c.Data != "" {
					fr.Parts = append(fr.Parts, InlineData{MimeType: c.MimeType, Data: c.Data})
				}
			}
		}
		parts = append(parts, Part{Resp: fr})
		cv.wireRecord(trajStep{Kind: trajEnv, Call: pc.Call, Output: fr.Output, Error: fr.Error})
		cv.Steps++
	}
	cv.Contents = append(cv.Contents, Content{Role: "model", Parts: parts})
	cv.Pending = nil
}

func wireJoinText(cs []wireContentItem) string {
	var b strings.Builder
	for _, c := range cs {
		if c.Type == "text" {
			b.WriteString(c.Text)
		}
	}
	return b.String()
}

func (c *wireConv) wireRecord(st trajStep) {
	if len(c.Traj) == c.Steps {
		c.Traj = append(c.Traj, st)
	}
}

func (d *Daemon) wireUserStep(cv *wireConv, promptText string, images []InlineData, model ModelInfo, routeID string) {
	now := time.Now()
	var u strings.Builder
	u.WriteString("<USER_REQUEST>\n" + promptText + "\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: " + utcStamp(now) + ".\n")
	if len(images) > 0 {
		paths := d.saveUploads(cv.Cascade, images, now)
		u.WriteString(fmt.Sprintf("\nThe user has uploaded %d image(s):\n", len(images)))
		for _, pth := range paths {
			u.WriteString("- " + pth + "\n")
		}
		u.WriteString("You can embed this image in an artifact if you need the USER to review it.\n")
	}
	u.WriteString("</ADDITIONAL_METADATA>")
	if model.DisplayName != cv.AnnouncedModel {
		from := cv.AnnouncedModel
		if from == "" {
			from = "None"
		}
		u.WriteString("\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from " + from + " to " + model.DisplayName + settingsChangeTail)
		cv.AnnouncedModel = model.DisplayName
	}
	parts := []Part{{Text: strp(u.String())}}
	for i := range images {
		parts = append(parts, Part{Inline: &images[i]})
	}
	cv.Contents = append(cv.Contents, Content{Role: "user", Parts: parts})
	cv.wireRecord(trajStep{Kind: trajUser, Text: promptText})
	cv.Steps++
	if cv.InputInTool {
		d.wrapWireUserInput(cv)
	}
	cv.CurLastExec = ""
	if cv.Prompts > 0 {
		cv.CurLastExec = cv.LastExecID
	}
	cv.Prompts++
	cv.LastExecID = ids.UUID()
	if !cv.Titled {
		cv.Titled = true
		go d.generateTitle(routeID, promptText, images)
	}
}

func randCallID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "call_" + strconv.FormatUint(binary.BigEndian.Uint64(b[:]), 10)
}

func randToolUseID() string {
	return "toolu_" + randHex(12)
}

func withSummary(input json.RawMessage, summary string) json.RawMessage {
	if summary == "" {
		return input
	}
	m := map[string]json.RawMessage{}
	_ = json.Unmarshal(input, &m)
	s, _ := json.Marshal(summary)
	m["toolSummary"] = s
	b, _ := json.Marshal(m)
	return b
}

func (d *Daemon) wireRebuild(p *wireChatParams, model ModelInfo) *wireConv {
	cv := &wireConv{Key: p.Conv, Cascade: ids.UUID(), Trajectory: ids.UUID(), Cwd: p.Cwd, AppendText: p.Append,
		Tools: p.Tools, ToolsFP: p.ToolsFP, Titled: true, InputInTool: p.InputInTool}
	if len(p.Tools) > 0 {
		cv.MCPServer = d.wireMCPServerName("c")
	}
	server := cv.MCPServer

	lastUser := -1
	for i, t := range p.Turns {
		if t.Kind == "user" {
			lastUser = i
		}
	}
	flattenFrom := len(p.Turns)
	for i := lastUser + 1; i < len(p.Turns); i++ {
		if t := p.Turns[i]; t.Kind == "assistant" && len(t.ToolUses) > 0 && t.Signature == "" {
			flattenFrom = i
			break
		}
	}

	callIDs := map[string]FunctionCall{}
	var lastUserText string
	var lastImages []InlineData
	for _, t := range p.Turns[:flattenFrom] {
		switch t.Kind {
		case "user":
			d.wireUserStep(cv, t.Text, t.Images, model, p.RouteID)
			lastUserText, lastImages = t.Text, t.Images
		case "assistant":
			var parts []Part
			var calls []FunctionCall
			if len(t.ToolUses) > 0 {
				for j, tu := range t.ToolUses {
					meta := t.CallMeta[tu.ID]
					gid := meta.GemID
					if gid == "" {
						gid = randCallID()
					}
					name := tu.Name
					if server != "" && !isBuiltinToolName(name) {
						name = schema.DeclName(server, tu.Name)
					}
					fc := FunctionCall{ID: gid, Name: name, Args: withSummary(tu.Input, meta.Summary)}
					callIDs[tu.ID] = fc
					calls = append(calls, fc)
					pp := Part{Call: &calls[len(calls)-1]}
					if j == 0 {
						pp.Signature = t.Signature
					}
					parts = append(parts, pp)
				}
			} else {
				if t.Thinking != "" {
					parts = append(parts, Part{Text: strp(t.Thinking), Thought: true})
				}
				parts = append(parts, Part{Text: strp(t.Text), Signature: t.Signature})
			}
			cv.Contents = append(cv.Contents, Content{Role: "model", Parts: parts})
			cv.wireRecord(trajStep{Kind: trajModel, Thought: t.Thinking, Text: t.Text, Calls: calls})
			cv.Steps++
			cv.ModelCalls++
		case "results":
			now := time.Now()
			var parts []Part
			for _, r := range t.Results {
				fc, ok := callIDs[r.ID]
				if !ok {
					continue
				}
				fr := &FunctionResponse{ID: fc.ID, Name: fc.Name}
				if r.IsError {
					fr.Error = strp("Encountered error in tool execution: " + wireJoinText(r.Content))
				} else {
					out := d.finishToolOutput(cv.Cascade, cv.Steps, "Created At: "+utcStamp(now)+"\nCompleted At: "+utcStamp(now)+"\n"+wireJoinText(r.Content))
					fr.Output = &out
					for _, c := range r.Content {
						if c.Type == "image" && c.Data != "" {
							fr.Parts = append(fr.Parts, InlineData{MimeType: c.MimeType, Data: c.Data})
						}
					}
				}
				parts = append(parts, Part{Resp: fr})
				cv.wireRecord(trajStep{Kind: trajEnv, Call: fc, Output: fr.Output, Error: fr.Error})
				cv.Steps++
			}
			if len(parts) > 0 {
				cv.Contents = append(cv.Contents, Content{Role: "model", Parts: parts})
			}
		}
	}
	if flattenFrom < len(p.Turns) {
		d.wireUserStep(cv, flattenTurns(p.Turns[flattenFrom:]), nil, model, p.RouteID)
	}

	if lastUserText != "" || len(lastImages) > 0 {
		go d.generateTitle(p.RouteID, lastUserText, lastImages)
	}
	return cv
}

func isBuiltinToolName(name string) bool {
	switch name {
	case "read_file", "run_command", "manage_task", "send_message", "schedule", "invoke_subagent", "define_subagent",
		"manage_subagents", "write_file", "edit_file", "search_web", "run_workflow", "swarm", "ask_question",
		"list_resources", "read_resource":
		return true
	}
	return false
}

func flattenTurns(turns []wireTurn) string {
	var b strings.Builder
	for _, t := range turns {
		switch t.Kind {
		case "assistant":
			if strings.TrimSpace(t.Text) != "" {
				b.WriteString(t.Text + "\n")
			}
			for _, tu := range t.ToolUses {
				b.WriteString("[Tool Call: " + tu.Name + " (id=" + tu.ID + ") input=" + string(tu.Input) + "]\n")
			}
		case "results":
			for _, r := range t.Results {
				status := "OK"
				if r.IsError {
					status = "ERROR"
				}
				b.WriteString("[Tool Result (id=" + r.ID + ", status=" + status + ")]:\n" + wireJoinText(r.Content) + "\n")
			}
		case "user":
			b.WriteString(t.Text + "\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

func (d *Daemon) wireLoop(ctx context.Context, cv *wireConv, p *wireChatParams, model ModelInfo) (map[string]any, error) {
	var usage turnUsage
	system := RenderSystemPrompt(d.home, cv.Cwd, cv.Cascade, cv.MCPServer, cv.Tools, cv.AppendText, model.Guidelines)
	mapper := newBuiltinMapper(p.Tools, p.Cwd)
	denied := 0
	for {
		if ctx.Err() != nil {
			return nil, &wireError{err: context.Canceled, attempts: 1}
		}
		body, err := RenderAgent(AgentRequest{
			ToolConfig:     wireToolConfig(p.ToolConfig, cv.MCPServer, p.Tools),
			ThinkingConfig: p.ThinkingConfig,
			Cascade:        cv.Cascade, Trajectory: cv.Trajectory, UnixMilli: time.Now().UnixMilli(),
			StepIndex: cv.Steps, ModelCalls: cv.ModelCalls, LastExecutionID: cv.CurLastExec,
			Model: model, System: system, MCPServer: cv.MCPServer, MCPTools: cv.Tools, Contents: cv.Contents,
			ResourceTools: d.mcpEverRegistered(),
		})
		if err != nil {
			return nil, err
		}
		cv.ModelCalls++
		res, attempts, err := d.generateCounted(ctx, p.RouteID, body)
		if err != nil {
			if errors.Is(err, ErrAuth) {
				return nil, ErrAuth
			}
			var ae *APIError
			if errors.As(err, &ae) && ae.HTTPStatus == 401 {
				return nil, ErrAuth
			}
			return nil, &wireError{err: err, attempts: attempts}
		}
		usage.add(res.usage)
		if res.usage != nil {
			d.update(p.RouteID, map[string]any{"sessionUpdate": "usage_update", "used": res.usage.TotalTokenCount, "size": model.MaxTokens})
		}
		cv.Contents = append(cv.Contents, res.modelContent())
		modelStep := cv.Steps
		cv.wireRecord(trajStep{Kind: trajModel, Thought: res.thought, Text: res.text, Calls: res.calls})
		cv.Steps++
		out := map[string]any{"usage": usage.acp(), "signature": res.signature, "thought": res.thought != ""}
		if len(res.calls) == 0 {
			stop := "end_turn"
			if res.finish == "MAX_TOKENS" {
				stop = "max_tokens"
			}
			cv.LastStop, cv.LastToolUseIDs, cv.Pending = stop, nil, nil
			out["stopReason"] = stop
			return out, nil
		}

		prefix := ""
		if cv.MCPServer != "" {
			prefix = "mcp_" + cv.MCPServer + "_"
		}
		allInternal := true
		denyTools := p.DenyTools
		for _, fc := range res.calls {
			if !d.wireInternal(fc) {
				allInternal = false
				if prefix == "" || !strings.HasPrefix(fc.Name, prefix) {
					if _, _, ok := mapper.translate(fc); !ok {
						denyTools = true
					}
				}
			}
		}
		if denyTools || allInternal {

			var parts []Part
			for k, fc := range res.calls {
				d.runPreToolMonitors(ctx, cv.Traj, model, modelStep, k, fc)
				fr := &FunctionResponse{ID: fc.ID, Name: fc.Name}
				if denyTools {
					reply := p.DenyReply
					if reply == "" {
						reply = "Use the tools provided by the client instead of unavailable host tools."
					}
					fr.Error = strp("Encountered error in tool execution: " + reply)
				} else {
					d.readFile(fr, fc, time.Now())
					if fr.Output != nil && fr.Parts == nil {
						o := d.finishToolOutput(cv.Cascade, cv.Steps, *fr.Output)
						fr.Output = &o
					}
				}
				parts = append(parts, Part{Resp: fr})
				cv.wireRecord(trajStep{Kind: trajEnv, Call: fc, Output: fr.Output, Error: fr.Error})
				cv.Steps++
			}
			cv.Contents = append(cv.Contents, Content{Role: "model", Parts: parts})
			if denyTools {
				denied++
				if denied >= wireMaxDeniedRounds {
					cv.LastStop, cv.LastToolUseIDs = "end_turn", nil
					out["stopReason"] = "end_turn"
					return out, nil
				}
			}
			continue
		}

		d.runPreToolMonitors(ctx, cv.Traj, model, modelStep, 0, res.calls[0])
		now := time.Now()
		var calls []wireToolCallOut
		meta := map[string]wireCallMeta{}
		cv.Pending, cv.LastToolUseIDs = nil, nil
		for k, fc := range res.calls {
			tid := randToolUseID()
			internal := d.wireInternal(fc)
			cv.Pending = append(cv.Pending, wirePending{ToolUseID: tid, Call: fc, Internal: internal, IssuedAt: now, Monitored: k == 0})
			if internal {
				continue
			}
			args := argsMap(fc.Args)
			summary, _ := args["toolSummary"].(string)
			meta[tid] = wireCallMeta{Summary: summary, GemID: fc.ID}
			name, builtin := fc.Name, true
			if prefix != "" && strings.HasPrefix(fc.Name, prefix) {
				name, builtin = strings.TrimPrefix(fc.Name, prefix), false
			}
			input := stripToolSummary(fc.Args)
			if builtin {
				if n, a, ok := mapper.translate(fc); ok {
					name, input, builtin = n, a, false
				}
			}
			calls = append(calls, wireToolCallOut{ID: tid, Name: name, Input: input, Builtin: builtin})
			cv.LastToolUseIDs = append(cv.LastToolUseIDs, tid)
		}
		cv.PendingStep = modelStep
		cv.LastStop = "tool_use"
		out["stopReason"] = "tool_use"
		out["toolCalls"] = calls
		out["callMeta"] = meta
		return out, nil
	}
}

func (d *Daemon) wireInternal(fc FunctionCall) bool {
	if fc.Name != "read_file" {
		return false
	}
	var a struct {
		Path string `json:"Path"`
	}
	_ = json.Unmarshal(fc.Args, &a)
	root := filepath.Dir(d.home) + string(os.PathSeparator)
	return a.Path != "" && strings.HasPrefix(filepath.Clean(a.Path), root)
}

func (d *Daemon) generateCounted(ctx context.Context, routeID string, body []byte) (*genResult, int, error) {
	attempts := 0
	res, err := d.generateWithRetryHook(ctx, routeID, body, func() { attempts++ })
	return res, attempts, err
}
