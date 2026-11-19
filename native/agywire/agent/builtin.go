package agent

import (
	"encoding/json"
	"strings"

	"gcli2api/agy-http/agywire/schema"
)

type builtinSpec struct {
	name                     string
	keys                     map[string]string
	questions, objectOptions bool
}

type builtinMapper struct {
	tools map[string]builtinSpec
	home  string
}

var builtinAliases = map[string]string{
	"bash": "run_command", "shell": "run_command", "run_command": "run_command", "run_terminal_cmd": "run_command", "run_terminal_command": "run_command", "execute_command": "run_command", "exec_command": "run_command", "terminal": "run_command",
	"read": "read_file", "read_file": "read_file", "view_file": "read_file", "cat_file": "read_file", "open_file": "read_file",
	"write": "write_file", "write_file": "write_file", "write_to_file": "write_file", "create_file": "write_file", "save_file": "write_file",
	"edit": "edit_file", "edit_file": "edit_file", "replace_in_file": "edit_file", "str_replace": "edit_file", "search_replace": "edit_file",
	"grep": "grep_search", "grep_search": "grep_search", "ripgrep": "grep_search", "search_files": "grep_search",
	"glob": "find_by_name", "find_by_name": "find_by_name", "file_search": "find_by_name", "find_files": "find_by_name",
	"ls": "list_dir", "list": "list_dir", "list_dir": "list_dir", "list_directory": "list_dir", "list_files": "list_dir",
	"askuserquestion": "ask_question", "question": "ask_question", "ask_user_question": "ask_question", "ask_question": "ask_question", "ask_followup_question": "ask_question",
	"websearch": "search_web", "web_search": "search_web", "search_web": "search_web", "google_search": "search_web",
}

func newBuiltinMapper(tools []schema.MCPTool, home string) builtinMapper {
	m := builtinMapper{tools: map[string]builtinSpec{}, home: strings.TrimRight(home, "/")}
	for _, tool := range tools {
		kind := builtinAliases[strings.ToLower(tool.Name)]
		if kind == "" {
			continue
		}
		if _, exists := m.tools[kind]; exists {
			continue
		}
		var s struct {
			Properties map[string]json.RawMessage `json:"properties"`
		}
		_ = json.Unmarshal(tool.InputSchema, &s)
		b := builtinSpec{name: tool.Name, keys: map[string]string{}}
		pick := func(slot, fallback string, candidates ...string) {
			b.keys[slot] = fallback
			for _, k := range candidates {
				if _, ok := s.Properties[k]; ok {
					b.keys[slot] = k
					break
				}
			}
		}
		if kind == "list_dir" {
			pick("path", "path", "path", "target_directory", "directory", "DirectoryPath", "relative_workspace_path", "dir")
		} else {
			pick("path", "file_path", "file_path", "filePath", "path", "target_file", "Path", "filename")
		}
		switch kind {
		case "run_command":
			pick("cmd", "command", "command", "cmd", "CommandLine", "script")
			pick("desc", "", "description", "toolSummary", "explanation")
		case "read_file":
			pick("offset", "", "offset", "start_line_one_indexed")
			pick("limit", "", "limit", "max_lines")
			pick("start", "", "start_line", "StartLine")
			pick("end", "", "end_line", "EndLine")
		case "write_file":
			pick("content", "content", "content", "contents", "Content", "file_text", "text")
		case "edit_file":
			pick("old", "old_string", "old_string", "oldString", "old_str", "target", "Target", "search")
			pick("new", "new_string", "new_string", "newString", "new_str", "replacement", "Replacement", "replace")
		case "grep_search":
			pick("pattern", "pattern", "pattern", "query", "Query", "regex")
			pick("path", "", "path", "SearchPath", "directory", "dir")
			pick("glob", "", "glob", "include", "file_pattern")
			pick("case", "", "-i", "case_insensitive", "CaseInsensitive")
		case "find_by_name":
			pick("pattern", "pattern", "pattern", "Pattern", "glob_pattern", "query")
			pick("path", "", "path", "SearchDirectory", "target_directory", "dir")
		case "ask_question":
			q, exists := s.Properties["questions"]
			b.questions = exists
			var shape struct {
				Items struct {
					Properties map[string]struct {
						Items struct {
							Type string `json:"type"`
						} `json:"items"`
					} `json:"properties"`
				} `json:"items"`
			}
			_ = json.Unmarshal(q, &shape)
			b.objectOptions = shape.Items.Properties["options"].Items.Type == "object"
			pick("question", "question", "question", "prompt", "message")
		case "search_web":
			pick("query", "query", "query", "q", "search_term")
		}
		m.tools[kind] = b
	}
	return m
}

