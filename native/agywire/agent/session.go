package agent

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"sync"

	"gcli2api/agy-http/agywire/ids"
	"gcli2api/agy-http/agywire/mcpc"
	"gcli2api/agy-http/agywire/schema"
)

type Session struct {
	ID         string
	Trajectory string
	Cwd        string
	AppendText string

	MCPServer string
	mcp       *mcpc.Client
	MCPTools  []schema.MCPTool

	mu             sync.Mutex
	ModelID        string
	announcedModel string
	Contents       []Content
	Steps          int
	ModelCalls     int
	lastExecID     string
	prompts        int
	titled         bool
	traj           []trajStep

	cancelMu sync.Mutex
	cancel   context.CancelFunc
}

func (s *Session) setCancel(c context.CancelFunc) {
	s.cancelMu.Lock()
	s.cancel = c
	s.cancelMu.Unlock()
}

func (s *Session) cancelTurn() {
	s.cancelMu.Lock()
	if s.cancel != nil {
		s.cancel()
	}
	s.cancelMu.Unlock()
}

type mcpServerParam struct {
	Name    string `json:"name"`
	Type    string `json:"type"`
	URL     string `json:"url"`
	Headers []struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	} `json:"headers"`
}

type sessionNewParams struct {
	SessionID  string           `json:"sessionId"`
	Cwd        string           `json:"cwd"`
	MCPServers []mcpServerParam `json:"mcpServers"`
	Meta       struct {
		Antigravity struct {
			SystemPrompt struct {
				Append string `json:"append"`
			} `json:"systemPrompt"`
		} `json:"antigravity"`
	} `json:"_meta"`
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func (d *Daemon) mcpEverRegistered() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.mcpServers) > 0
}

func (d *Daemon) mcpServerName(name string) string {
	d.mu.Lock()
	defer d.mu.Unlock()
	n := d.mcpServers[name]
	d.mcpServers[name] = n + 1
	if n == 0 {
		return name
	}

	if q := os.Getenv("AGY_WIRE_TEST_MCP_SUFFIXES"); q != "" {
		parts := strings.Split(q, ",")
		if d.testSuffixIdx < len(parts) {
			sfx := parts[d.testSuffixIdx]
			d.testSuffixIdx++
			return name + "_" + sfx
		}
	}
	return name + "_" + randHex(4)
}

func (d *Daemon) wireMCPServerName(name string) string {
	if n := d.mcpServerName(name); n != name {
		return n
	}
	return d.mcpServerName(name)
}

func (d *Daemon) sessionNew(raw json.RawMessage) (any, error) {
	var p sessionNewParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, err
	}
	cat, err := d.Catalog(d.ctx)
	if err != nil {
		return nil, err
	}
	s := &Session{
		ID:         ids.UUID(),
		Trajectory: ids.UUID(),
		Cwd:        p.Cwd,
		AppendText: p.Meta.Antigravity.SystemPrompt.Append,
		ModelID:    cat.DefaultModel,
	}
	if p.SessionID != "" {
		s.ID = p.SessionID
	}
	if len(p.MCPServers) > 0 {
		srv := p.MCPServers[0]
		hdr := map[string]string{}
		for _, h := range srv.Headers {
			hdr[h.Name] = h.Value
		}
		c := mcpc.New(srv.URL, hdr)
		ctx, cancel := context.WithTimeout(d.ctx, mcpc.CallTimeout)
		defer cancel()
		if err := c.Initialize(ctx); err != nil {
			return nil, fmt.Errorf("MCP 初始化失败: %w", err)
		}
		tools, err := c.ListTools(ctx)
		if err != nil {
			return nil, fmt.Errorf("MCP tools/list 失败: %w", err)
		}
		s.mcp, s.MCPTools = c, tools
		s.MCPServer = d.mcpServerName(srv.Name)
	}
	d.mu.Lock()
	d.sessions[s.ID] = s
	d.mu.Unlock()

	result := d.sessionResult(s, cat)

	go func() {
		d.update(s.ID, map[string]any{"sessionUpdate": "available_commands_update", "availableCommands": sessionTemplates().AvailableCommands})
		d.update(s.ID, map[string]any{"sessionUpdate": "usage_update", "used": 0, "size": d.contextSize(cat, s.ModelID)})
	}()
	return result, nil
}

