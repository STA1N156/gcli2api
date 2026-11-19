package schema

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	"gcli2api/agy-http/agywire/pj"
)

const ToolSummaryDescription = "Brief 2-5 word noun phrase describing the specific task. Capitalize like a sentence. Some examples: 'Directory analysis', 'Web search', 'Git status check', 'Test execution', 'Code search'."

type MCPTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
}

func DeclName(server, tool string) string { return "mcp_" + server + "_" + tool }

func WriteToolElement(w *pj.Buf, server string, t MCPTool) error {
	var root map[string]any
	if len(t.InputSchema) > 0 {
		dec := json.NewDecoder(bytes.NewReader(t.InputSchema))
		dec.UseNumber()
		if err := dec.Decode(&root); err != nil {
			return fmt.Errorf("工具 %s 的 inputSchema 不是对象: %w", t.Name, err)
		}
	}
	if root == nil {
		root = map[string]any{"type": "object"}
	}
	root = withToolSummary(root)
	w.ObjStart().Key("functionDeclarations").ArrStart().ObjStart()
	w.Key("name").Str(DeclName(server, t.Name))
	w.Key("description").Str("This is a tool from the " + server + " MCP server.\n" + t.Description)
	w.Key("parameters")
	if err := writeSchema(w, root); err != nil {
		return fmt.Errorf("工具 %s: %w", t.Name, err)
	}
	w.ObjEnd().ArrEnd().ObjEnd()
	return nil
}

func withToolSummary(root map[string]any) map[string]any {
	out := make(map[string]any, len(root)+2)
	for k, v := range root {
		out[k] = v
	}
	props := map[string]any{}
	if p, ok := root["properties"].(map[string]any); ok {
		for k, v := range p {
			props[k] = v
		}
	}
	props["toolSummary"] = map[string]any{"type": "string", "description": ToolSummaryDescription}
	out["properties"] = props
	var req []any
	if r, ok := root["required"].([]any); ok {
		req = append(req, r...)
	}
	out["required"] = append(req, "toolSummary")
	if _, ok := out["type"]; !ok {
		out["type"] = "object"
	}
	return out
}

var protoOrder = []string{
	"type", "format", "title", "description", "nullable", "default", "items",
	"minItems", "maxItems", "enum", "properties", "propertyOrdering", "required",
	"minProperties", "maxProperties", "minimum", "maximum", "minLength", "maxLength",
	"pattern", "example", "anyOf", "additionalProperties", "ref", "defs",
}

var int64Fields = map[string]bool{
	"minItems": true, "maxItems": true, "minProperties": true, "maxProperties": true,
	"minLength": true, "maxLength": true,
}

func writeSchema(w *pj.Buf, s map[string]any) error {
	w.ObjStart()
	for _, k := range protoOrder {
		v, ok := s[k]
		if !ok || v == nil {
			continue
		}
		switch k {
		case "type":
			t, ok := v.(string)
			if !ok {
				return fmt.Errorf("type 不是字符串: %v", v)
			}
			w.Key("type").Str(strings.ToUpper(t))
		case "format", "title", "description", "pattern", "ref":
			if str, ok := v.(string); ok && str != "" {
				w.Key(k).Str(str)
			}
		case "nullable":
			if b, ok := v.(bool); ok && b {
				w.Key(k).Bool(true)
			}
		case "default", "example":
			w.Key(k)
			WriteValue(w, v)
		case "items":
			if m, ok := v.(map[string]any); ok {
				w.Key(k)
				if err := writeSchema(w, m); err != nil {
					return err
				}
			}
		case "properties", "defs":
			m, ok := v.(map[string]any)
			if !ok {
				continue
			}
			w.Key(k).ObjStart()
			for _, pk := range pj.SortedKeys(m) {
				sub, ok := m[pk].(map[string]any)
				if !ok {
					continue
				}
				w.Key(pk)
				if err := writeSchema(w, sub); err != nil {
					return err
				}
			}
			w.ObjEnd()
		case "anyOf":
			arr, ok := v.([]any)
			if !ok {
				continue
			}
			w.Key(k).ArrStart()
			for _, it := range arr {
				if m, ok := it.(map[string]any); ok {
					if err := writeSchema(w, m); err != nil {
						return err
					}
				}
			}
			w.ArrEnd()
		case "enum", "required", "propertyOrdering":
			arr, ok := v.([]any)
			if !ok {
				continue
			}
			w.Key(k).ArrStart()
			for _, it := range arr {
				w.Str(fmt.Sprint(it))
			}
			w.ArrEnd()
		case "minimum", "maximum":

			if f, ok := toFloat(v); ok && f != 0 {
				w.Key(k).Double(f)
			}
		case "additionalProperties":

			w.Key(k)
			WriteValue(w, v)
		default:
			if int64Fields[k] {
				if f, ok := toFloat(v); ok && f != 0 {
					w.Key(k).Int64(int64(f))
				}
			}
		}
	}
	w.ObjEnd()
	return nil
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	case float64:
		return n, true
	}
	return 0, false
}

func WriteValue(w *pj.Buf, v any) {
	switch x := v.(type) {
	case nil:
		w.Raw([]byte("null"))
	case bool:
		w.Bool(x)
	case string:
		w.Str(x)
	case json.Number:
		f, _ := x.Float64()
		w.Double(f)
	case float64:
		w.Double(x)
	case []any:
		w.ArrStart()
		for _, it := range x {
			WriteValue(w, it)
		}
		w.ArrEnd()
	case map[string]any:
		w.ObjStart()
		for _, k := range pj.SortedKeys(x) {
			w.Key(k)
			WriteValue(w, x[k])
		}
		w.ObjEnd()
	}
}
