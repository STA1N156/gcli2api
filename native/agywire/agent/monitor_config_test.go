package agent

import (
	"encoding/json"
	"testing"
)

func TestUpdatedMonitorConfig(t *testing.T) {
	config := `# Newly published upstream revision, not a pinned hash.
monitors {
 name: "updated_tool_safety"
 version: "new-revision"
 enabled: true
 mode: LOG
 execution_phases: EXECUTION_PHASE_PRE_TOOL
 tool_safety_config { allowed_tools: "new_upstream_read_tool" }
}`
	raw, _ := json.Marshal(map[string]any{
		"models": map[string]any{"gemini-4-hydrogen-low": map[string]any{"model": "MODEL_TEST", "modelExperimentIds": []string{"monitors"}}},
		"sharedModelExperiments": map[string]any{"monitors": map[string]any{"experiments": map[string]any{
			"allow-experimental-monitors":   map[string]any{"boolValue": true},
			"default-sonar-monitors-config": map[string]any{"stringValue": config},
		}}},
	})
	catalog, err := parseCatalog(raw)
	if err != nil {
		t.Fatal(err)
	}
	model := catalog.Models["gemini-4-hydrogen-low"]
	if !Supported(model) || model.Monitors == nil {
		t.Fatal("updated config still rejected")
	}
	if !model.Monitors.Monitors[0].Heuristic.allowed("new_upstream_read_tool", nil) {
		t.Fatal("upstream monitor rules were not preserved")
	}
	for _, invalid := range []string{"", `version: "empty"`, `monitors { name: "broken" enabled: true }`, `monitors {`} {
		if parseMonitorsConfig(invalid) != nil {
			t.Fatalf("invalid config accepted: %q", invalid)
		}
	}
}
