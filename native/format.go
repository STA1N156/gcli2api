package main

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	wiresig "gcli2api/agy-http/agywire/signature"
	translator "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
	_ "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator/builtin"
)

const replyInstruction = "（本次回复的全部内容必须通过 output_reply 工具的 content 字段提交，保留原有格式，不要在普通消息中重复输出，不要在正文提及这个工具。）"

type responseFormat struct {
	format            translator.Format
	model             string
	original, request []byte
	state             any
}

func prepareInput(ctx context.Context, raw []byte, format translator.Format, model string, key [32]byte, anti bool) (context.Context, []byte, bool, error) {
	ctx, clean := wiresig.Prepare(ctx, raw, key)
	translated := translator.TranslateRequest(format, translator.FormatAntigravity, model, clean, true)
	var envelope object
	if err := json.Unmarshal(translated, &envelope); err != nil {
		return ctx, nil, false, err
	}
	inner := obj(envelope["request"])
	if len(inner) == 0 {
		return ctx, nil, false, fmt.Errorf("请求缺少有效消息")
	}
	gen := obj(inner["generationConfig"])
	if v, ok := inner["thinkingConfig"]; ok {
		gen["thinkingConfig"] = v
		delete(inner, "thinkingConfig")
	}
	if gen["thinkingConfig"] == nil {
		for _, level := range []string{"low", "medium", "high", "minimal"} {
			if strings.HasSuffix(model, "-"+level) {
				gen["thinkingConfig"] = object{"thinkingLevel": strings.ToUpper(level)}
				break
			}
		}
	}
	thinking := obj(gen["thinkingConfig"])
	if level := text(thinking["thinkingLevel"]); level != "" {
		level = strings.ToUpper(level)
		if level == "MAX" || level == "XHIGH" {
			level = "HIGH"
		}
		if level == "NONE" || level == "MINIMAL" {
			level = "LOW"
		}
		thinking["thinkingLevel"] = level
		delete(thinking, "thinkingBudget")
	}
	if len(thinking) > 0 {
		gen["thinkingConfig"] = thinking
	}
	inner["generationConfig"] = gen
	owned := false
	if anti && !strings.Contains(model, "image") {
		envelope["_input_in_tool"] = true
		config := obj(inner["toolConfig"])
		function := obj(config["functionCallingConfig"])
		tools := list(inner["tools"])
		decls := 0
		callerReply := false
		for _, t := range tools {
			for _, d := range list(obj(t)["functionDeclarations"]) {
				decls++
				callerReply = callerReply || text(obj(d)["name"]) == "output_reply"
			}
		}
		if !callerReply && function["mode"] != "ANY" && function["mode"] != "NONE" {
			owned = true
			tools = append(tools, object{"functionDeclarations": []any{object{"name": "output_reply", "description": "将本次回复交给聊天界面显示。正文及需要附带的全部内容等全部放入 content。", "parameters": object{"type": "OBJECT", "properties": object{"content": object{"type": "STRING", "description": "本次回复的原文，保留原有格式；作为 JSON 字符串正确转义。"}}, "required": []string{"content"}}}}})
			inner["tools"] = tools
			function["mode"] = "ANY"
			delete(function, "allowedFunctionNames")
			if decls == 0 {
				function["allowedFunctionNames"] = []string{"output_reply"}
			}
			config["functionCallingConfig"] = function
			inner["toolConfig"] = config
			system := obj(inner["systemInstruction"])
			system["parts"] = append(list(system["parts"]), object{"text": replyInstruction})
			inner["systemInstruction"] = system
			envelope["_reply_tool_owned"] = true
		}
	}
	envelope["request"] = inner
	return ctx, encoded(envelope), owned, nil
}

func unwrapReply(raw []byte, owned bool, thoughts bool) ([]byte, bool) {
	var root object
	if json.Unmarshal(raw, &root) != nil {
		return raw, false
	}
	response := obj(root["response"])
	visible := false
	for _, v := range list(response["candidates"]) {
		candidate := obj(v)
		content := obj(candidate["content"])
		parts := []any{}
		var reply string
		replyCount := 0
		realTool := false
		for _, p := range list(content["parts"]) {
			part := obj(p)
			call := obj(part["functionCall"])
			if owned && call["name"] == "output_reply" {
				replyCount++
				args := obj(call["args"])
				if value, ok := args["content"].(string); ok && len(args) == 1 && strings.TrimSpace(value) != "" {
					reply = value
				}
				continue
			}
			if truth(part["thought"]) {
				if thoughts {
					parts = append(parts, part)
				}
				continue
			}
			visible = visible || strings.TrimSpace(text(part["text"])) != "" || len(call) > 0 || part["inlineData"] != nil || part["fileData"] != nil
			realTool = realTool || len(call) > 0
			parts = append(parts, part)
		}
		reason := text(candidate["finishReason"])
		if replyCount == 1 && !realTool && reply != "" && (reason == "" || reason == "STOP" || reason == "MAX_TOKENS" || reason == "FINISH_REASON_UNSPECIFIED") {
			parts = append(parts, object{"text": reply})
			visible = true
		}
		content["parts"] = parts
	}
	return encoded(root), visible
}

func (f *responseFormat) stream(ctx context.Context, raw []byte) [][]byte {
	if f.format == translator.FormatGemini {
		var root object
		_ = json.Unmarshal(raw, &root)
		return [][]byte{append(append([]byte("data: "), encoded(root["response"])...), []byte("\n\n")...)}
	}
	var result [][]byte
	for _, chunk := range translator.TranslateStream(ctx, translator.FormatAntigravity, f.format, f.model, f.original, f.request, raw, &f.state) {
		if len(chunk) == 0 {
			continue
		}
		if f.format == translator.FormatOpenAI {
			chunk = append(append([]byte("data: "), chunk...), []byte("\n\n")...)
		}
		result = append(result, chunk)
	}
	return result
}
func (f *responseFormat) nonStream(ctx context.Context, raw []byte) []byte {
	return translator.TranslateNonStream(ctx, translator.FormatAntigravity, f.format, f.model, f.original, f.request, raw, &f.state)
}
