package agent

import (
	_ "embed"
	"encoding/json"
	"strconv"
	"strings"

	"gcli2api/agy-http/agywire/ids"
	"gcli2api/agy-http/agywire/pj"
	"gcli2api/agy-http/agywire/schema"
)

//go:embed assets/system_prompt.tmpl
var systemPromptTmpl string

//go:embed assets/title_system.txt
var titleSystemPrompt string

//go:embed assets/builtin_tools.json
var builtinToolsRaw []byte

//go:embed assets/mcp_resource_tools.json
var mcpResourceToolsRaw []byte

//go:embed assets/guidelines_hydrogen.txt
var guidelinesHydrogen string

const Project = "aicode-consumers"

const TitleModel = "gemini-3.5-flash-lite"

type Part struct {
	Text      *string
	Thought   bool
	Signature string

	Call *FunctionCall
	Resp *FunctionResponse

	Inline *InlineData
}

type FunctionCall struct {
	ID   string
	Name string
	Args json.RawMessage
}

type FunctionResponse struct {
	ID     string
	Name   string
	Output *string
	Error  *string
	Parts  []InlineData
}

type InlineData struct {
	MimeType string
	Data     string
}

type Content struct {
	Role  string
	Parts []Part
}

func strp(s string) *string { return &s }

func writePart(w *pj.Buf, p Part) error {
	w.ObjStart()
	switch {
	case p.Call != nil:
		w.Key("functionCall").ObjStart()
		if p.Call.ID != "" {
			w.Key("id").Str(p.Call.ID)
		}
		w.Key("name").Str(p.Call.Name)
		w.Key("args")
		if err := writeStruct(w, p.Call.Args); err != nil {
			return err
		}
		w.ObjEnd()
		if p.Signature != "" {
			w.Key("thoughtSignature").Str(p.Signature)
		}
	case p.Resp != nil:
		w.Key("functionResponse").ObjStart()
		if p.Resp.ID != "" {
			w.Key("id").Str(p.Resp.ID)
		}
		w.Key("name").Str(p.Resp.Name)
		w.Key("response").ObjStart()
		if p.Resp.Error != nil {
			w.Key("error").Str(*p.Resp.Error)
		} else if p.Resp.Output != nil {
			w.Key("output").Str(*p.Resp.Output)
		}
		w.ObjEnd()
		if len(p.Resp.Parts) > 0 {
			w.Key("parts").ArrStart()
			for _, d := range p.Resp.Parts {
				w.ObjStart().Key("inlineData").ObjStart().Key("mimeType").Str(d.MimeType).Key("data").Str(d.Data).ObjEnd().ObjEnd()
			}
			w.ArrEnd()
		}
		w.ObjEnd()
	case p.Inline != nil:
		w.Key("inlineData").ObjStart().Key("mimeType").Str(p.Inline.MimeType).Key("data").Str(p.Inline.Data).ObjEnd()
	case p.Text != nil:
		w.Key("text").Str(*p.Text)
		if p.Thought {
			w.Key("thought").Bool(true)
		}
		if p.Signature != "" {
			w.Key("thoughtSignature").Str(p.Signature)
		}
	}
	w.ObjEnd()
	return nil
}

func writeStruct(w *pj.Buf, raw json.RawMessage) error {
	if len(raw) == 0 {
		w.ObjStart().ObjEnd()
		return nil
	}
	var v any
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		return err
	}
	if _, ok := v.(map[string]any); !ok {
		v = map[string]any{}
	}
	schema.WriteValue(w, v)
	return nil
}

func writeContents(w *pj.Buf, contents []Content) error {
	w.Key("contents").ArrStart()
	for _, c := range contents {
		w.ObjStart().Key("role").Str(c.Role).Key("parts").ArrStart()
		for _, p := range c.Parts {
			if err := writePart(w, p); err != nil {
				return err
			}
		}
		w.ArrEnd().ObjEnd()
	}
	w.ArrEnd()
	return nil
}

func writeSystem(w *pj.Buf, text string) {
	w.Key("systemInstruction").ObjStart().Key("role").Str("user").Key("parts").ArrStart().
		ObjStart().Key("text").Str(text).ObjEnd().ArrEnd().ObjEnd()
}

func thinkingLevelName(level int) (string, bool) {
	switch level {
	case 1:
		return "LOW", true
	case 2:
		return "MEDIUM", true
	case 3:
		return "HIGH", true
	}
	return "", false
}

type AgentRequest struct {
	ToolConfig          json.RawMessage
	ThinkingConfig      json.RawMessage
	Cascade, Trajectory string
	UnixMilli           int64
	StepIndex           int
	ModelCalls          int
	LastExecutionID     string
	Model               ModelInfo
	System              string
	MCPServer           string
	MCPTools            []schema.MCPTool

	ResourceTools bool
	Contents      []Content
}

