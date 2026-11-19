package acp

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"sync"
)

type Request struct {
	ID     json.RawMessage
	Method string
	Params json.RawMessage
}

type inbound struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *RPCError       `json:"error,omitempty"`
}

type RPCError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *RPCError) Error() string { return fmt.Sprintf("rpc error %d: %s", e.Code, e.Message) }

type Conn struct {
	mu      sync.Mutex
	w       *bufio.Writer
	nextID  int64
	pending map[string]chan inbound
	pmu     sync.Mutex
}

func NewConn(w io.Writer) *Conn {
	return &Conn{w: bufio.NewWriterSize(w, 64*1024), pending: map[string]chan inbound{}}
}

func (c *Conn) writeLine(b []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := c.w.Write(b); err != nil {
		return err
	}
	if err := c.w.WriteByte('\n'); err != nil {
		return err
	}
	return c.w.Flush()
}

func marshal(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		return json.RawMessage("null")
	}
	return b
}

func (c *Conn) Reply(id json.RawMessage, result any) error {
	line := fmt.Sprintf(`{"jsonrpc":"2.0","result":%s,"id":%s}`, marshal(result), id)
	return c.writeLine([]byte(line))
}

func (c *Conn) ReplyError(id json.RawMessage, code int, msg string) error {
	line := fmt.Sprintf(`{"jsonrpc":"2.0","error":%s,"id":%s}`, marshal(RPCError{Code: code, Message: msg}), id)
	return c.writeLine([]byte(line))
}

func (c *Conn) Notify(method string, params any) error {
	line := fmt.Sprintf(`{"jsonrpc":"2.0","method":%s,"params":%s}`, marshal(method), marshal(params))
	return c.writeLine([]byte(line))
}

func (c *Conn) Call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	c.pmu.Lock()
	c.nextID++
	id := strconv.FormatInt(c.nextID, 10)
	ch := make(chan inbound, 1)
	c.pending[id] = ch
	c.pmu.Unlock()
	defer func() {
		c.pmu.Lock()
		delete(c.pending, id)
		c.pmu.Unlock()
	}()
	line := fmt.Sprintf(`{"jsonrpc":"2.0","method":%s,"params":%s,"id":%s}`, marshal(method), marshal(params), marshal(id))
	if err := c.writeLine([]byte(line)); err != nil {
		return nil, err
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case resp := <-ch:
		if resp.Error != nil {
			return nil, resp.Error
		}
		return resp.Result, nil
	}
}

func (c *Conn) Serve(r io.Reader, handle func(Request)) error {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 1<<20), 256<<20)
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var m inbound
		if err := json.Unmarshal(line, &m); err != nil {
			continue
		}
		if m.Method == "" && len(m.ID) > 0 {
			var sid string
			if err := json.Unmarshal(m.ID, &sid); err != nil {
				sid = string(m.ID)
			}
			c.pmu.Lock()
			ch := c.pending[sid]
			c.pmu.Unlock()
			if ch != nil {
				ch <- m
			}
			continue
		}
		req := Request{ID: m.ID, Method: m.Method, Params: m.Params}
		go handle(req)
	}
	if err := sc.Err(); err != nil {
		return err
	}
	return io.EOF
}

var ErrMethodNotFound = errors.New("method not found")
