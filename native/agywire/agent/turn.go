package agent

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"mime"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gcli2api/agy-http/agywire/ids"
	"gcli2api/agy-http/agywire/mcpc"
	"gcli2api/agy-http/agywire/upsse"
)

func Supported(m ModelInfo) bool {
	return !m.TemplateOverride && (!m.NeedsMonitors || m.Monitors != nil)
}

func unsupportedError(id string, m ModelInfo) error {
	reason := "未知原因"
	switch {
	case m.TemplateOverride:
		reason = "目录里的系统提示模板未核对"
	case m.NeedsMonitors && m.Monitors == nil:
		reason = "目录里的安全监控配置未核对 sha256=" + m.UnverifiedMonitorsSHA
	}
	return fmt.Errorf("unknown model: %s(%s)", id, reason)
}

type promptBlock struct {
	Type     string `json:"type"`
	Text     string `json:"text"`
	MimeType string `json:"mimeType"`
	Data     string `json:"data"`
}

type turnUsage struct {
	Input, Output, CachedRead, Thought, Total int64
}

func (u turnUsage) acp() map[string]any {
	return map[string]any{
		"inputTokens": u.Input, "outputTokens": u.Output, "cachedReadTokens": u.CachedRead,
		"cachedWriteTokens": 0, "thoughtTokens": u.Thought, "totalTokens": u.Total,
	}
}

func (u *turnUsage) add(m *upsse.Usage) {
	if m == nil {
		return
	}
	u.Input += m.PromptTokenCount - m.CachedContentTokenCount
	u.CachedRead += m.CachedContentTokenCount
	u.Output += m.CandidatesTokenCount + m.ThoughtsTokenCount
	u.Thought += m.ThoughtsTokenCount
	u.Total += m.TotalTokenCount
}

const settingsChangeTail = ". No need to comment on this change if the user doesn't ask about it. If reporting what model you are, please use a human readable name instead of the exact string.\n</USER_SETTINGS_CHANGE>"

