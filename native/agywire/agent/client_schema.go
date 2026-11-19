package agent

import (
	"crypto/sha256"
	"fmt"
	"strings"
)

func safeToolName(original string, used map[string]bool) string {
	var b strings.Builder
	for _, c := range original {
		if c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-' || c == '.' {
			b.WriteRune(c)
		} else {
			b.WriteByte('_')
		}
	}
	name := b.String()
	if name == "" || !(name[0] >= 'a' && name[0] <= 'z' || name[0] >= 'A' && name[0] <= 'Z' || name[0] == '_') {
		name = "t" + name
	}
	if len(name) > 48 {
		sum := sha256.Sum256([]byte(original))
		name = fmt.Sprintf("%s_%x", name[:40], sum[:3])
	}
	base := name
	for n := 2; used[name]; n++ {
		name = fmt.Sprintf("%s_%d", base, n)
	}
	used[name] = true
	return name
}

func cleanClientSchema(value any, root bool) map[string]any {
	in, ok := value.(map[string]any)
	if !ok {
		if root {
			return map[string]any{"type": "object", "properties": map[string]any{}}
		}
		return map[string]any{"type": "string"}
	}
	out := map[string]any{}
	for k, v := range in {
		switch k {
		case "$schema", "$id", "$defs", "definitions", "additionalProperties", "unevaluatedProperties", "propertyNames", "patternProperties", "examples", "title", "$ref", "$comment", "not", "if", "then", "else", "dependentSchemas", "dependentRequired":
			continue
		case "properties":
			if properties, ok := v.(map[string]any); ok {
				clean := map[string]any{}
				for name, s := range properties {
					clean[name] = cleanClientSchema(s, false)
				}
				out[k] = clean
			}
			continue
		case "items":
			if a, ok := v.([]any); ok {
				if len(a) > 0 {
					v = a[0]
				} else {
					continue
				}
			}
			out[k] = cleanClientSchema(v, false)
			continue
		case "enum":
			if a, ok := v.([]any); ok {
				var clean []any
				for _, x := range a {
					if x == nil {
						out["nullable"] = true
					}
					if s, ok := x.(string); ok && s != "" {
						clean = append(clean, s)
					}
				}
				if len(clean) > 0 {
					out[k] = clean
				}
			}
			continue
		case "const":
			if s, ok := v.(string); ok && s != "" {
				if _, exists := in["enum"]; !exists {
					out["enum"] = []any{s}
				}
			}
			continue
		case "default":
			if v == nil {
				continue
			}
		case "type":
			if a, ok := v.([]any); ok {
				for _, x := range a {
					if x == "null" {
						out["nullable"] = true
					} else if _, exists := out[k]; !exists {
						out[k] = x
					}
				}
				continue
			}
		case "anyOf", "oneOf", "allOf":
			if a, ok := v.([]any); ok {
				var clean []any
				for _, x := range a {
					m, _ := x.(map[string]any)
					if m["type"] == "null" {
						out["nullable"] = true
						continue
					}
					clean = append(clean, cleanClientSchema(x, false))
				}
				if len(clean) == 1 {
					for name, x := range clean[0].(map[string]any) {
						if _, exists := in[name]; !exists {
							out[name] = x
						}
					}
				} else if len(clean) > 0 {
					out["anyOf"] = clean
				}
			}
			continue
		}
		out[k] = v
	}
	if root {
		out["type"] = "object"
		if _, ok := out["properties"].(map[string]any); !ok {
			out["properties"] = map[string]any{}
		}
	}
	if required, ok := out["required"].([]any); ok {
		props, _ := out["properties"].(map[string]any)
		var kept []any
		for _, x := range required {
			if name, ok := x.(string); ok {
				if _, exists := props[name]; exists {
					kept = append(kept, x)
				}
			}
		}
		if len(kept) > 0 {
			out["required"] = kept
		} else {
			delete(out, "required")
		}
	}
	kind, _ := out["type"].(string)
	if strings.EqualFold(kind, "array") && out["items"] == nil {
		out["items"] = map[string]any{"type": "string"}
	}
	if out["type"] == nil && out["anyOf"] == nil {
		if out["properties"] != nil {
			out["type"] = "object"
		} else {
			out["type"] = "string"
		}
	}
	return out
}
