// Package signature implements the reference gateway's account-bound wire envelope.
package signature

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"strings"
)

const Prefix = "agyw1."

type CallMeta struct {
	Summary string `json:"s,omitempty"`
	GemID   string `json:"g,omitempty"`
}

type Payload struct {
	Signature string              `json:"s,omitempty"`
	Calls     map[string]CallMeta `json:"m,omitempty"`
}

func Key(refreshToken string) [32]byte {
	return sha256.Sum256([]byte("caio-agy-wire-sig-v1\x00" + refreshToken))
}

func Wrap(key [32]byte, p Payload) string {
	raw, _ := json.Marshal(p)
	body := base64.RawURLEncoding.EncodeToString(raw)
	mac := hmac.New(sha256.New, key[:])
	_, _ = mac.Write([]byte(body))
	return Prefix + body + "." + base64.RawURLEncoding.EncodeToString(mac.Sum(nil)[:16])
}

func Unwrap(key [32]byte, value string) (Payload, bool) {
	var p Payload
	if !strings.HasPrefix(value, Prefix) {
		return p, false
	}
	body, tag, ok := strings.Cut(strings.TrimPrefix(value, Prefix), ".")
	if !ok {
		return p, false
	}
	got, err := base64.RawURLEncoding.DecodeString(tag)
	if err != nil || len(got) != 16 {
		return p, false
	}
	mac := hmac.New(sha256.New, key[:])
	_, _ = mac.Write([]byte(body))
	if !hmac.Equal(got, mac.Sum(nil)[:16]) {
		return p, false
	}
	raw, err := base64.RawURLEncoding.DecodeString(body)
	if err != nil || json.Unmarshal(raw, &p) != nil {
		return Payload{}, false
	}
	return p, true
}