func utcStamp(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05Z") }

func (d *Daemon) sessionPrompt(raw json.RawMessage) (any, error) {
	var p struct {
		SessionID string        `json:"sessionId"`
		Prompt    []promptBlock `json:"prompt"`
	}
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, err
	}
	s, err := d.session(p.SessionID)
	if err != nil {
		return nil, err
	}
	cat, err := d.Catalog(d.ctx)
	if err != nil {
		return nil, err
	}
	d.touchCatalog()

	s.mu.Lock()
	defer s.mu.Unlock()
	ctx, cancel := context.WithCancel(d.ctx)
	s.setCancel(cancel)
	defer func() { cancel(); s.setCancel(nil) }()

	model, ok := cat.Models[s.ModelID]
	if !ok || !Supported(model) {

		return nil, fmt.Errorf("unknown model: %s (agy-wire 第一期不支持)", s.ModelID)
	}

	var text strings.Builder
	var images []InlineData
	for _, b := range p.Prompt {
		switch b.Type {
		case "text":
			text.WriteString(b.Text)
			d.update(s.ID, map[string]any{"sessionUpdate": "user_message_chunk", "content": map[string]any{"type": "text", "text": b.Text}})
		case "image":
			images = append(images, InlineData{MimeType: b.MimeType, Data: b.Data})
		}
	}
	promptText := text.String()
	now := time.Now()

	if !s.titled {
		s.titled = true
		go d.generateTitle(s.ID, promptText, images)
	}

	var u strings.Builder
	u.WriteString("<USER_REQUEST>\n" + promptText + "\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: " + utcStamp(now) + ".\n")
	if len(images) > 0 {
		paths := d.saveUploads(s.ID, images, now)
		u.WriteString(fmt.Sprintf("\nThe user has uploaded %d image(s):\n", len(images)))
		for _, pth := range paths {
			u.WriteString("- " + pth + "\n")
		}
		u.WriteString("You can embed this image in an artifact if you need the USER to review it.\n")
	}
	u.WriteString("</ADDITIONAL_METADATA>")
	if model.DisplayName != s.announcedModel {
		from := s.announcedModel
		if from == "" {
			from = "None"
		}
		u.WriteString("\n<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from " + from + " to " + model.DisplayName + settingsChangeTail)
		s.announcedModel = model.DisplayName
	}
	userParts := []Part{{Text: strp(u.String())}}
	for i := range images {
		userParts = append(userParts, Part{Inline: &images[i]})
	}
	s.Contents = append(s.Contents, Content{Role: "user", Parts: userParts})
	s.recordStep(trajStep{Kind: trajUser, Text: promptText})
	s.Steps++
	lastExec := ""
	if s.prompts > 0 {
		lastExec = s.lastExecID
	}
	s.prompts++
	s.lastExecID = ids.UUID()

	var usage turnUsage
	system := RenderSystemPrompt(d.home, s.Cwd, s.ID, s.MCPServer, s.MCPTools, s.AppendText, model.Guidelines)
	for {
		if ctx.Err() != nil {
			return map[string]any{"stopReason": "cancelled", "usage": usage.acp()}, nil
		}
		body, err := RenderAgent(AgentRequest{
			Cascade: s.ID, Trajectory: s.Trajectory, UnixMilli: time.Now().UnixMilli(),
			StepIndex: s.Steps, ModelCalls: s.ModelCalls, LastExecutionID: lastExec,
			Model: model, System: system, MCPServer: s.MCPServer, MCPTools: s.MCPTools, Contents: s.Contents,
			ResourceTools: d.mcpEverRegistered(),
		})
		if err != nil {
			return nil, err
		}
		s.ModelCalls++
		res, err := d.generateWithRetry(ctx, s.ID, body)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return map[string]any{"stopReason": "cancelled", "usage": usage.acp()}, nil
			}
			if errors.Is(err, ErrAuth) {
				return nil, ErrAuth
			}
			var ae *APIError
			if errors.As(err, &ae) {
				if ae.HTTPStatus == 401 {
					return nil, ErrAuth
				}

				d.update(s.ID, map[string]any{"sessionUpdate": "agent_message_chunk", "content": map[string]any{"type": "text",
					"text": "\n⚠ " + ae.Error() + "\n"}})
				return map[string]any{"stopReason": "end_turn", "usage": usage.acp()}, nil
			}
			return nil, err
		}
		usage.add(res.usage)
		if res.usage != nil {
			d.update(s.ID, map[string]any{"sessionUpdate": "usage_update", "used": res.usage.TotalTokenCount, "size": model.MaxTokens})
		}
		s.Contents = append(s.Contents, res.modelContent())
		modelStep := s.Steps
		s.recordStep(trajStep{Kind: trajModel, Thought: res.thought, Text: res.text, Calls: res.calls})
		s.Steps++
		if len(res.calls) == 0 {
			stop := "end_turn"
			if res.finish == "MAX_TOKENS" {
				stop = "max_tokens"
			}
			return map[string]any{"stopReason": stop, "usage": usage.acp()}, nil
		}
		var respParts []Part
		for k, fc := range res.calls {

			d.runPreToolMonitors(ctx, s.traj, model, modelStep, k, fc)
			fr := d.runTool(ctx, s, fc, s.Steps)
			respParts = append(respParts, Part{Resp: fr})
			s.recordStep(trajStep{Kind: trajEnv, Call: fc, Output: fr.Output, Error: fr.Error})
			s.Steps++
		}
		s.Contents = append(s.Contents, Content{Role: "model", Parts: respParts})
	}
}

func (s *Session) recordStep(st trajStep) {
	if len(s.traj) == s.Steps {
		s.traj = append(s.traj, st)
	}
}

type genResult struct {
	thought   string
	text      string
	calls     []FunctionCall
	signature string
	sigOnText bool
	usage     *upsse.Usage
	finish    string
}

