package agent

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	"gcli2api/agy-http/agywire/acp"
	"gcli2api/agy-http/agywire/transport"
)

//go:embed assets/acp_initialize_result.json
var initializeResult []byte

//go:embed assets/acp_session_templates.json
var sessionTemplatesRaw []byte

type Daemon struct {
	ctx    context.Context
	conn   *acp.Conn
	notify func(string, map[string]any)
	home   string
	proxy  string
	tokens *TokenSource

	httpOnce sync.Once
	http     *http.Client
	httpErr  error

	ctrl ctrlState

	mu         sync.Mutex
	sessions   map[string]*Session
	mcpServers map[string]int

	testSuffixIdx int

	wire *wireStore

	testGen func(body []byte) (*genResult, error)

	monitorSend func(ctx context.Context, body []byte, timeout time.Duration) monitorResult
}

func NewDaemon(ctx context.Context, out io.Writer) *Daemon {
	home, _ := os.UserHomeDir()
	proxy := firstEnv("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
	return &Daemon{
		ctx:        ctx,
		conn:       acp.NewConn(out),
		home:       home,
		proxy:      proxy,
		tokens:     NewTokenSource(home),
		sessions:   map[string]*Session{},
		mcpServers: map[string]int{},
		wire:       newWireStore(home),
	}
}

func firstEnv(keys ...string) string {
	for _, k := range keys {
		if v := os.Getenv(k); v != "" {
			return v
		}
	}
	return ""
}

func (d *Daemon) client() (*http.Client, error) {
	d.httpOnce.Do(func() { d.http, d.httpErr = transport.ClientFor(d.proxy) })
	return d.http, d.httpErr
}

func (d *Daemon) anonGet(ctx context.Context, url string) ([]byte, int, error) {
	c, err := d.client()
	if err != nil {
		return nil, 0, err
	}
	req, err := transport.Build(ctx, transport.Spec{Method: "GET", URL: url})
	if err != nil {
		return nil, 0, err
	}
	resp, err := c.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	return b, resp.StatusCode, err
}

func (d *Daemon) Run(in io.Reader) error {
	d.startControlPlane(d.ctx)
	return d.conn.Serve(in, d.dispatch)
}

func (d *Daemon) dispatch(req acp.Request) {
	var result any
	var err error
	switch req.Method {
	case "initialize":
		result = json.RawMessage(initializeResult)
	case "session/new":
		result, err = d.sessionNew(req.Params)
	case "session/set_model":
		result, err = d.sessionSetModel(req.Params)
	case "session/set_config_option":
		result, err = d.sessionSetConfigOption(req.Params)
	case "session/prompt":
		result, err = d.sessionPrompt(req.Params)
	case "session/cancel":
		d.sessionCancel(req.Params)
		return
	case "session/close":
		result, err = d.sessionClose(req.Params)
	case "session/resume":
		result, err = d.sessionResume(req.Params)
	case "_wire/chat":
		result, err = d.wireChat(req.Params)
	case "_wire/quota":
		ctx, cancel := context.WithTimeout(d.ctx, quotaRPCTimeout)
		result, err = d.wireQuota(ctx)
		cancel()
	case "_wire/cancel":
		var c struct {
			RouteID string `json:"routeId"`
		}
		if json.Unmarshal(req.Params, &c) == nil && c.RouteID != "" {
			d.wire.cancel(c.RouteID)
		}
		return
	default:
		if len(req.ID) > 0 {
			_ = d.conn.ReplyError(req.ID, -32601, fmt.Sprintf("Method not found: %s", req.Method))
		}
		return
	}
	if len(req.ID) == 0 {
		return
	}
	if err != nil {
		code := -32603
		if err == ErrAuth {
			code = -32000
		}
		msg := err.Error()
		var ae *APIError
		if errors.As(err, &ae) {
			msg += ae.GatewayHint()
		}
		_ = d.conn.ReplyError(req.ID, code, msg)
		return
	}
	if err := d.conn.Reply(req.ID, result); err != nil {
		log.Printf("ACP 回复失败: %v", err)
	}
}

func (d *Daemon) session(id string) (*Session, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	s, ok := d.sessions[id]
	if !ok {
		return nil, fmt.Errorf("Session not found: %s", id)
	}
	return s, nil
}

func (d *Daemon) update(sessionID string, update map[string]any) {
	if d.notify != nil {
		d.notify(sessionID, update)
		return
	}
	_ = d.conn.Notify("session/update", map[string]any{"sessionId": sessionID, "update": update})
}
