package agent

import (
	"encoding/json"
	"strings"
)

// Resolve legacy client aliases only to entries present in this account's catalog.
func resolveClientModel(c *Catalog, p *wireChatParams) (ModelInfo, bool) {
	if m, ok := c.Models[p.Model]; ok {
		return m, true
	}
	candidates := []string{strings.TrimSuffix(p.Model, "-thinking"), p.Model + "-thinking"}
	for _, suffix := range []string{"-low", "-medium", "-high", "-minimal", "-max", "-xhigh"} {
		if strings.HasSuffix(p.Model, suffix) {
			base := strings.TrimSuffix(p.Model, suffix)
			if strings.HasSuffix(base, "-flash-lite") {
				candidates = append(candidates, base)
			}
			if suffix == "-max" || suffix == "-xhigh" {
				candidates = append(candidates, base+"-high")
			}
		}
	}
	if p.Model == "gemini-pro-agent" {
		candidates = append(candidates, "gemini-3.1-pro-high")
	}
	switch p.Model {
	case "gemini-3.8-flash", "gemini-4-carbon", "gemini-4-argon", "gemini-4-hydrogen", "gemini-4-neon":
		var thinking struct {
			Level string `json:"thinkingLevel"`
		}
		_ = json.Unmarshal(p.ThinkingConfig, &thinking)
		level := strings.ToLower(thinking.Level)
		if level != "low" && level != "medium" && level != "xhigh" {
			level = "high"
		}
		candidates = append(candidates, p.Model+"-"+level)
	}
	for _, id := range candidates {
		if m, ok := c.Models[id]; ok {
			p.Model = id
			return m, true
		}
	}
	return ModelInfo{}, false
}
