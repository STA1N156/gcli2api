package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"strconv"
	"strings"
	"time"

	"gcli2api/agy-http/agywire/transport"
	"gcli2api/agy-http/agywire/upsse"
)

const Endpoint = "https://daily-cloudcode-pa.googleapis.com"

type APIError struct {
	HTTPStatus int
	Status     string
	Message    string
	Reason     string
	RetryDelay time.Duration
	RetryAfter time.Duration
	Raw        string

	QuotaResetAt string
	QuotaModel   string
}

func (e *APIError) GatewayHint() string {
	if e.QuotaResetAt == "" && e.QuotaModel == "" {
		return ""
	}
	return fmt.Sprintf(" [quotaResetTimeStamp=%s model=%s]", e.QuotaResetAt, e.QuotaModel)
}

func (e *APIError) Error() string {
	return fmt.Sprintf("%s (code %d): %s", e.Status, e.HTTPStatus, e.Message)
}

func (e *APIError) Retryable() bool {
	switch e.Reason {
	case "MONETIZATION_DENIED", "INSUFFICIENT_G1_CREDITS_BALANCE", "QUOTA_EXHAUSTED":
		return false
	}
	switch e.HTTPStatus {
	case 429, 502, 503, 504:
		return true
	}
	return false
}

func parseAPIError(resp *http.Response) *APIError {
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	e := &APIError{HTTPStatus: resp.StatusCode, Status: http.StatusText(resp.StatusCode), Message: strings.TrimSpace(string(body)), Raw: string(body)}
	var w struct {
		Error struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
			Status  string `json:"status"`
			Details []struct {
				Type       string            `json:"@type"`
				Reason     string            `json:"reason"`
				RetryDelay string            `json:"retryDelay"`
				Metadata   map[string]string `json:"metadata"`
			} `json:"details"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &w) == nil && w.Error.Status != "" {
		e.Status, e.Message = w.Error.Status, w.Error.Message
		for _, d := range w.Error.Details {
			if d.Reason != "" {
				e.Reason = d.Reason
			}
			if v := d.Metadata["quotaResetTimeStamp"]; v != "" {
				e.QuotaResetAt = v
			}
			if v := d.Metadata["model"]; v != "" {
				e.QuotaModel = v
			}
			if d.RetryDelay != "" {
				if dur, err := time.ParseDuration(d.RetryDelay); err == nil {
					e.RetryDelay = dur
				}
			}
		}
	}
	if ra := resp.Header.Get("Retry-After"); ra != "" {
		if secs, err := strconv.Atoi(ra); err == nil {
			e.RetryAfter = time.Duration(secs) * time.Second
		}
	}
	return e
}

const (
	retryInitial    = 4 * time.Second
	retryMultiplier = 1.8
	retryMax        = 60 * time.Second
	retryMaxRetries = 6
)

func backoff(attempt int) time.Duration {
	d := float64(retryInitial)
	for i := 1; i < attempt; i++ {
		d *= retryMultiplier
	}
	if d > float64(retryMax) {
		d = float64(retryMax)
	}

	d *= 0.85 + 0.3*rand.Float64()
	return time.Duration(d)
}

func (d *Daemon) streamGenerate(ctx context.Context, body []byte) (*upsse.Reader, io.Closer, error) {
	token, err := d.tokens.AccessToken()
	if err != nil {
		return nil, nil, err
	}
	client, err := d.client()
	if err != nil {
		return nil, nil, err
	}

	resp, err := transport.DoStream(ctx, client, transport.Spec{
		Method: "POST", URL: Endpoint + "/v1internal:streamGenerateContent?alt=sse",
		Body: body, Mode: transport.BodyChunked, BearerToken: token,
		UserAgent: transport.UserAgent, ContentType: "application/json",
	}, transport.StreamIdleTimeout)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode != http.StatusOK {
		defer resp.Body.Close()
		return nil, nil, parseAPIError(resp)
	}
	return upsse.NewReader(resp.Body), resp.Body, nil
}

func (d *Daemon) controlCall(ctx context.Context, method, url string, body []byte, withUA bool) ([]byte, int, error) {
	token, err := d.tokens.AccessToken()
	if err != nil {
		return nil, 0, err
	}
	client, err := d.client()
	if err != nil {
		return nil, 0, err
	}
	spec := transport.Spec{Method: method, URL: url, BearerToken: token}
	if withUA {
		spec.UserAgent = transport.UserAgent
	}
	if body != nil {
		spec.Body, spec.Mode, spec.ContentType = body, transport.BodyLength, "application/json"
	}
	req, err := transport.Build(ctx, spec)
	if err != nil {
		return nil, 0, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	return b, resp.StatusCode, err
}
