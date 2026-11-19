package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Resolve remote images once before translation. Documents are never materialized.
func (s *server) resolveImages(ctx context.Context, input object) error {
	for _, m := range list(input["messages"]) {
		for _, v := range list(obj(m)["content"]) {
			part := obj(v)
			var address string
			switch part["type"] {
			case "image_url":
				address = text(obj(part["image_url"])["url"])
			case "image":
				address = text(obj(part["source"])["url"])
			}
			if !strings.HasPrefix(address, "http://") && !strings.HasPrefix(address, "https://") {
				continue
			}
			tr, err := s.bridge.transport("image-download", text(s.store.value("proxy")))
			if err != nil {
				return err
			}
			request, err := http.NewRequestWithContext(ctx, "GET", address, nil)
			if err != nil {
				return err
			}
			response, err := (&http.Client{Transport: tr, Timeout: 30 * time.Second}).Do(request)
			if err != nil {
				return fmt.Errorf("图片下载失败")
			}
			data, readErr := io.ReadAll(io.LimitReader(response.Body, (20<<20)+1))
			response.Body.Close()
			if response.StatusCode != 200 || readErr != nil || len(data) == 0 || len(data) > 20<<20 {
				return fmt.Errorf("图片下载失败或超过 20 MiB")
			}
			typ, _, _ := mime.ParseMediaType(response.Header.Get("Content-Type"))
			if !strings.HasPrefix(typ, "image/") {
				typ = http.DetectContentType(data)
			}
			if !strings.HasPrefix(typ, "image/") {
				return fmt.Errorf("图片 URL 没有返回有效图片")
			}
			encoded := base64.StdEncoding.EncodeToString(data)
			if part["type"] == "image_url" {
				u := clone(obj(part["image_url"]))
				u["url"] = "data:" + typ + ";base64," + encoded
				part["image_url"] = u
			} else {
				part["source"] = object{"type": "base64", "media_type": typ, "data": encoded}
			}
		}
	}
	return nil
}

func imageModelOptions(model string, raw object) (string, object) {
	config := clone(obj(raw["imageConfig"]))
	for _, size := range []string{"4k", "2k"} {
		if strings.Contains(model, "-"+size) {
			config["imageSize"] = strings.ToUpper(size)
		}
	}
	for _, aspect := range []string{"21x9", "16x9", "9x16", "4x3", "3x4", "1x1"} {
		if strings.Contains(model, "-"+aspect) {
			config["aspectRatio"] = strings.ReplaceAll(aspect, "x", ":")
		}
	}
	if size := text(raw["size"]); size != "" {
		w, h, ok := strings.Cut(size, "x")
		width, _ := strconv.Atoi(w)
		height, _ := strconv.Atoi(h)
		if ok && width > 0 && height > 0 {
			a, b := width, height
			for b != 0 {
				a, b = b, a%b
			}
			config["aspectRatio"] = fmt.Sprintf("%d:%d", width/a, height/a)
			switch {
			case max(width, height) > 2048:
				config["imageSize"] = "4K"
			case max(width, height) > 1024:
				config["imageSize"] = "2K"
			default:
				config["imageSize"] = "1K"
			}
		}
	}
	base := model
	for _, suffix := range []string{"-4k", "-2k", "-21x9", "-16x9", "-9x16", "-4x3", "-3x4", "-1x1"} {
		base = strings.ReplaceAll(base, suffix, "")
	}
	return base, config
}
