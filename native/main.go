package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"sync"
	"time"

	"golang.org/x/net/proxy"
)

type accountPool struct {
	proxy     string
	transport *http.Transport
}

type bridge struct {
	mu    sync.Mutex
	pools sync.Map
	wires sync.Map
	ctx   context.Context
}

func (b *bridge) transport(account, proxyURL string) (*http.Transport, error) {
	if value, ok := b.pools.Load(account); ok {
		pool := value.(accountPool)
		if pool.proxy == proxyURL {
			return pool.transport, nil
		}
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if value, ok := b.pools.Load(account); ok {
		pool := value.(accountPool)
		if pool.proxy == proxyURL {
			return pool.transport, nil
		}
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.ForceAttemptHTTP2 = false
	tr.TLSNextProto = make(map[string]func(string, *tls.Conn) http.RoundTripper)
	tr.TLSClientConfig = &tls.Config{}
	if path := os.Getenv("AGY_CA_CERT"); path != "" {
		pem, err := os.ReadFile(path)
		if err != nil {
			return nil, err
		}
		roots, err := x509.SystemCertPool()
		if err != nil {
			return nil, err
		}
		if !roots.AppendCertsFromPEM(pem) {
			return nil, os.ErrInvalid
		}
		tr.TLSClientConfig.RootCAs = roots
	}
	tr.MaxIdleConns, tr.MaxIdleConnsPerHost = 128, 100
	tr.IdleConnTimeout = 90 * time.Second
	if proxyURL == "direct" {
		tr.Proxy = nil
	} else if proxyURL != "" {
		parsed, err := url.Parse(proxyURL)
		if err != nil {
			return nil, err
		}
		switch parsed.Scheme {
		case "http", "https":
			tr.Proxy = http.ProxyURL(parsed)
		case "socks5", "socks5h":
			parsed.Scheme = "socks5"
			dialer, err := proxy.FromURL(parsed, &net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second})
			if err != nil {
				return nil, err
			}
			contextDialer, ok := dialer.(proxy.ContextDialer)
			if !ok {
				return nil, os.ErrInvalid
			}
			tr.Proxy, tr.DialContext = nil, contextDialer.DialContext
		default:
			return nil, os.ErrInvalid
		}
	}
	tr = CloneAntigravityTransport(tr)
	if old, ok := b.pools.Load(account); ok {
		old.(accountPool).transport.CloseIdleConnections()
	}
	b.pools.Store(account, accountPool{proxyURL, tr})
	return tr, nil
}

func main() {
	if err := runServer(); err != nil {
		log.Fatal(err)
	}
}
