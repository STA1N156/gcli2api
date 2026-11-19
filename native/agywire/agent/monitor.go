package agent

import (
	"bytes"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"log"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"gcli2api/agy-http/agywire/ids"
	"gcli2api/agy-http/agywire/pj"
)

//go:embed assets/monitor_prompt_pieces.json
var monitorPiecesRaw []byte

var monitorPieces struct {
	Pre, Mid1, Mid2, Mid3, Schema, Post string
}

func init() {
	if err := json.Unmarshal(monitorPiecesRaw, &monitorPieces); err != nil {
		panic(err)
	}
}

var knownMonitorConfigs = map[string]bool{
	"3f9d77e3a5264771ea9c6edc385746b997b7e07469bfca2679024f7ada2d69b0": true,

	"bd1a2428b53a89d32e2f40fa4c426cc5e190335039da6258002f21f3a2e795da": true,
}

var monitorTierModel = map[string]string{
	"MODEL_TIER_FLASH_LITE": "gemini-3.5-flash-lite",
}

type monitorDef struct {
	Name, Mode, Phase                string
	Enabled                          bool
	TimeoutSeconds                   float64
	ParentName                       string
	ParentSeverity, ParentConfidence float64
	Heuristic                        *toolSafety
	Prompt, ModelTier, ThinkingLevel string
	Compaction                       string
	MaxTrajectoryTokens              float64
}

type MonitorsConfig struct {
	Monitors []monitorDef
}

type toolSafety struct {
	tools       map[string]bool
	commands    map[string]bool
	subcommands map[string]subcmdRule
	toolActions map[string]map[string]bool
}

type subcmdRule struct {
	subs     map[string]bool
	exact    map[string]bool
	prefixes []string
}

func setOf(xs []string) map[string]bool {
	m := map[string]bool{}
	for _, x := range xs {
		m[x] = true
	}
	return m
}

func parseMonitorsConfig(text string) *MonitorsConfig {
	sum := sha256.Sum256([]byte(text))
	if !knownMonitorConfigs[hex.EncodeToString(sum[:])] {
		return nil
	}
	root, err := parseTextproto(text)
	if err != nil {
		log.Printf("监控配置解析失败: %v", err)
		return nil
	}
	cfg := &MonitorsConfig{}
	for _, m := range root.msgs("monitors") {
		d := monitorDef{
			Name: m.str("name"), Mode: m.str("mode"), Phase: m.str("execution_phases"),
			Enabled: m.str("enabled") == "true", TimeoutSeconds: m.num("timeout_seconds"),
		}
		if dep := m.msg("dependency_config"); dep != nil {
			d.ParentName = dep.str("parent_monitor_name")
			d.ParentSeverity = dep.num("severity_threshold")
			d.ParentConfidence = dep.num("confidence_threshold")
		}
		if ts := m.msg("tool_safety_config"); ts != nil {
			h := &toolSafety{tools: setOf(ts.strs("allowed_tools")), commands: setOf(ts.strs("allowed_commands")),
				subcommands: map[string]subcmdRule{}, toolActions: map[string]map[string]bool{}}
			for _, e := range ts.msgs("allowed_subcommands") {
				v := e.msg("value")
				fl := v.msg("flags")
				h.subcommands[e.str("key")] = subcmdRule{subs: setOf(v.strs("subcommands")), exact: setOf(fl.strs("exact")), prefixes: fl.strs("prefixes")}
			}
			for _, e := range ts.msgs("allowed_tool_actions") {
				h.toolActions[e.str("key")] = setOf(e.msg("value").strs("actions"))
			}
			d.Heuristic = h
		}
		if lc := m.msg("llm_config"); lc != nil {
			d.Prompt, d.ModelTier, d.ThinkingLevel = lc.str("prompt"), lc.str("model_tier"), lc.str("thinking_level")
			d.Compaction = lc.str("trajectory_compaction")
			d.MaxTrajectoryTokens = lc.num("max_trajectory_tokens")
		}
		cfg.Monitors = append(cfg.Monitors, d)
	}
	return cfg
}

func (h *toolSafety) allowed(name string, args map[string]any) bool {
	if h.tools[name] {
		return true
	}
	if acts, ok := h.toolActions[name]; ok {
		if a, _ := args["Action"].(string); acts[a] {
			return true
		}
	}
	if name == "run_command" {
		cmd, _ := args["CommandLine"].(string)
		return h.commandAllowed(cmd)
	}
	return false
}

var shellMeta = regexp.MustCompile("[`$<>()\\\\]")

