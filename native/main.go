package main

import (
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/proxy"
)

type accountPool struct {
	proxy     string
	transport *http.Transport
}

type bridge struct {
	secret string
	mu     sync.Mutex
	pools  map[string]accountPool
}

func (b *bridge) transport(account, proxyURL string) (*http.Transport, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if pool, ok := b.pools[account]; ok && pool.proxy == proxyURL {
		return pool.transport, nil
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.ForceAttemptHTTP2 = false
	tr.TLSNextProto = make(map[string]func(string, *tls.Conn) http.RoundTripper)
	tr.TLSClientConfig = &tls.Config{NextProtos: []string{"http/1.1"}}
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
	tr.MaxIdleConns, tr.MaxIdleConnsPerHost = 128, 64
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
	if old, ok := b.pools[account]; ok {
		old.transport.CloseIdleConnections()
	}
	b.pools[account] = accountPool{proxyURL, tr}
	return tr, nil
}

func cleanHeaders(headers http.Header) {
	for _, key := range strings.Split(headers.Get("Connection"), ",") {
		headers.Del(strings.TrimSpace(key))
	}
	for _, key := range []string{"Connection", "Proxy-Connection", "Keep-Alive", "Proxy-Authorization", "Te", "Trailer", "Transfer-Encoding", "Upgrade"} {
		headers.Del(key)
	}
	for key := range headers {
		if strings.HasPrefix(strings.ToLower(key), "x-gcli-") {
			headers.Del(key)
		}
	}
}

type flushWriter struct{ http.ResponseWriter }

func (w flushWriter) Write(data []byte) (int, error) {
	n, err := w.ResponseWriter.Write(data)
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
	return n, err
}

func (b *bridge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("X-Gcli-Key") != b.secret {
		http.Error(w, "Forbidden", 403)
		return
	}
	target, err := url.Parse(r.Header.Get("X-Gcli-Target"))
	if err != nil || target.Host == "" || (target.Scheme != "https" && target.Scheme != "http") {
		http.Error(w, "Invalid upstream URL", 400)
		return
	}
	tr, err := b.transport(r.Header.Get("X-Gcli-Account"), r.Header.Get("X-Gcli-Proxy"))
	if err != nil {
		http.Error(w, "Invalid account proxy or TLS configuration", 502)
		return
	}
	req, err := http.NewRequestWithContext(r.Context(), r.Method, target.String(), r.Body)
	if err != nil {
		http.Error(w, "Invalid upstream request", 400)
		return
	}
	req.Header = r.Header.Clone()
	cleanHeaders(req.Header)
	req.Header.Del("Content-Length")
	req.ContentLength = r.ContentLength
	if strings.Contains(target.Path, ":streamGenerateContent") {
		req.ContentLength = -1
	}
	resp, err := tr.RoundTrip(req)
	if err != nil {
		http.Error(w, "Account upstream connection failed", 502)
		return
	}
	defer resp.Body.Close()
	cleanHeaders(resp.Header)
	for key, values := range resp.Header {
		w.Header()[key] = values
	}
	w.WriteHeader(resp.StatusCode)
	if strings.Contains(target.Path, ":streamGenerateContent") {
		io.Copy(flushWriter{w}, resp.Body)
	} else {
		io.Copy(w, resp.Body)
	}
}

func main() {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		log.Fatal("Cannot start local HTTP transport")
	}
	var key [32]byte
	if _, err = rand.Read(key[:]); err != nil {
		log.Fatal("Cannot initialize local HTTP transport")
	}
	b := &bridge{secret: hex.EncodeToString(key[:]), pools: make(map[string]accountPool)}
	json.NewEncoder(os.Stdout).Encode(map[string]string{"url": "http://" + listener.Addr().String(), "key": b.secret})
	go func() { io.Copy(io.Discard, os.Stdin); listener.Close() }()
	server := &http.Server{Handler: b, ReadHeaderTimeout: 10 * time.Second, ErrorLog: log.New(io.Discard, "", 0)}
	server.Serve(listener)
}
