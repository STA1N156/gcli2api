package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"gcli2api/agy-http/agywire/transport"
)

func parseVersion(data []byte) object {
	v := object{}
	for _, line := range strings.Split(string(data), "\n") {
		if key, value, ok := strings.Cut(strings.TrimSpace(line), "="); ok {
			v[key] = value
		}
	}
	return v
}
func (s *server) versionInfo(w http.ResponseWriter, r *http.Request) {
	data, _ := os.ReadFile("version.txt")
	v := parseVersion(data)
	result := object{"success": true, "version": v["short_hash"], "full_hash": v["full_hash"], "date": v["date"], "message": v["message"], "antigravity_cli_version": "1.5.11", "user_agent": transport.UserAgent}
	if result["version"] == nil {
		result["version"] = "go-wire"
	}
	if r.URL.Query().Get("check_update") == "true" {
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		req, _ := http.NewRequestWithContext(ctx, "GET", "https://raw.githubusercontent.com/su-kaka/gcli2api/refs/heads/master/version.txt", nil)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			result["check_update"] = false
			result["update_error"] = "更新检查连接失败"
		} else {
			defer resp.Body.Close()
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
			if resp.StatusCode == 200 {
				latest := parseVersion(body)
				result["check_update"] = true
				result["latest_version"] = latest["short_hash"]
				result["latest_hash"] = latest["full_hash"]
				result["latest_message"] = latest["message"]
				result["latest_date"] = latest["date"]
				result["has_update"] = latest["full_hash"] != v["full_hash"]
			} else {
				result["update_error"] = fmt.Sprintf("GitHub HTTP %d", resp.StatusCode)
			}
		}
	}
	jsonResponse(w, 200, result)
}
