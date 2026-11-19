package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	wiresig "gcli2api/agy-http/agywire/signature"
	translator "github.com/router-for-me/CLIProxyAPI/v7/sdk/translator"
)

type binding struct {
	name   string
	expiry time.Time
}
type modelRotation struct{ next atomic.Uint64 }
type server struct {
	store     *store
	bridge    *bridge
	bindings  sync.Map
	rotations sync.Map
	ctx       context.Context
	logs      *logBuffer
	capture   captureState
	oauth     oauthState
	quota     quotaState
}

func jsonResponse(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func failure(w http.ResponseWriter, status int, message string) {
	jsonResponse(w, status, object{"detail": message})
}
func readObject(w http.ResponseWriter, r *http.Request) (object, bool) {
	r.Body = http.MaxBytesReader(w, r.Body, 128<<20)
	var v object
	dec := json.NewDecoder(r.Body)
	if err := dec.Decode(&v); err != nil || v == nil {
		failure(w, 400, "无效的 JSON 请求")
		return nil, false
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		failure(w, 400, "请求只能包含一个 JSON 对象")
		return nil, false
	}
	return v, true
}
func (s *server) authorized(r *http.Request, panel bool) bool {
	key := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if panel && r.URL.Path == "/logs/stream" && key == "" {
		key = r.URL.Query().Get("token")
	}
	if !panel && key == "" {
		for _, h := range []string{"x-api-key", "x-goog-api-key", "access_token", "x-anthropic-auth-token", "anthropic-auth-token"} {
			if key = r.Header.Get(h); key != "" {
				break
			}
		}
		if key == "" {
			key = r.URL.Query().Get("key")
		}
	}
	want := s.store.password(panel)
	return want != "" && subtle.ConstantTimeCompare([]byte(key), []byte(want)) == 1
}
func (s *server) pick(model, key string, attempted map[string]bool) *credential {
	accounts := s.store.accounts.Load()
	now := unixNow()
	if key != "" && truth(s.store.value("session_affinity_enabled")) {
		if value, ok := s.bindings.Load(key); ok {
			b := value.(binding)
			c := accounts.byName[b.name]
			if time.Now().Before(b.expiry) && c != nil && !attempted[c.name] && c.available(model, now) {
				return c
			}
			s.bindings.Delete(key)
		}
	}
	if len(accounts.all) == 0 {
		return nil
	}
	value, ok := s.rotations.Load(model)
	if !ok {
		value, _ = s.rotations.LoadOrStore(model, &modelRotation{})
	}
	start := value.(*modelRotation).next.Add(1) - 1
	for i := 0; i < len(accounts.all); i++ {
		c := accounts.all[(int(start%uint64(len(accounts.all)))+i)%len(accounts.all)]
		if !attempted[c.name] && c.available(model, now) {
			return c
		}
	}
	return nil
}
func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Headers", "*")
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
	if r.Method == "OPTIONS" {
		w.WriteHeader(204)
		return
	}
	if r.URL.Path == "/keepalive" {
		w.WriteHeader(200)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/antigravity/") {
		if !s.authorized(r, false) {
			failure(w, 401, "API 密码错误")
			return
		}
		s.api(w, r)
		return
	}
	if r.URL.Path == "/" || strings.HasPrefix(r.URL.Path, "/assets/") || r.URL.Path == "/favicon.ico" {
		s.static(w, r)
		return
	}
	if r.URL.Path == "/oauth-callback" {
		s.oauthCallback(w, r)
		return
	}
	if r.URL.Path != "/auth/login" && r.URL.Path != "/version/info" && !s.authorized(r, true) {
		failure(w, 401, "面板密码错误")
		return
	}
	s.panel(w, r)
}
func (s *server) static(w http.ResponseWriter, r *http.Request) {
	root := "dist"
	if v := os.Getenv("FRONTEND_DIST"); v != "" {
		root = v
	}
	if r.URL.Path == "/" {
		http.ServeFile(w, r, filepath.Join(root, "index.html"))
		return
	}
	http.FileServer(http.Dir(root)).ServeHTTP(w, r)
}
func (s *server) api(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/antigravity/")
	if r.Method == "GET" && (path == "v1/models" || path == "v1beta/models") {
		s.modelList(w, r, path == "v1beta/models")
		return
	}
	if r.Method != "POST" {
		failure(w, 405, "Method not allowed")
		return
	}
	input, ok := readObject(w, r)
	if !ok {
		return
	}
	format := translator.FormatOpenAI
	model := text(input["model"])
	stream := truth(input["stream"])
	count := false
	switch {
	case path == "v1/chat/completions":
	case path == "v1/messages" || path == "v1/messages/count_tokens":
		format = translator.FormatClaude
		count = strings.HasSuffix(path, "count_tokens")
	case strings.HasPrefix(path, "v1beta/models/") || strings.HasPrefix(path, "v1/models/"):
		format = translator.FormatGemini
		name := strings.SplitN(path, "/models/", 2)[1]
		var operation string
		model, operation, _ = strings.Cut(name, ":")
		stream = operation == "streamGenerateContent"
		count = operation == "countTokens"
		if operation != "generateContent" && !stream && !count {
			failure(w, 404, "未知的模型接口")
			return
		}
	default:
		failure(w, 404, "Not found")
		return
	}
	if strings.TrimSpace(model) == "" {
		failure(w, 400, "缺少 model")
		return
	}
	anti := false
	for _, prefix := range []string{"抗截断/", "流式抗截断/"} {
		if strings.HasPrefix(model, prefix) {
			anti = true
			model = strings.TrimPrefix(model, prefix)
			break
		}
	}
	if format != translator.FormatGemini {
		input["model"] = model
	}
	key := sessionKey(input, r.Header)
	if truth(s.store.value("compatibility_mode_enabled")) {
		for _, v := range list(input["messages"]) {
			message := obj(v)
			if message["role"] == "system" {
				message["role"] = "user"
			}
		}
		if content := input["system"]; content != nil {
			input["messages"] = append([]any{object{"role": "user", "content": content}}, list(input["messages"])...)
			delete(input, "system")
		}
	}
	if err := s.resolveImages(r.Context(), input); err != nil {
		failure(w, 400, err.Error())
		return
	}
	if strings.Contains(model, "image") {
		gen := obj(input["generationConfig"])
		options := clone(gen)
		if input["size"] != nil {
			options["size"] = input["size"]
		}
		var imageConfig object
		model, imageConfig = imageModelOptions(model, options)
		gen["imageConfig"] = imageConfig
		gen["responseModalities"] = []string{"TEXT", "IMAGE"}
		gen["candidateCount"] = 1
		input["generationConfig"] = gen
	}
	raw := encoded(input)
	if count {
		s.countTokens(w, r, raw, format, model)
		return
	}
	s.generateAPI(w, r, raw, key, format, model, stream, anti)
}

