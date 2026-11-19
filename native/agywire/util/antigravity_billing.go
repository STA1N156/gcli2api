package util

import "strings"

// StripAntigravityBillingHeader removes rolling client metadata without changing other text.
func StripAntigravityBillingHeader(text string) string {
	const prefix = "x-anthropic-billing-header:"
	var out strings.Builder
	position := 0
	changed := false
	for line := range strings.Lines(text) {
		trimmed := strings.TrimLeft(line, " \t\r")
		if len(trimmed) >= len(prefix) && strings.EqualFold(trimmed[:len(prefix)], prefix) {
			if !changed {
				out.Grow(len(text))
				out.WriteString(text[:position])
				changed = true
			}
		} else if changed {
			out.WriteString(line)
		}
		position += len(line)
	}
	if !changed {
		return text
	}
	return strings.TrimLeft(out.String(), "\n")
}