func (r *genResult) modelContent() Content {
	var parts []Part
	if len(r.calls) > 0 {
		for i := range r.calls {
			p := Part{Call: &r.calls[i]}
			if i == 0 {
				p.Signature = r.signature
			}
			parts = append(parts, p)
		}
		return Content{Role: "model", Parts: parts}
	}
	if r.thought != "" {
		parts = append(parts, Part{Text: strp(r.thought), Thought: true})
	}
	parts = append(parts, Part{Text: strp(r.text), Signature: r.signature})
	return Content{Role: "model", Parts: parts}
}

func (d *Daemon) generateWithRetry(ctx context.Context, sessionID string, body []byte) (*genResult, error) {
	return d.generateWithRetryHook(ctx, sessionID, body, nil)
}

func (d *Daemon) generateWithRetryHook(ctx context.Context, sessionID string, body []byte, onAttempt func()) (*genResult, error) {
	attempt := 0
	for {
		attempt++
		if onAttempt != nil {
			onAttempt()
		}
		if d.testGen != nil {
			return d.testGen(body)
		}
		t0 := time.Now()
		res, err := d.generateOnce(ctx, sessionID, body)
		if err == nil {
			log.Printf("生成成功: bytes=%d attempt=%d ms=%d", len(body), attempt, time.Since(t0).Milliseconds())
			return res, nil
		}
		var ae *APIError
		if errors.As(err, &ae) && ae.HTTPStatus == 429 && ae.Message == "Resource has been exhausted (e.g. check quota)." {
			return nil, err
		}
		if errors.As(err, &ae) {
			raw := strings.Join(strings.Fields(ae.Raw), " ")
			if len(raw) > 1500 {
				raw = raw[:1500]
			}
			log.Printf("生成失败: bytes=%d attempt=%d http=%d status=%s reason=%s retryDelay=%s retryAfter=%s raw=%s",
				len(body), attempt, ae.HTTPStatus, ae.Status, ae.Reason, ae.RetryDelay, ae.RetryAfter, raw)
		} else {
			log.Printf("生成失败: bytes=%d attempt=%d err=%v", len(body), attempt, err)
		}

		if errors.As(err, &ae) && ae.HTTPStatus == 401 && attempt == 1 {
			d.tokens.invalidate()
			continue
		}
		if !errors.As(err, &ae) || !ae.Retryable() || attempt > retryMaxRetries {
			return nil, err
		}
		d.update(sessionID, map[string]any{"sessionUpdate": "agent_message_chunk", "content": map[string]any{"type": "text",
			"text": fmt.Sprintf("\n⚠ API error (attempt %d): %s\n", attempt, ae.Error())}})
		wait := backoff(attempt)
		if ae.RetryDelay > wait {
			wait = ae.RetryDelay
		}
		if ae.RetryAfter > wait {
			wait = ae.RetryAfter
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(wait):
		}
	}
}

func (d *Daemon) generateOnce(ctx context.Context, sessionID string, body []byte) (*genResult, error) {
	rd, closer, err := d.streamGenerate(ctx, body)
	if err != nil {
		return nil, err
	}
	defer closer.Close()
	res := &genResult{}
	for {
		ch, err := rd.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		if ch.Usage != nil {
			res.usage = ch.Usage
		}
		if ch.FinishReason != "" {
			res.finish = ch.FinishReason
		}
		for _, p := range ch.Parts {
			if p.ThoughtSignature != "" && res.signature == "" {
				res.signature = p.ThoughtSignature
			}
			switch {
			case p.FunctionCall != nil:
				res.calls = append(res.calls, FunctionCall{ID: p.FunctionCall.ID, Name: p.FunctionCall.Name, Args: p.FunctionCall.Args})
			case p.Thought:
				if p.Text != "" {
					res.thought += p.Text
					d.update(sessionID, map[string]any{"sessionUpdate": "agent_thought_chunk", "content": map[string]any{"type": "text", "text": p.Text}})
				}
			default:
				if p.Text != "" {
					res.text += p.Text
					d.update(sessionID, map[string]any{"sessionUpdate": "agent_message_chunk", "content": map[string]any{"type": "text", "text": p.Text}})
				}
			}
		}
	}
	return res, nil
}