func (h *toolSafety) commandAllowed(cmd string) bool {
	if strings.TrimSpace(cmd) == "" || shellMeta.MatchString(cmd) {
		return false
	}
	for _, seg := range regexp.MustCompile(`&&|\|\||;|\|`).Split(cmd, -1) {
		f := strings.Fields(seg)
		if len(f) == 0 {
			return false
		}
		if h.commands[f[0]] {
			continue
		}
		rule, ok := h.subcommands[f[0]]
		if !ok || len(f) < 2 || !rule.subs[f[1]] {
			return false
		}
		for _, a := range f[2:] {
			if !strings.HasPrefix(a, "-") {
				continue
			}
			okFlag := rule.exact[a]
			for _, p := range rule.prefixes {
				okFlag = okFlag || strings.HasPrefix(a, p)
			}
			if !okFlag {
				return false
			}
		}
	}
	return true
}

type trajKind int

const (
	trajUser trajKind = iota
	trajModel
	trajEnv
)

type trajStep struct {
	Kind    trajKind
	Text    string
	Thought string
	Calls   []FunctionCall
	Call    FunctionCall
	Output  *string
	Error   *string
}

const toolErrPrefix = "Encountered error in tool execution: "

func goJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func goJSONIndentNoEscape(v any) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
	return strings.TrimSuffix(buf.String(), "\n")
}

func argsMap(raw json.RawMessage) map[string]any {
	m := map[string]any{}
	_ = json.Unmarshal(raw, &m)
	return m
}

func renderToolCallBlock(fc FunctionCall) string {
	return "```tool_call\nname: " + fc.Name + "\narguments_json: " + goJSON(argsMap(fc.Args)) + "\n```\n"
}

