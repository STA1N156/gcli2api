package agent

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"gcli2api/agy-http/agywire/ids"
)

func (e *Embedded) Catalog(ctx context.Context) (*Catalog, error) { return e.d.Catalog(ctx) }

// Image requests share the account transport, token source, catalog and cancellation.
func (e *Embedded) imageStream(ctx context.Context, payload []byte, model string, emit func([]byte) error) error {
	cat, err := e.d.Catalog(ctx)
	if err != nil {
		return err
	}
	if _, ok := cat.Models[model]; !ok {
		return fmt.Errorf("unknown model: %s", model)
	}
	var input struct {
		Project string         `json:"project"`
		Request map[string]any `json:"request"`
	}
	if err = json.Unmarshal(payload, &input); err != nil {
		return err
	}
	if input.Project == "" {
		input.Project = Project
	}
	for _, k := range []string{"tools", "toolConfig", "systemInstruction", "safetySettings"} {
		delete(input.Request, k)
	}
	// Struct fields retain the reference envelope order.
	body, err := json.Marshal(struct {
		Project     string         `json:"project"`
		RequestID   string         `json:"requestId"`
		Request     map[string]any `json:"request"`
		Model       string         `json:"model"`
		UserAgent   string         `json:"userAgent"`
		RequestType string         `json:"requestType"`
	}{input.Project, fmt.Sprintf("imagegen/%s/%d", ids.UUID(), time.Now().UnixMilli()), input.Request, model, "antigravity", "image_gen"})
	if err != nil {
		return err
	}
	_, closer, err := e.d.streamGenerate(ctx, body)
	if err != nil {
		return err
	}
	defer closer.Close()
	// Preserve inlineData verbatim instead of passing images through the text/tool decoder.
	reader, ok := closer.(interface{ Read([]byte) (int, error) })
	if !ok {
		return fmt.Errorf("invalid image stream")
	}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64<<10), 64<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "" || data == "[DONE]" {
			continue
		}
		if !json.Valid([]byte(data)) {
			return fmt.Errorf("invalid image response")
		}
		var envelope map[string]json.RawMessage
		_ = json.Unmarshal([]byte(data), &envelope)
		chunk := []byte(data)
		if envelope["response"] == nil {
			chunk, _ = json.Marshal(map[string]json.RawMessage{"response": chunk})
		}
		if err = emit(chunk); err != nil {
			return err
		}
	}
	return scanner.Err()
}
