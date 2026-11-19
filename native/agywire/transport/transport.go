package transport

import (
	"bytes"
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"slices"
	"sync"
)

const UserAgent = "antigravity/cli/1.5.11 (aidev_client; os_type=linux; arch=amd64; cl=1024248524; auth_method=consumer; datacollection_opt_in=false; host_path=acp-client/1.0.0)"

var ErrNoProxy = errors.New("账号没有出口代理,拒绝直连")

func newCloudCodeTransport(proxy *url.URL) *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConns = 512
	t.MaxIdleConnsPerHost = 100
	t.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
	if t.TLSClientConfig == nil {
		t.TLSClientConfig = &tls.Config{}
	}
	t.TLSClientConfig.NextProtos = slices.DeleteFunc(slices.Clone(t.TLSClientConfig.NextProtos),
		func(p string) bool { return p == "h2" })
	if proxy != nil {
		t.Proxy = http.ProxyURL(proxy)
	} else {
		t.Proxy = nil
	}
	return t
}

type Pool struct {
	mu      sync.Mutex
	clients map[string]*http.Client
}

func NewPool() *Pool { return &Pool{clients: map[string]*http.Client{}} }

func (p *Pool) Client(proxyURL string) (*http.Client, error) {
	if proxyURL == "" {
		return nil, ErrNoProxy
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if c, ok := p.clients[proxyURL]; ok {
		return c, nil
	}
	u, err := url.Parse(proxyURL)
	if err != nil || u.Host == "" {
		return nil, fmt.Errorf("出口代理地址无效: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" && u.Scheme != "socks5" && u.Scheme != "socks5h" {
		return nil, fmt.Errorf("不支持的代理协议 %q", u.Scheme)
	}
	c := &http.Client{Transport: newCloudCodeTransport(u)}
	p.clients[proxyURL] = c
	return c, nil
}

type BodyMode int

const (
	BodyNone BodyMode = iota

	BodyLength

	BodyChunked
)

type Spec struct {
	Method      string
	URL         string
	Body        []byte
	Mode        BodyMode
	BearerToken string
	UserAgent   string
	ContentType string
}

func Build(ctx context.Context, s Spec) (*http.Request, error) {
	var body io.Reader
	switch s.Mode {
	case BodyLength:
		body = bytes.NewReader(s.Body)
	case BodyChunked:

		body = io.NopCloser(bytes.NewReader(s.Body))
	}
	req, err := http.NewRequestWithContext(ctx, s.Method, s.URL, body)
	if err != nil {
		return nil, err
	}
	if s.Mode == BodyChunked {
		req.ContentLength = -1
	}
	if s.UserAgent != "" {
		req.Header.Set("User-Agent", s.UserAgent)
	}
	if s.BearerToken != "" {
		req.Header.Set("Authorization", "Bearer "+s.BearerToken)
	}
	if s.ContentType != "" {
		req.Header.Set("Content-Type", s.ContentType)
	}
	return req, nil
}

func ClientFor(proxyURL string) (*http.Client, error) {
	if proxyURL == "" {
		return &http.Client{Transport: newCloudCodeTransport(nil)}, nil
	}
	return NewPool().Client(proxyURL)
}