func (d *Daemon) generateTitle(sessionID, promptText string, images []InlineData) {
	if d.testGen != nil {
		return
	}
	body := RenderTitle(promptText, images)
	ctx, cancel := context.WithTimeout(d.ctx, 2*time.Minute)
	defer cancel()
	rd, closer, err := d.streamGenerate(ctx, body)
	if err != nil {
		return
	}
	defer closer.Close()
	var title strings.Builder
	for {
		ch, err := rd.Next()
		if err != nil {
			break
		}
		for _, p := range ch.Parts {
			if !p.Thought {
				title.WriteString(p.Text)
			}
		}
	}
	if t := strings.TrimSpace(title.String()); t != "" {
		d.update(sessionID, map[string]any{"sessionUpdate": "session_info_update", "title": t})
	}
}

func (d *Daemon) saveUploads(conv string, images []InlineData, now time.Time) []string {
	dir := filepath.Join(d.home, ".gemini", "antigravity-cli", "brain", conv, ".user_uploaded")
	_ = os.MkdirAll(dir, 0o700)
	var paths []string
	for i, img := range images {
		ext := ".png"
		if exts, _ := mime.ExtensionsByType(img.MimeType); len(exts) > 0 {
			ext = exts[0]
			if img.MimeType == "image/jpeg" {
				ext = ".jpg"
			}
		}
		name := fmt.Sprintf("uploaded_media_%d%s", now.UnixMilli()+int64(i), ext)
		pth := filepath.Join(dir, name)
		if raw, err := base64.StdEncoding.DecodeString(img.Data); err == nil {
			_ = os.WriteFile(pth, raw, 0o600)
		}
		paths = append(paths, pth)
	}
	return paths
}

func (d *Daemon) toolCallUpdate(sessionID, kind string, fc FunctionCall, status string) map[string]any {
	args := fc.Args
	if len(args) == 0 {
		args = json.RawMessage("{}")
	}
	return map[string]any{
		"sessionUpdate": kind, "toolCallId": fc.ID, "name": fc.Name, "args": args,
		"status": status, "kind": "other", "title": "Run " + fc.Name, "rawInput": args,
	}
}

