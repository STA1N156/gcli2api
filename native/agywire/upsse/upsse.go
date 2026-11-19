package upsse

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"strings"
)

type Part struct {
	Text             string        `json:"text,omitempty"`
	Thought          bool          `json:"thought,omitempty"`
	ThoughtSignature string        `json:"thoughtSignature,omitempty"`
	FunctionCall     *FunctionCall `json:"functionCall,omitempty"`
}

type FunctionCall struct {
	Name string          `json:"name"`
	Args json.RawMessage `json:"args"`
	ID   string          `json:"id,omitempty"`
}

type Usage struct {
	PromptTokenCount        int64 `json:"promptTokenCount"`
	CandidatesTokenCount    int64 `json:"candidatesTokenCount"`
	TotalTokenCount         int64 `json:"totalTokenCount"`
	CachedContentTokenCount int64 `json:"cachedContentTokenCount"`
	ThoughtsTokenCount      int64 `json:"thoughtsTokenCount"`
}

type Chunk struct {
	Parts        []Part
	FinishReason string
	Usage        *Usage
	ModelVersion string
	ResponseID   string
	TraceID      string
}

type wireChunk struct {
	Response *struct {
		Candidates []struct {
			Content struct {
				Role  string `json:"role"`
				Parts []Part `json:"parts"`
			} `json:"content"`
			FinishReason string `json:"finishReason"`
		} `json:"candidates"`
		UsageMetadata *Usage `json:"usageMetadata"`
		ModelVersion  string `json:"modelVersion"`
		ResponseID    string `json:"responseId"`
	} `json:"response"`
	TraceID string `json:"traceId"`
}

type Reader struct {
	sc *bufio.Scanner
}

func NewReader(r io.Reader) *Reader {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	return &Reader{sc: sc}
}

func (r *Reader) Next() (*Chunk, error) {
	for r.sc.Scan() {
		line := strings.TrimRight(r.sc.Text(), "\r")
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		payload := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if payload == "" || payload == "[DONE]" {
			continue
		}
		return ParseChunk([]byte(payload))
	}
	if err := r.sc.Err(); err != nil {
		return nil, err
	}
	return nil, io.EOF
}

func ParseChunk(b []byte) (*Chunk, error) {
	var w wireChunk
	if err := json.Unmarshal(b, &w); err != nil {
		return nil, fmt.Errorf("上游 SSE 帧不是 JSON: %w", err)
	}
	if w.Response == nil {

		wrapped := append(append([]byte(`{"response":`), b...), '}')
		if err := json.Unmarshal(wrapped, &w); err != nil || w.Response == nil {
			return nil, fmt.Errorf("上游 SSE 帧缺 response")
		}
	}
	c := &Chunk{
		Usage:        w.Response.UsageMetadata,
		ModelVersion: w.Response.ModelVersion,
		ResponseID:   w.Response.ResponseID,
		TraceID:      w.TraceID,
	}
	if len(w.Response.Candidates) > 0 {
		cand := w.Response.Candidates[0]
		c.Parts = cand.Content.Parts
		c.FinishReason = cand.FinishReason
	}
	return c, nil
}
