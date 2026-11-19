package mcpc

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync/atomic"
	"time"

	"gcli2api/agy-http/agywire/schema"
)

const CallTimeout = 3 * time.Minute

type Client struct {
	URL     string
	Headers map[string]string
	hc      *http.Client
	id      atomic.Int64
}

func New(url string, headers map[string]string) *Client {
	return &Client{URL: url, Headers: headers, hc: &http.Client{Transport: &http.Transport{Proxy: nil}}}
}

type rpcResp struct {
	Result json.RawMessage `json:"result"`
	Error  *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func (c *Client) call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	id := c.id.Add(1)
	body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
	req, err := http.NewRequestWithContext(ctx, "POST", c.URL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	for k, v := range c.Headers {
		req.Header.Set(k, v)
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var r rpcResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, fmt.Errorf("MCP %s 回包解析失败(HTTP %d): %w", method, resp.StatusCode, err)
	}
	if r.Error != nil {
		return nil, fmt.Errorf("MCP %s 错误 %d: %s", method, r.Error.Code, r.Error.Message)
	}
	return r.Result, nil
}

func (c *Client) notify(ctx context.Context, method string) {
	body, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "method": method})
	req, err := http.NewRequestWithContext(ctx, "POST", c.URL, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if resp, err := c.hc.Do(req); err == nil {
		resp.Body.Close()
	}
}

func (c *Client) Initialize(ctx context.Context) error {
	_, err := c.call(ctx, "initialize", map[string]any{
		"protocolVersion": "2024-11-05",
		"capabilities":    map[string]any{},
		"clientInfo":      map[string]any{"name": "antigravity", "version": "1.5.11"},
	})
	if err != nil {
		return err
	}
	c.notify(ctx, "notifications/initialized")
	return nil
}

func (c *Client) ListTools(ctx context.Context) ([]schema.MCPTool, error) {
	raw, err := c.call(ctx, "tools/list", map[string]any{})
	if err != nil {
		return nil, err
	}
	var r struct {
		Tools []schema.MCPTool `json:"tools"`
	}
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, err
	}
	return r.Tools, nil
}

type Content struct {
	Type     string `json:"type"`
	Text     string `json:"text,omitempty"`
	Data     string `json:"data,omitempty"`
	MimeType string `json:"mimeType,omitempty"`
}

type CallResult struct {
	Content []Content `json:"content"`
	IsError bool      `json:"isError"`
}

func (c *Client) CallTool(ctx context.Context, name string, args json.RawMessage) (*CallResult, error) {
	ctx, cancel := context.WithTimeout(ctx, CallTimeout)
	defer cancel()
	if len(args) == 0 {
		args = json.RawMessage("{}")
	}
	raw, err := c.call(ctx, "tools/call", map[string]any{"name": name, "arguments": args})
	if err != nil {
		return nil, err
	}
	var r CallResult
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, err
	}
	return &r, nil
}
