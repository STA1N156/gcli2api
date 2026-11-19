package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"

	"gcli2api/agy-http/httpwire"
	"gcli2api/agy-http/proxyutil"
	utls "github.com/refraction-networking/utls"
	"golang.org/x/net/proxy"
)

func CloneAntigravityTransport(base *http.Transport) *http.Transport {
	if base == nil {
		base, _ = http.DefaultTransport.(*http.Transport)
	}
	if base == nil {
		base = &http.Transport{}
	}
	transport := base.Clone()
	transport.ForceAttemptHTTP2 = false
	transport.TLSNextProto = make(map[string]func(string, *tls.Conn) http.RoundTripper)
	if transport.TLSClientConfig == nil {
		transport.TLSClientConfig = &tls.Config{}
	} else {
		transport.TLSClientConfig = transport.TLSClientConfig.Clone()
	}
	transport.TLSClientConfig.NextProtos = []string{"http/1.1"}
	// Keep explicitly supplied TLS dialers, including already configured clones.
	if base.DialTLSContext != nil {
		return transport
	}
	dialContext, proxyForRequest := transport.DialContext, transport.Proxy
	// Tunnel proxies before TLS so the upstream always sees the same profile.
	transport.Proxy = nil
	dial := func(ctx context.Context, network, address, scheme string) (net.Conn, error) {
		if proxyForRequest != nil {
			proxyURL, err := proxyForRequest((&http.Request{URL: &url.URL{Scheme: scheme, Host: address}}).WithContext(ctx))
			if err != nil {
				return nil, err
			}
			if proxyURL != nil {
				dialer, _, err := proxyutil.BuildDialer(proxyURL.String())
				if err != nil {
					return nil, err
				}
				if contextual, ok := dialer.(proxy.ContextDialer); ok {
					return contextual.DialContext(ctx, network, address)
				}
				return nil, fmt.Errorf("antigravity: proxy dialer does not support context")
			}
		}
		if dialContext != nil {
			return dialContext(ctx, network, address)
		}
		return (&net.Dialer{}).DialContext(ctx, network, address)
	}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		conn, err := dial(ctx, network, address, "http")
		if err != nil {
			return nil, err
		}
		return httpwire.NewOrderedRequestConn(conn, antigravityHeaderOrder), nil
	}
	transport.DialTLSContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		conn, err := dial(ctx, network, address, "https")
		if err != nil {
			return nil, err
		}
		host, _, err := net.SplitHostPort(address)
		if err != nil {
			_ = conn.Close()
			return nil, err
		}
		baseConfig := transport.TLSClientConfig
		if baseConfig.ServerName != "" {
			host = baseConfig.ServerName
		}
		config := &utls.Config{ServerName: host, RootCAs: baseConfig.RootCAs, InsecureSkipVerify: baseConfig.InsecureSkipVerify, VerifyPeerCertificate: baseConfig.VerifyPeerCertificate}
		if verify := baseConfig.VerifyConnection; verify != nil {
			config.VerifyConnection = func(state utls.ConnectionState) error {
				return verify(tls.ConnectionState{Version: state.Version, HandshakeComplete: state.HandshakeComplete, DidResume: state.DidResume, CipherSuite: state.CipherSuite, NegotiatedProtocol: state.NegotiatedProtocol, ServerName: state.ServerName, PeerCertificates: state.PeerCertificates, VerifiedChains: state.VerifiedChains})
			}
		}
		secured := utls.UClient(conn, config, utls.HelloCustom)
		if err = secured.ApplyPreset(antigravityOpenSSLHello()); err == nil {
			err = secured.HandshakeContext(ctx)
		}
		if err != nil {
			_ = conn.Close()
			return nil, fmt.Errorf("antigravity TLS: %w", err)
		}
		return httpwire.NewOrderedRequestConn(secured, antigravityHeaderOrder), nil
	}
	return transport
}

var antigravityHeaders = []string{"Host", "Connection", "User-Agent", "Authorization", "Content-Type", "Accept", "Accept-Encoding", "Content-Length"}
var antigravityOAuthHeaders = []string{"Host", "Accept", "Accept-Encoding", "Connection", "User-Agent", "Content-Type", "Authorization", "Content-Length"}

func antigravityHeaderOrder(_, target string) []string {
	if strings.HasPrefix(target, "/token") || strings.HasPrefix(target, "/oauth2/v2/userinfo") {
		return antigravityOAuthHeaders
	}
	return antigravityHeaders
}

// Match httpx 0.28's default OpenSSL 3 ClientHello used by agy2api.
func antigravityOpenSSLHello() *utls.ClientHelloSpec {
	return &utls.ClientHelloSpec{
		TLSVersMin: utls.VersionTLS12, TLSVersMax: utls.VersionTLS13,
		CipherSuites:       []uint16{4866, 4867, 4865, 49196, 49200, 49195, 49199, 52393, 52392, 49188, 49192, 49187, 49191, 159, 158, 107, 103, 255},
		CompressionMethods: []byte{0},
		Extensions: []utls.TLSExtension{
			&utls.SNIExtension{},
			&utls.SupportedPointsExtension{SupportedPoints: []byte{0, 1, 2}},
			&utls.SupportedCurvesExtension{Curves: []utls.CurveID{29, 23, 30, 25, 24, 256, 257, 258, 259, 260}},
			&utls.SessionTicketExtension{},
			&utls.ALPNExtension{AlpnProtocols: []string{"http/1.1"}},
			&utls.GenericExtension{Id: 22},
			&utls.ExtendedMasterSecretExtension{},
			&utls.SignatureAlgorithmsExtension{SupportedSignatureAlgorithms: []utls.SignatureScheme{1027, 1283, 1539, 2055, 2056, 2057, 2058, 2059, 2052, 2053, 2054, 1025, 1281, 1537, 771, 769, 770, 1026, 1282, 1538}},
			&utls.SupportedVersionsExtension{Versions: []uint16{utls.VersionTLS13, utls.VersionTLS12}},
			&utls.PSKKeyExchangeModesExtension{Modes: []uint8{1}},
			&utls.KeyShareExtension{KeyShares: []utls.KeyShare{{Group: utls.X25519}}},
			&utls.UtlsPaddingExtension{GetPaddingLen: utls.BoringPaddingStyle},
		},
	}
}