func (m builtinMapper) cleanPath(raw string) string {
	s := strings.TrimPrefix(strings.TrimSpace(raw), "file://")
	if m.home != "" {
		if s == m.home {
			return "."
		}
		if rel, ok := strings.CutPrefix(s, m.home+"/"); ok {
			return rel
		}
	}
	return raw
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'" }

func (m builtinMapper) translate(call FunctionCall) (string, json.RawMessage, bool) {
	var a map[string]any
	if json.Unmarshal(call.Args, &a) != nil {
		return "", nil, false
	}
	str := func(empty bool, keys ...string) (string, bool) {
		for _, k := range keys {
			if s, ok := a[k].(string); ok && (empty || s != "") {
				return s, true
			}
		}
		return "", false
	}
	kind := call.Name
	if kind == "grep" {
		kind = "grep_search"
	}
	if kind == "glob" {
		kind = "find_by_name"
	}
	b, found := m.tools[kind]
	out := map[string]any{}
	put := func(slot string, v any) {
		if key := b.keys[slot]; key != "" {
			out[key] = v
		}
	}
	shell := func(cmd, desc string) bool {
		var ok bool
		b, ok = m.tools["run_command"]
		if ok {
			put("cmd", cmd)
			put("desc", desc)
		}
		return ok
	}
	path, hasPath := str(false, "Path", "file_path", "path")
	switch kind {
	case "run_command":
		cmd, ok := str(false, "CommandLine", "command", "cmd")
		if !found || !ok {
			return "", nil, false
		}
		put("cmd", cmd)
		desc, ok := str(false, "toolSummary", "description")
		if !ok {
			first, _, multiple := strings.Cut(cmd, "\n")
			runes := []rune(strings.TrimSpace(first))
			short := string(runes[:min(len(runes), 60)])
			if len(runes) > 60 || multiple {
				short += "…"
			}
			desc = "Run: " + short
			if short == "" {
				desc = "Run command"
			}
		}
		put("desc", desc)
	case "read_file":
		if !found || !hasPath {
			return "", nil, false
		}
		put("path", m.cleanPath(path))
		start, sok := builtinInt(a, "StartLine", "start_line")
		end, eok := builtinInt(a, "EndLine", "end_line")
		if sok {
			if b.keys["start"] != "" {
				put("start", start)
				if eok {
					put("end", end)
				}
			} else {
				put("offset", max(start, 1))
				if eok && end >= start {
					put("limit", max(end-start+1, 1))
				}
			}
		}
	case "write_file":
		content, ok := str(true, "Content", "content")
		if !hasPath || !ok {
			return "", nil, false
		}
		if !found {
			b, found = m.tools["edit_file"]
			if !found {
				return "", nil, false
			}
			put("old", "")
			put("new", content)
		} else {
			put("content", content)
		}
		put("path", m.cleanPath(path))
	case "edit_file":
		target, ok := str(true, "Target", "old_string")
		if !found || !hasPath || !ok {
			return "", nil, false
		}
		replacement, _ := str(true, "Replacement", "new_string")
		put("path", m.cleanPath(path))
		put("old", target)
		put("new", replacement)
	case "grep_search":
		pattern, ok := str(false, "Query", "pattern", "query")
		if !found || !ok {
			return "", nil, false
		}
		put("pattern", pattern)
		if p, ok := str(false, "SearchPath", "path", "directory"); ok {
			if p = m.cleanPath(p); p != "" && p != "." {
				put("path", p)
			}
		}
		glob, gok := str(false, "glob", "include")
		if includes, ok := a["Includes"].([]any); ok && len(includes) > 0 {
			if v, ok := includes[0].(string); ok {
				glob, gok = v, true
			}
		}
		if gok {
			put("glob", glob)
		}
		for _, k := range []string{"CaseInsensitive", "case_insensitive"} {
			if v, ok := a[k].(bool); ok {
				put("case", v)
				break
			}
		}
	case "find_by_name":
		pattern, ok := str(false, "Pattern", "pattern")
		if !ok {
			return "", nil, false
		}
		dir, _ := str(false, "SearchDirectory", "path")
		dir = m.cleanPath(dir)
		if !found {
			if dir == "" {
				dir = "."
			}
			if !shell("find "+shellQuote(dir)+" -name "+shellQuote(pattern)+" -not -path '*/.git/*' 2>/dev/null | head -200", "Find files named "+pattern) {
				return "", nil, false
			}
		} else {
			put("pattern", pattern)
			if dir != "" && dir != "." {
				put("path", dir)
			}
		}
	case "list_dir":
		dir, ok := str(false, "DirectoryPath", "path")
		if !ok {
			dir = "."
		}
		dir = m.cleanPath(dir)
		if !found {
			if dir == "" {
				dir = "."
			}
			if !shell("ls -la "+shellQuote(dir), "List directory "+dir) {
				return "", nil, false
			}
		} else {
			put("path", dir)
		}
	case "ask_question":
		qs, ok := a["questions"].([]any)
		if !found || !ok || len(qs) == 0 {
			return "", nil, false
		}
		if b.questions {
			mapped := []any{}
			for _, raw := range qs {
				q, ok := raw.(map[string]any)
				if !ok {
					continue
				}
				text, ok := q["question"].(string)
				if !ok {
					continue
				}
				multi, ok := q["is_multi_select"].(bool)
				if !ok {
					multi, _ = q["multiSelect"].(bool)
				}
				opts := []any{}
				if v, ok := q["options"].([]any); ok {
					opts = append(opts, v...)
				}
				if b.objectOptions {
					for i, v := range opts {
						if s, ok := v.(string); ok {
							opts[i] = map[string]any{"label": s, "description": ""}
						}
					}
				}
				mapped = append(mapped, map[string]any{"question": text, "header": "Question", "multiSelect": multi, "options": opts})
			}
			if len(mapped) == 0 {
				return "", nil, false
			}
			out["questions"] = mapped
		} else {
			q, ok := qs[0].(map[string]any)
			if !ok {
				return "", nil, false
			}
			text, ok := q["question"].(string)
			if !ok {
				return "", nil, false
			}
			put("question", text)
		}
	case "search_web":
		query, ok := str(false, "query", "q")
		if !found || !ok {
			return "", nil, false
		}
		put("query", query)
	default:
		return "", nil, false
	}
	raw, err := json.Marshal(out)
	return b.name, raw, err == nil
}

func builtinInt(a map[string]any, keys ...string) (int64, bool) {
	for _, k := range keys {
		if n, ok := a[k].(float64); ok && n == float64(int64(n)) {
			return int64(n), true
		}
	}
	return 0, false
}