func (s *server) generateAPI(w http.ResponseWriter, r *http.Request, raw []byte, key string, format translator.Format, model string, stream, anti bool) {
	attempted := map[string]bool{}
	limit := len(s.store.accounts.Load().all)
	if truth(s.store.value("credential_retry_limit_enabled")) {
		limit = min(limit, max(1, int(number(s.store.value("max_retry_credentials")))))
	}
	var lastErr error
	for len(attempted) < limit {
		if r.Context().Err() != nil {
			return
		}
		c := s.pick(model, key, attempted)
		if c == nil {
			break
		}
		attempted[c.name] = true
		a, err := s.engine(r.Context(), c)
		if err != nil {
			if r.Context().Err() != nil {
				return
			}
			lastErr = redactError(err)
			recordFailure(c, model, lastErr)
			continue
		}
		sigKey := wiresig.Key(text((*c.data.Load())["refresh_token"]))
		ctx, payload, owned, err := prepareInput(r.Context(), raw, format, model, sigKey, anti)
		if err != nil {
			a.active.Add(-1)
			failure(w, 400, err.Error())
			return
		}
		f := &responseFormat{format: format, model: model, original: raw, request: payload}
		responseID, created := "chatcmpl-"+randomID(), time.Now().UTC().Format(time.RFC3339Nano)
		wrote, visible := false, false
		var response object
		var parts []any
		emit := func(data []byte) error {
			data, hasOutput := unwrapReply(data, owned, truth(s.store.value("return_thoughts_to_frontend")))
			visible = visible || hasOutput
			var envelope object
			if e := json.Unmarshal(data, &envelope); e != nil {
				return e
			}
			response = obj(envelope["response"])
			if response["responseId"] == nil {
				response["responseId"] = responseID
			}
			if response["createTime"] == nil {
				response["createTime"] = created
			}
			data = encoded(envelope)
			if stream {
				// Stream thoughts immediately; suppress only an empty attempt's terminal metadata.
				if !wrote && !visible && len(list(obj(obj(first(response["candidates"]))["content"])["parts"])) == 0 {
					return nil
				}
				if !wrote {
					w.Header().Set("Content-Type", "text/event-stream")
					w.Header().Set("Cache-Control", "no-cache")
					w.WriteHeader(200)
					wrote = true
				}
				for _, chunk := range f.stream(ctx, data) {
					if _, e := w.Write(chunk); e != nil {
						return e
					}
				}
				if fl, ok := w.(http.Flusher); ok {
					fl.Flush()
				}
				return nil
			}
			for _, v := range list(response["candidates"]) {
				parts = append(parts, list(obj(obj(v)["content"])["parts"])...)
			}
			return nil
		}
		for retry := 0; retry < 2; retry++ {
			err = a.engine.Stream(ctx, payload, model, key, emit, sigKey)
			if err == nil {
				if visible || !truth(s.store.value("empty_output_error_enabled")) {
					break
				}
				if retry == 0 {
					var p object
					_ = json.Unmarshal(payload, &p)
					p["_input_in_tool"] = true
					payload = encoded(p)
					continue
				}
				err = fmt.Errorf("上游没有返回有效内容")
			}
			if precise429(err) && !wrote {
				if moved, ok := moveSystem(payload); ok {
					payload = moved
					continue
				}
			}
			status, _ := errorStatus(err)
			if status == 401 && retry == 0 && !wrote {
				if refreshErr := s.refreshToken(r.Context(), c, true); refreshErr == nil {
					data := *c.data.Load()
					token := text(data["access_token"])
					if token == "" {
						token = text(data["token"])
					}
					a.token.Store(&token)
					continue
				}
			}
			break
		}
		a.last.Store(time.Now().UnixNano())
		a.active.Add(-1)
		if r.Context().Err() != nil {
			return
		}
		if err == nil {
			s.capture.recordResult(r, raw, 200)
			c.calls.Add(1)
			c.success.Store(time.Now().UnixMilli())
			c.dirty.Store(true)
			if key != "" && truth(s.store.value("session_affinity_enabled")) {
				s.bindings.Store(key, binding{c.name, time.Now().Add(time.Duration(number(s.store.value("session_affinity_ttl_seconds"))) * time.Second)})
			}
			if stream {
				if format == translator.FormatClaude {
					if !wrote {
						w.Header().Set("Content-Type", "text/event-stream")
					}
					for _, chunk := range f.stream(ctx, []byte("[DONE]")) {
						_, _ = w.Write(chunk)
					}
				}
				if format == translator.FormatOpenAI {
					_, _ = io.WriteString(w, "data: [DONE]\n\n")
				}
				return
			}
			if response == nil {
				failure(w, 502, "上游没有返回内容")
				return
			}
			candidates := list(response["candidates"])
			if len(candidates) > 0 {
				obj(candidates[0])["content"] = object{"role": "model", "parts": parts}
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(f.nonStream(ctx, encoded(object{"response": response})))
			return
		}
		lastErr = err
		recordFailure(c, model, err)
		if wrote {
			event := encoded(object{"error": object{"message": err.Error(), "type": "upstream_error"}})
			if format == translator.FormatClaude {
				_, _ = fmt.Fprintf(w, "event: error\ndata: %s\n\n", event)
			} else {
				_, _ = fmt.Fprintf(w, "data: %s\n\n", event)
			}
			return
		}
		code, _ := errorStatus(err)
		if precise429(err) {
			code = 400
		}
		if code == http.StatusNotImplemented || (code != 401 && code != 403 && code != 404 && code != 429 && code < 500) {
			break
		}
		if delay := number(s.store.value("credential_retry_interval")); delay > 0 {
			select {
			case <-r.Context().Done():
				return
			case <-time.After(time.Duration(delay * float64(time.Second))):
			}
		}
	}
	if lastErr != nil {
		status, body := errorStatus(lastErr)
		if precise429(lastErr) {
			status = 400
		}
		s.capture.recordResult(r, raw, status)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write(body)
		return
	}
	jsonResponse(w, 503, object{"error": object{"message": "auth_unavailable: 当前模型没有可用凭证", "type": "server_error", "code": "auth_unavailable"}})
}
func first(v any) any {
	values := list(v)
	if len(values) > 0 {
		return values[0]
	}
	return nil
}

func runServer() error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	store, err := openStore(ctx)
	if err != nil {
		return err
	}
	defer func() { cancel(); <-store.done; store.db.Close() }()
	if os.Getenv("AGY_WIRE_STATE_DIR") == "" {
		os.Setenv("AGY_WIRE_STATE_DIR", filepath.Join(store.dir, ".wire"))
	}
	logs := newLogBuffer()
	if os.Getenv("ENABLE_LOG") == "0" {
		log.SetOutput(io.Discard)
	} else {
		log.SetOutput(io.MultiWriter(os.Stderr, logs))
	}
	s := &server{store: store, bridge: &bridge{ctx: ctx}, ctx: ctx, logs: logs}
	go s.bridge.sweepWires()
	go s.maintenance()
	addr := net.JoinHostPort(text(store.value("host")), strconv.Itoa(int(number(store.value("port")))))
	httpServer := &http.Server{Addr: addr, Handler: s, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 90 * time.Second, MaxHeaderBytes: 1 << 20}
	go func() {
		<-ctx.Done()
		shutdown, done := context.WithTimeout(context.Background(), 15*time.Second)
		defer done()
		_ = httpServer.Shutdown(shutdown)
	}()
	log.Printf("GCLI2API Go server listening on %s; shared session engine enabled", addr)
	err = httpServer.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}
func (s *server) maintenance() {
	tick := time.NewTicker(time.Minute)
	defer tick.Stop()
	for {
		select {
		case <-s.ctx.Done():
			return
		case now := <-tick.C:
			s.bindings.Range(func(k, v any) bool {
				if now.After(v.(binding).expiry) {
					s.bindings.Delete(k)
				}
				return true
			})
			if endpoint := text(s.store.value("keepalive_url")); endpoint != "" {
				interval := time.Duration(number(s.store.value("keepalive_interval"))) * time.Second
				if interval < time.Minute || now.Unix()%int64(interval.Seconds()) < 60 {
					ctx, cancel := context.WithTimeout(s.ctx, 10*time.Second)
					req, _ := http.NewRequestWithContext(ctx, "HEAD", endpoint, nil)
					if req != nil {
						if resp, err := http.DefaultClient.Do(req); err == nil {
							resp.Body.Close()
						}
					}
					cancel()
				}
			}
		}
	}
}
