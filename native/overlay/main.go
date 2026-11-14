package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

func run() error {
	sourcePath := filepath.Join(runtime.GOROOT(), "src", "crypto", "tls", "common.go")
	source, err := os.ReadFile(sourcePath)
	if err != nil {
		return err
	}
	before := "\t\t// Omit ML-DSA from signature_algorithms_cert in Go 1.27 to work\n\t\t// around non-compliant middleboxes. See https://go.dev/issue/82536.\n\t\tif isCert {\n\t\t\treturn true\n\t\t}\n"
	text := strings.ReplaceAll(string(source), "\r\n", "\n")
	if strings.Count(text, before) != 1 {
		return fmt.Errorf("unsupported Go TLS source: expected the Go 1.27 ML-DSA certificate compatibility guard")
	}
	directory, err := os.MkdirTemp("", "cpa-agy-tls-")
	if err != nil {
		return err
	}
	patchedPath := filepath.Join(directory, "common.go")
	if err = os.WriteFile(patchedPath, []byte(strings.Replace(text, before, "", 1)), 0600); err != nil {
		return err
	}
	data, err := json.Marshal(map[string]any{"Replace": map[string]string{sourcePath: patchedPath}})
	if err != nil {
		return err
	}
	overlayPath := filepath.Join(directory, "overlay.json")
	if err = os.WriteFile(overlayPath, data, 0600); err != nil {
		return err
	}
	fmt.Println(overlayPath)
	return nil
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