func RenderAgent(r AgentRequest) ([]byte, error) {
	var w pj.Buf
	w.ObjStart()
	w.Key("project").Str(Project)
	w.Key("requestId").Str(ids.AgentRequestID(r.Cascade, r.UnixMilli, r.Trajectory, r.StepIndex))
	w.Key("request").ObjStart()
	if err := writeContents(&w, r.Contents); err != nil {
		return nil, err
	}
	writeSystem(&w, r.System)
	w.Key("tools").ArrStart()
	w.Raw(builtinToolsRaw[1 : len(builtinToolsRaw)-1])
	for _, t := range r.MCPTools {
		if err := schema.WriteToolElement(&w, r.MCPServer, t); err != nil {
			return nil, err
		}
	}
	if r.ResourceTools || len(r.MCPTools) > 0 {
		w.Raw(mcpResourceToolsRaw[1 : len(mcpResourceToolsRaw)-1])
	}
	w.ArrEnd()

	if len(r.ToolConfig) > 0 {
		w.Key("toolConfig")
		if err := writeStruct(&w, r.ToolConfig); err != nil {
			return nil, err
		}
	}
	labels := map[string]string{
		"cascade_id":               r.Cascade,
		"last_step_index":          itoa(r.StepIndex - 1),
		"model_enum":               r.Model.Enum,
		"request_id":               r.Trajectory + "-" + itoa(r.ModelCalls),
		"root_cascade_id":          r.Cascade,
		"trajectory_id":            r.Trajectory,
		"used_claude":              "false",
		"used_claude_conservative": "false",
		"used_non_gemini_model":    "false",
	}
	if r.LastExecutionID != "" {
		labels["last_execution_id"] = r.LastExecutionID
	}
	w.Key("labels").ObjStart()
	for _, k := range pj.SortedKeys(labels) {
		w.Key(k).Str(labels[k])
	}
	w.ObjEnd()
	w.Key("generationConfig").ObjStart()
	if r.Model.MaxOutputTokens > 0 {
		w.Key("maxOutputTokens").Int32(r.Model.MaxOutputTokens)
	}
	if !forcedClaudeTools(r.Model.ID, r.ToolConfig) {
		w.Key("thinkingConfig")
		if len(r.ThinkingConfig) > 0 {
			var thinking map[string]any
			if err := json.Unmarshal(r.ThinkingConfig, &thinking); err != nil {
				return nil, err
			}
			if thinking == nil {
				thinking = map[string]any{}
			}
			thinking["includeThoughts"] = true
			raw, _ := json.Marshal(thinking)
			if err := writeStruct(&w, raw); err != nil {
				return nil, err
			}
		} else {
			w.ObjStart().Key("includeThoughts").Bool(true)
			if name, ok := thinkingLevelName(r.Model.ThinkingLevel); ok {
				w.Key("thinkingLevel").Str(name)
			} else if r.Model.ThinkingLevel != 0 {
				w.Key("thinkingLevel").Int32(int64(r.Model.ThinkingLevel))
			}
			w.ObjEnd()
		}
	}
	w.ObjEnd()
	w.Key("sessionId").Str(ids.AgentSessionID)
	w.ObjEnd()
	w.Key("model").Str(r.Model.ID)
	w.Key("userAgent").Str("antigravity")
	w.Key("requestType").Str("chat")
	w.ObjEnd()
	return w.Bytes(), nil
}

func RenderTitle(promptText string, images []InlineData) []byte {
	var w pj.Buf
	w.ObjStart()
	w.Key("project").Str(Project)
	w.Key("requestId").Str(ids.PrefixedRequestID("checkpoint"))
	w.Key("request").ObjStart()
	parts := []Part{{Text: strp(promptText)}}
	for i := range images {
		parts = append(parts, Part{Inline: &images[i]})
	}
	_ = writeContents(&w, []Content{{Role: "user", Parts: parts}})
	writeSystem(&w, titleSystemPrompt)
	w.Key("generationConfig").ObjStart().Key("thinkingConfig").ObjStart().
		Key("includeThoughts").Bool(true).Key("thinkingLevel").Str("LOW").ObjEnd().ObjEnd()
	w.Key("sessionId").Str(ids.AgentSessionID)
	w.ObjEnd()
	w.Key("model").Str(TitleModel)
	w.Key("userAgent").Str("antigravity")
	w.Key("requestType").Str("checkpoint")
	w.ObjEnd()
	return w.Bytes()
}

func RenderSystemPrompt(home, cwd, conversationID, mcpServer string, mcpTools []schema.MCPTool, appendText, guidelines string) string {
	var sec string
	if len(mcpTools) > 0 {
		var b strings.Builder
		b.WriteString("<mcp_servers>\nEach MCP server has a directory `<appDataDir>/mcp/<serverName>` containing tool schemas (`<toolName>.json`) and optionally an `instructions.md` file with best practices.\nEagerly loaded tools are registered as native tools under the name `mcp_<serverName>_<toolName>`. Call eager tools directly.\nFor lazily-loaded tools, read the corresponding schema file to understand the arguments and usage, then call the tool using the `call_mcp_tool` tool.\nThe following MCP servers and their available tools are listed below, following this format:\n```\n# <serverName>\nEager:\n<toolName>\nLazy:\n<toolName>\n```\n")
		b.WriteString("# " + mcpServer + "\nEager:\n")
		for _, t := range mcpTools {
			b.WriteString(t.Name + "\n")
		}
		b.WriteString("</mcp_servers>\n")
		sec = b.String()
	}
	s := strings.NewReplacer(
		"{{CONVERSATION_ID}}", conversationID,
		"{{HOME}}", home,
		"{{CWD}}", cwd,
		"{{MCP_SECTION}}", sec,
	).Replace(systemPromptTmpl)
	if guidelines != "" {
		s = replaceSection(s, "guidelines", guidelines)
	}
	return s + appendText
}

func itoa(n int) string { return strconv.Itoa(n) }

func replaceSection(s, tag, body string) string {
	open, closeTag := "<"+tag+">\n", "\n</"+tag+">"
	i := strings.Index(s, open)
	j := strings.Index(s, closeTag)
	if i < 0 || j < i {
		return s
	}
	return s[:i+len(open)] + body + s[j:]
}

func HydrogenGuidelines() string { return guidelinesHydrogen }