func renderEnv(step int, st trajStep) string {
	args := argsMap(st.Call.Args)
	keys := make([]string, 0, len(args))
	for k := range args {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	n := strconv.Itoa(step)
	b.WriteString("\n<STEP_" + n + "><ENVIRONMENT>\nTool: " + st.Call.Name + "\n")
	for _, k := range keys {
		v, ok := args[k].(string)
		if !ok {
			v = goJSON(args[k])
		}
		b.WriteString(k + ": " + v + "\n")
	}
	if st.Error != nil {
		b.WriteString("</ENVIRONMENT></STEP_" + n + ">\n<STEP_" + n + "><ERROR>\n" + strings.TrimPrefix(*st.Error, toolErrPrefix) + "\n</ERROR></STEP_" + n + ">\n")
		return b.String()
	}
	if st.Output != nil {
		b.WriteString(*st.Output)
	}
	b.WriteString("\n</ENVIRONMENT></STEP_" + n + ">\n")
	return b.String()
}

func renderModelStep(step int, thought, text string, done []FunctionCall, envs []trajStep) string {
	var b strings.Builder
	n := strconv.Itoa(step)
	hasOut := text != "" || len(done) > 0
	if thought != "" {
		b.WriteString("<STEP_" + n + "><JETSKI_THOUGHT>\n" + thought + "\n</JETSKI_THOUGHT></STEP_" + n + ">\n")
		if !hasOut {
			b.WriteString("\n")
		}
	}
	if hasOut {
		b.WriteString("<STEP_" + n + "><JETSKI_OUTPUT>\n")
		if text != "" {
			b.WriteString(text + "\n")
		}
		for _, fc := range done {
			b.WriteString(renderToolCallBlock(fc))
		}
		b.WriteString("</JETSKI_OUTPUT></STEP_" + n + ">\n")
	}
	for i, e := range envs {
		b.WriteString(renderEnv(step+1+i, e))
	}
	return b.String()
}

func renderLatestStepTrajectory(traj []trajStep, step, k int, cur trajStep) string {
	var envs []trajStep
	if k > 0 && step+1+k <= len(traj) {
		envs = traj[step+1 : step+1+k]
	}
	s := renderModelStep(step, cur.Thought, cur.Text, cur.Calls[:k], envs)
	if s == "" && step > 0 {
		prev := step - 1
		switch traj[prev].Kind {
		case trajUser:
			p := strconv.Itoa(prev)
			s = "<STEP_" + p + "><JETSKI_USER>\n" + traj[prev].Text + "\n</JETSKI_USER></STEP_" + p + ">\n"
		case trajEnv:
			ms := prev
			for ms >= 0 && traj[ms].Kind == trajEnv {
				ms--
			}
			if ms >= 0 && traj[ms].Kind == trajModel {
				m := traj[ms]
				s = renderModelStep(ms, m.Thought, m.Text, m.Calls, traj[ms+1:step])
			}
		}
	}
	return "\n" + s
}

func renderPending(fc FunctionCall) string {
	return "\nTool: " + fc.Name + "\nArguments: " + goJSONIndentNoEscape(argsMap(fc.Args)) + "\n"
}

func renderMonitorText(traj, pending, rubric string) string {
	p := monitorPieces
	return p.Pre + "<JETSKI_TRAJECTORY>" + traj + "</JETSKI_TRAJECTORY>" + p.Mid1 +
		"<PENDING_TOOL_CALL>" + pending + "</PENDING_TOOL_CALL>" + p.Mid2 +
		"<REVIEW_RUBRIC>\n" + rubric + "\n</REVIEW_RUBRIC>" + p.Mid3 +
		"<OUTPUT_SCHEMA>" + p.Schema + "</OUTPUT_SCHEMA>" + p.Post
}

func RenderMonitor(text, model, thinkingLevel string) []byte {
	var w pj.Buf
	w.ObjStart()
	w.Key("project").Str(Project)
	w.Key("requestId").Str(ids.PrefixedRequestID("agent_monitoring"))
	w.Key("request").ObjStart()
	_ = writeContents(&w, []Content{{Role: "user", Parts: []Part{{Text: strp(text)}}}})
	writeSystem(&w, "")
	w.Key("generationConfig").ObjStart().Key("thinkingConfig").ObjStart().
		Key("includeThoughts").Bool(true).Key("thinkingLevel").Str(thinkingLevel).ObjEnd().ObjEnd()
	w.Key("sessionId").Str(ids.AgentSessionID)
	w.ObjEnd()
	w.Key("model").Str(model)
	w.Key("userAgent").Str("antigravity")
	w.Key("requestType").Str("agent_monitoring")
	w.ObjEnd()
	return w.Bytes()
}

type monitorResult struct {
	ran                  bool
	severity, confidence float64
}

var monitorJSON = regexp.MustCompile(`(?s)\{.*\}`)

func (d *Daemon) runPreToolMonitors(ctx context.Context, traj []trajStep, model ModelInfo, step, k int, fc FunctionCall) {
	cfg := model.Monitors
	if cfg == nil {
		return
	}
	if step >= len(traj) || traj[step].Kind != trajModel {
		log.Printf("监控:轨迹步号对不上(step=%d len=%d),跳过", step, len(traj))
		return
	}
	cur := traj[step]
	res := map[string]monitorResult{}
	for _, m := range cfg.Monitors {
		if !m.Enabled || m.Phase != "EXECUTION_PHASE_PRE_TOOL" {
			continue
		}
		if m.ParentName != "" {
			p := res[m.ParentName]
			if !p.ran || p.severity < m.ParentSeverity || p.confidence < m.ParentConfidence {
				continue
			}
		}
		switch {
		case m.Heuristic != nil:
			r := monitorResult{ran: true}
			if !m.Heuristic.allowed(fc.Name, argsMap(fc.Args)) {
				r.severity, r.confidence = 1, 1
			}
			res[m.Name] = r
		case m.Compaction == "6" || m.Compaction == "TRAJECTORY_COMPACTION_LATEST_STEP_ONLY":
			mdl, ok := monitorTierModel[m.ModelTier]
			if !ok {
				log.Printf("监控 %s:未知模型档位 %s,跳过", m.Name, m.ModelTier)
				continue
			}
			text := renderMonitorText(renderLatestStepTrajectory(traj, step, k, cur), renderPending(fc), m.Prompt)
			send := d.callMonitor
			if d.monitorSend != nil {
				send = d.monitorSend
			}
			t0 := time.Now()
			r := send(ctx, RenderMonitor(text, mdl, m.ThinkingLevel), time.Duration(m.TimeoutSeconds*float64(time.Second)))
			res[m.Name] = r
			log.Printf("监控 %s: tool=%s ran=%v severity=%.2f confidence=%.2f ms=%d", m.Name, fc.Name, r.ran, r.severity, r.confidence, time.Since(t0).Milliseconds())
		default:

			log.Printf("监控 %s 触发但未实现(compaction=%s),fail-open 跳过", m.Name, m.Compaction)
		}
	}
}

func (d *Daemon) callMonitor(ctx context.Context, body []byte, timeout time.Duration) monitorResult {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	rd, closer, err := d.streamGenerate(cctx, body)
	if err != nil {
		log.Printf("监控请求失败(fail-open): %v", err)
		return monitorResult{}
	}
	defer closer.Close()
	var text strings.Builder
	for {
		ch, err := rd.Next()
		if err != nil {
			break
		}
		for _, p := range ch.Parts {
			if !p.Thought {
				text.WriteString(p.Text)
			}
		}
	}
	var out struct {
		Severity, Confidence float64
	}
	if j := monitorJSON.FindString(text.String()); j != "" {
		_ = json.Unmarshal([]byte(j), &out)
	}
	return monitorResult{ran: true, severity: out.Severity / 10, confidence: out.Confidence / 10}
}
