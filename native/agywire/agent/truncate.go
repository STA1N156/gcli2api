package agent

import (
	"os"
	"path/filepath"
	"strconv"
)

const (
	toolOutputLimit    = 10240
	toolOutputOverhead = 64
)

func toolOutputURL(home, conv string, step int) string {
	return "file://" + filepath.Join(home, ".gemini", "antigravity-cli", "brain", conv,
		".system_generated", "steps", strconv.Itoa(step), "output.txt")
}

func truncateToolOutput(full, url string) (string, bool) {
	if len(full) <= toolOutputLimit {
		return full, false
	}
	pre := "The tool's output was truncated due to being too long. You can find the full output in a file at " +
		url + ".\n\nHere is a preview with the middle of the output truncated:\n"
	half := (toolOutputLimit - len(pre) - toolOutputOverhead) / 2
	if half < 0 {
		half = 0
	}
	h := half
	for h > 0 && isContinuation(full[h]) {
		h--
	}
	t := len(full) - half
	for t < len(full) && isContinuation(full[t]) {
		t++
	}
	cut := t - h
	return pre + full[:h] + "\n<truncated " + strconv.Itoa(cut) + " of " + strconv.Itoa(len(full)) +
		" bytes from the middle of the output>\n" + full[t:], true
}

func isContinuation(b byte) bool { return b&0xC0 == 0x80 }

func (d *Daemon) finishToolOutput(conv string, step int, full string) string {
	url := toolOutputURL(d.home, conv, step)
	out, truncated := truncateToolOutput(full, url)
	if truncated {
		p := url[len("file://"):]
		if err := os.MkdirAll(filepath.Dir(p), 0o700); err == nil {
			_ = os.WriteFile(p, []byte(full), 0o600)
		}
	}
	return out
}