func (d *Daemon) requestPermission(ctx context.Context, sessionID string, fc FunctionCall) bool {
	params := map[string]any{
		"sessionId": sessionID,
		"toolCall":  d.toolCallUpdate(sessionID, "tool_call", fc, "pending"),
		"options": []map[string]string{
			{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
			{"optionId": "deny", "name": "Deny", "kind": "reject_once"},
		},
	}
	raw, err := d.conn.Call(ctx, "session/request_permission", params)
	if err != nil {
		return false
	}
	var r struct {
		Outcome struct {
			OptionID string `json:"optionId"`
		} `json:"outcome"`
	}
	_ = json.Unmarshal(raw, &r)
	return r.Outcome.OptionID == "allow"
}

func stripToolSummary(args json.RawMessage) json.RawMessage {
	var m map[string]json.RawMessage
	if json.Unmarshal(args, &m) != nil {
		return args
	}
	delete(m, "toolSummary")
	b, _ := json.Marshal(m)
	return b
}

func (d *Daemon) runTool(ctx context.Context, s *Session, fc FunctionCall, step int) *FunctionResponse {
	d.update(s.ID, d.toolCallUpdate(s.ID, "tool_call", fc, "in_progress"))
	d.update(s.ID, d.toolCallUpdate(s.ID, "tool_call_update", fc, "pending"))
	fr := &FunctionResponse{ID: fc.ID, Name: fc.Name}
	prefix := "mcp_" + s.MCPServer + "_"
	isMCP := s.mcp != nil && strings.HasPrefix(fc.Name, prefix)

	if isMCP || !isReadOnlyBuiltin(fc.Name) {
		if !d.requestPermission(ctx, s.ID, fc) {

			fr.Error = strp("Encountered error in tool execution: User denied permission for " + fc.Name + ".")
			d.update(s.ID, d.toolCallUpdate(s.ID, "tool_call_update", fc, "failed"))
			return fr
		}
	}
	d.update(s.ID, d.toolCallUpdate(s.ID, "tool_call_update", fc, "in_progress"))
	start := time.Now()

	switch {
	case isMCP:
		name := strings.TrimPrefix(fc.Name, prefix)
		res, err := s.mcp.CallTool(ctx, name, stripToolSummary(fc.Args))
		end := time.Now()
		switch {
		case err != nil && errors.Is(err, context.DeadlineExceeded):
			fr.Error = strp(fmt.Sprintf("Encountered error in tool execution: MCP tool call to server %q timed out after %s: context deadline exceeded", s.MCPServer, mcpc.CallTimeout))
		case err != nil:
			fr.Error = strp("Encountered error in tool execution: " + err.Error())
		case res.IsError:
			fr.Error = strp("Encountered error in tool execution: " + joinText(res.Content))
		default:
			out := d.finishToolOutput(s.ID, step, "Created At: "+utcStamp(start)+"\nCompleted At: "+utcStamp(end)+"\n"+joinText(res.Content))
			fr.Output = &out
			for _, c := range res.Content {
				if c.Type == "image" && c.Data != "" {
					fr.Parts = append(fr.Parts, InlineData{MimeType: c.MimeType, Data: c.Data})
				}
			}
		}
	case fc.Name == "read_file":
		d.readFile(fr, fc, start)
		if fr.Output != nil && fr.Parts == nil {
			out := d.finishToolOutput(s.ID, step, *fr.Output)
			fr.Output = &out
		}
	default:
		fr.Error = strp("Encountered error in tool execution: tool " + fc.Name + " is not available")
	}
	status := "completed"
	if fr.Error != nil {
		status = "failed"
	}
	d.update(s.ID, d.toolCallUpdate(s.ID, "tool_call_update", fc, status))
	return fr
}

func isReadOnlyBuiltin(name string) bool {
	return name == "read_file"
}

func joinText(cs []mcpc.Content) string {
	var b strings.Builder
	for _, c := range cs {
		if c.Type == "text" {
			b.WriteString(c.Text)
		}
	}
	return b.String()
}

func (d *Daemon) readFile(fr *FunctionResponse, fc FunctionCall, start time.Time) {
	var a struct {
		Path string `json:"Path"`
	}
	_ = json.Unmarshal(fc.Args, &a)
	root := filepath.Dir(d.home)
	clean := filepath.Clean(a.Path)
	if a.Path == "" || !strings.HasPrefix(clean, root+string(os.PathSeparator)) {
		fr.Error = strp("Encountered error in tool execution: file " + a.Path + " is outside the allowed workspace")
		return
	}
	raw, err := os.ReadFile(clean)
	end := time.Now()
	if err != nil {
		fr.Error = strp("Encountered error in tool execution: " + err.Error())
		return
	}
	mt := mime.TypeByExtension(filepath.Ext(clean))
	if i := strings.IndexByte(mt, ';'); i > 0 {
		mt = mt[:i]
	}
	if mt == "" || strings.HasPrefix(mt, "text/") {
		out := "Created At: " + utcStamp(start) + "\nCompleted At: " + utcStamp(end) + "\n" + string(raw)
		fr.Output = &out
		return
	}
	out := "Created At: " + utcStamp(start) + "\nCompleted At: " + utcStamp(end) + "\nShowing binary file of type " + mt
	fr.Output = &out
	fr.Parts = []InlineData{{MimeType: mt, Data: base64.StdEncoding.EncodeToString(raw)}}
}
