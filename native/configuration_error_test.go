package main

import (
	"testing"

	"gcli2api/agy-http/agywire/agent"
)

func TestLocalConfigurationDoesNotCooldown(t *testing.T) {
	c := newCredential("test", object{}, object{"error_codes": []any{}, "model_cooldowns": object{}})
	err := &agent.ConfigurationError{Message: "local monitor config is unsupported"}
	code, _ := errorStatus(err)
	if code != 501 {
		t.Fatalf("local error reported as %d", code)
	}
	recordFailure(c, "gemini-4-hydrogen-low", err)
	if len(list((*c.state.Load())["error_codes"])) != 0 || len(obj((*c.state.Load())["model_cooldowns"])) != 0 {
		t.Fatal("local error marked the credential")
	}
}

func TestRepairLegacyMonitorFailure(t *testing.T) {
	state := object{
		"error_codes":      []any{float64(404), float64(429)},
		"error_messages":   object{"404": object{"model": "gemini-4-hydrogen-low", "message": "unknown model: gemini-4-hydrogen-low(目录里的安全监控配置未核对 sha256=65f17f96d6858c47)"}},
		"model_cooldowns":  object{"gemini-4-hydrogen-low": float64(9999999999), "other-model": float64(9999999999)},
		"supported_models": []any{"other-model"},
	}
	if !clearLegacyConfigurationFailure(state) {
		t.Fatal("legacy local rejection not repaired")
	}
	if len(obj(state["model_cooldowns"])) != 1 || len(list(state["supported_models"])) != 2 || len(list(state["error_codes"])) != 1 {
		t.Fatal("wrong model state repaired")
	}
	actual := object{"error_messages": object{"404": object{"model": "not-present", "message": "upstream model not found"}}}
	if clearLegacyConfigurationFailure(actual) {
		t.Fatal("real upstream error was cleared")
	}
}