type sessionTmpl struct {
	Modes             json.RawMessage `json:"modes"`
	ConfigOptions     json.RawMessage `json:"configOptions"`
	AvailableCommands json.RawMessage `json:"availableCommands"`
}

func sessionTemplates() sessionTmpl {
	var t sessionTmpl
	_ = json.Unmarshal(sessionTemplatesRaw, &t)
	return t
}

func (d *Daemon) contextSize(cat *Catalog, model string) int64 {
	if m, ok := cat.Models[model]; ok && m.MaxTokens > 0 {
		return m.MaxTokens
	}
	return 1048576
}

func (d *Daemon) sessionResult(s *Session, cat *Catalog) map[string]any {
	var avail []map[string]string
	for _, id := range cat.AgentModels {

		if m, ok := cat.Models[id]; ok && !Supported(m) {
			continue
		}
		name := id
		if m, ok := cat.Models[id]; ok && m.DisplayName != "" {
			name = m.DisplayName
		}
		avail = append(avail, map[string]string{"modelId": id, "name": name})
	}
	t := sessionTemplates()
	res := map[string]any{
		"sessionId":     s.ID,
		"models":        map[string]any{"availableModels": avail, "currentModelId": s.ModelID},
		"modes":         t.Modes,
		"configOptions": t.ConfigOptions,
	}
	if cat.PaidTier != nil {
		res["_meta"] = map[string]any{"antigravity": map[string]any{"tier": map[string]string{
			"name": cat.PaidTier["name"], "id": cat.PaidTier["id"], "upgradeUri": cat.PaidTier["upgradeSubscriptionUri"],
		}}}
	}
	return res
}

func (d *Daemon) sessionSetModel(raw json.RawMessage) (any, error) {
	var p struct {
		SessionID string `json:"sessionId"`
		ModelID   string `json:"modelId"`
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
	if m, ok := cat.Models[p.ModelID]; !ok {
		return nil, fmt.Errorf("unknown model: %s", p.ModelID)
	} else if !Supported(m) {
		return nil, unsupportedError(p.ModelID, m)
	}
	s.mu.Lock()
	s.ModelID = p.ModelID
	s.mu.Unlock()
	d.update(s.ID, map[string]any{"sessionUpdate": "config_option_update", "configOptions": sessionTemplates().ConfigOptions})
	return map[string]any{}, nil
}

func (d *Daemon) sessionSetConfigOption(raw json.RawMessage) (any, error) {
	var p struct {
		SessionID string `json:"sessionId"`
		ConfigID  string `json:"configId"`
		Value     string `json:"value"`
	}
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, err
	}
	if _, err := d.session(p.SessionID); err != nil {
		return nil, err
	}
	return map[string]any{"configOptions": sessionTemplates().ConfigOptions}, nil
}

func (d *Daemon) sessionCancel(raw json.RawMessage) {
	var p struct {
		SessionID string `json:"sessionId"`
	}
	if json.Unmarshal(raw, &p) != nil {
		return
	}
	if s, err := d.session(p.SessionID); err == nil {
		s.cancelTurn()
	}
}

func (d *Daemon) sessionClose(raw json.RawMessage) (any, error) {
	var p struct {
		SessionID string `json:"sessionId"`
	}
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, err
	}
	d.mu.Lock()
	s := d.sessions[p.SessionID]
	delete(d.sessions, p.SessionID)
	d.mu.Unlock()
	if s != nil {
		s.cancelTurn()
	}
	return map[string]any{}, nil
}

func (d *Daemon) sessionResume(raw json.RawMessage) (any, error) {
	var p sessionNewParams
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
	return d.sessionResult(s, cat), nil
}
