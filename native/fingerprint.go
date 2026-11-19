package main

import (
	"crypto/tls"
	"net/http"
	"slices"
)

// CloneAntigravityTransport uses the same native Go transport as agy-wire.
// Release builds pin the Go revision because it also defines the ClientHello.
func CloneAntigravityTransport(base *http.Transport) *http.Transport {
	if base == nil {
		base = http.DefaultTransport.(*http.Transport)
	}
	t := base.Clone()
	t.MaxIdleConns = 512
	t.MaxIdleConnsPerHost = 100
	t.ForceAttemptHTTP2 = false
	t.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
	if t.TLSClientConfig == nil {
		t.TLSClientConfig = &tls.Config{}
	} else {
		t.TLSClientConfig = t.TLSClientConfig.Clone()
	}
	t.TLSClientConfig.NextProtos = slices.DeleteFunc(slices.Clone(t.TLSClientConfig.NextProtos), func(p string) bool { return p == "h2" })
	return t
}
