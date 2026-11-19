package agent

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"
)

type ModelInfo struct {
	ID              string
	DisplayName     string
	Enum            string
	MaxTokens       int64
	MaxOutputTokens int64
	ThinkingLevel   int

	TemplateOverride bool

	Guidelines string

	NeedsMonitors bool

	Monitors *MonitorsConfig

	UnverifiedMonitorsSHA string
}

var knownGuidelinesTemplates = map[string]*string{

	"332984e33c92efa8de5ad75ab0fe5391ae4955263ffde60512551f4bc4cbedff": &guidelinesHydrogen,
}

type experimentValue struct {
	StringValue *string `json:"stringValue"`
	BoolValue   *bool   `json:"boolValue"`
}

func classifyExperiments(own map[string]experimentValue, shared []map[string]experimentValue) (guidelines string, tmplUnknown, needsMonitors bool, monitorsCfg string) {
	for k, v := range own {
		if !strings.HasPrefix(k, "template__") {
			continue
		}
		if k == "template__system_prompts__guidelines" && v.StringValue != nil {
			sum := sha256.Sum256([]byte(*v.StringValue))
			if g, ok := knownGuidelinesTemplates[hex.EncodeToString(sum[:])]; ok {
				guidelines = *g
				continue
			}
		}
		tmplUnknown = true
	}
	allow := false
	if v, ok := own["allow-experimental-monitors"]; ok && v.BoolValue != nil && *v.BoolValue {
		allow = true
	}
	hasConfig := false
	for _, sh := range shared {
		if v, ok := sh["allow-experimental-monitors"]; ok && v.BoolValue != nil && *v.BoolValue {
			allow = true
		}
		for k, v := range sh {
			if strings.HasSuffix(k, "-monitors-config") {
				hasConfig = true
				if v.StringValue != nil {
					monitorsCfg = *v.StringValue
				}
			}
		}
	}
	return guidelines, tmplUnknown, allow && hasConfig, monitorsCfg
}

type Catalog struct {
	Models       map[string]ModelInfo
	DefaultModel string
	AgentModels  []string
	PaidTier     map[string]string
}

type catalogWire struct {
	Models map[string]struct {
		DisplayName     string   `json:"displayName"`
		Model           string   `json:"model"`
		MaxTokens       int64    `json:"maxTokens"`
		MaxOutputTokens int64    `json:"maxOutputTokens"`
		ThinkingLevel   int      `json:"thinkingLevel"`
		ExperimentIDs   []string `json:"modelExperimentIds"`
		Experiments     struct {
			Experiments map[string]experimentValue `json:"experiments"`
		} `json:"modelExperiments"`
	} `json:"models"`
	SharedExperiments map[string]struct {
		Experiments map[string]experimentValue `json:"experiments"`
	} `json:"sharedModelExperiments"`
	DefaultAgentModelID string `json:"defaultAgentModelId"`
	AgentModelSorts     []struct {
		Groups []struct {
			ModelIDs []string `json:"modelIds"`
		} `json:"groups"`
	} `json:"agentModelSorts"`
}

func parseCatalog(raw []byte) (*Catalog, error) {
	var w catalogWire
	if err := json.Unmarshal(raw, &w); err != nil {
		return nil, err
	}
	c := &Catalog{Models: map[string]ModelInfo{}, DefaultModel: w.DefaultAgentModelID}
	parsedMonitors := map[string]*MonitorsConfig{}
	for id, m := range w.Models {
		var shared []map[string]experimentValue
		for _, eid := range m.ExperimentIDs {
			if sh, ok := w.SharedExperiments[eid]; ok {
				shared = append(shared, sh.Experiments)
			}
		}
		g, tmpl, mon, monCfg := classifyExperiments(m.Experiments.Experiments, shared)
		info := ModelInfo{ID: id, DisplayName: m.DisplayName, Enum: m.Model, MaxTokens: m.MaxTokens,
			MaxOutputTokens: m.MaxOutputTokens, ThinkingLevel: m.ThinkingLevel,
			TemplateOverride: tmpl, Guidelines: g, NeedsMonitors: mon}
		if mon {
			if cached, ok := parsedMonitors[monCfg]; ok {
				info.Monitors = cached
			} else {
				info.Monitors = parseMonitorsConfig(monCfg)
				parsedMonitors[monCfg] = info.Monitors
			}
			if info.Monitors == nil {
				sum := sha256.Sum256([]byte(monCfg))
				info.UnverifiedMonitorsSHA = hex.EncodeToString(sum[:])[:16]
			}
		}
		c.Models[id] = info
	}
	for _, s := range w.AgentModelSorts {
		for _, g := range s.Groups {
			for _, id := range g.ModelIDs {
				if len(id) > 9 && id[len(id)-9:] == "-disabled" {
					continue
				}
				c.AgentModels = append(c.AgentModels, id)
			}
		}
		break
	}
	return c, nil
}

const (
	experimentsEvery = 60 * time.Second
	cacheFresh       = 300 * time.Second
	idleRefreshEvery = 360 * time.Second

	minRefreshWait = time.Second

	quotaCacheFresh = 10 * time.Minute
)

func (cs *ctrlState) untilIdleRefresh(now time.Time) time.Duration {
	cs.mu.Lock()
	last := cs.lastRefresh
	cs.mu.Unlock()
	if left := idleRefreshEvery - now.Sub(last); left > minRefreshWait {
		return left
	}
	return minRefreshWait
}

type ctrlState struct {
	mu          sync.Mutex
	catalog     *Catalog
	catalogErr  error
	lastRefresh time.Time
	lcaRaw      []byte
	quotaRaw    []byte
	quotaAt     time.Time
	quotaFetch  sync.Mutex
	ready       chan struct{}
	readyOnce   sync.Once
	refreshing  bool
}

func (d *Daemon) startControlPlane(ctx context.Context) {
	cs := &d.ctrl
	cs.ready = make(chan struct{})
	go func() {
		project := []byte(`{"project":"` + Project + `"}`)
		var picture string
		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
			b, code, err := d.controlCall(ctx, "GET", "https://www.googleapis.com/oauth2/v2/userinfo", nil, false)
			if err == nil && code == 200 {
				var u struct {
					Picture string `json:"picture"`
				}
				_ = json.Unmarshal(b, &u)
				picture = u.Picture
			}
		}()
		d.refreshCatalogPair(ctx)
		_, _, _ = d.controlCall(ctx, "POST", Endpoint+"/v1internal:fetchAdminControls", project, true)
		_, _, _ = d.controlCall(ctx, "POST", Endpoint+"/v1internal:listExperiments", project, true)
		wg.Wait()
		if picture != "" {
			_, _, _ = d.anonGet(ctx, picture)
		}
		_, _, _ = d.controlCall(ctx, "POST", Endpoint+"/v1internal:fetchUserInfo", project, true)
		d.fetchQuotaSummary(ctx)
		_, _, _ = d.controlCall(ctx, "GET", "https://aicode.googleapis.com/v1alpha:fetchConfig", nil, true)
		_, _, _ = d.controlCall(ctx, "GET", "https://aicode.googleapis.com/v1alpha:fetchLicenses", nil, true)

		expTick := time.NewTicker(experimentsEvery)

		idleTimer := time.NewTimer(cs.untilIdleRefresh(time.Now()))
		defer expTick.Stop()
		defer idleTimer.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-expTick.C:
				_, _, _ = d.controlCall(ctx, "POST", Endpoint+"/v1internal:listExperiments", project, true)
			case <-idleTimer.C:
				if cs.untilIdleRefresh(time.Now()) <= minRefreshWait {
					d.refreshCatalogPair(ctx)
				}
				idleTimer.Reset(cs.untilIdleRefresh(time.Now()))
			}
		}
	}()
}

func (d *Daemon) refreshCatalogPair(ctx context.Context) {
	cs := &d.ctrl
	cs.mu.Lock()
	if cs.refreshing {
		cs.mu.Unlock()
		return
	}
	cs.refreshing = true
	cs.lastRefresh = time.Now()
	cs.mu.Unlock()
	defer func() {
		cs.mu.Lock()
		cs.refreshing = false
		cs.mu.Unlock()
		cs.readyOnce.Do(func() { close(cs.ready) })
	}()
	var paid map[string]string
	if b, code, err := d.controlCall(ctx, "POST", Endpoint+"/v1internal:loadCodeAssist", []byte(`{"metadata":{"ideType":"ANTIGRAVITY"}}`), true); err == nil && code == 200 {
		cs.mu.Lock()
		cs.lcaRaw = b
		cs.mu.Unlock()
		var l struct {
			PaidTier map[string]any `json:"paidTier"`
		}
		if json.Unmarshal(b, &l) == nil && l.PaidTier != nil {
			paid = map[string]string{}
			for _, k := range []string{"name", "id", "upgradeSubscriptionUri"} {
				if v, ok := l.PaidTier[k].(string); ok {
					paid[k] = v
				}
			}
		}
	} else if err != nil {
		log.Printf("loadCodeAssist 失败: %v", err)
	}
	b, code, err := d.controlCall(ctx, "POST", Endpoint+"/v1internal:fetchAvailableModels", []byte(`{"project":"`+Project+`"}`), true)
	if err != nil || code != 200 {
		log.Printf("fetchAvailableModels 失败: code=%d err=%v", code, err)
		if err == nil {
			err = &APIError{HTTPStatus: code, Message: "model catalog request failed", Raw: string(b)}
		}
		cs.mu.Lock()
		cs.catalogErr = err
		cs.mu.Unlock()
		return
	}
	cat, err := parseCatalog(b)
	if err != nil {
		log.Printf("模型目录解析失败: %v", err)
		cs.mu.Lock()
		cs.catalogErr = fmt.Errorf("invalid model catalog: %w", err)
		cs.mu.Unlock()
		return
	}
	cat.PaidTier = paid
	cs.mu.Lock()
	cs.catalog = cat
	cs.catalogErr = nil
	cs.mu.Unlock()
	cs.readyOnce.Do(func() { close(cs.ready) })
}

func (d *Daemon) touchCatalog() {
	cs := &d.ctrl
	cs.mu.Lock()
	stale := time.Since(cs.lastRefresh) >= cacheFresh && !cs.lastRefresh.IsZero()
	cs.mu.Unlock()
	if stale {
		go d.refreshCatalogPair(d.ctx)
	}
}

func (d *Daemon) Catalog(ctx context.Context) (*Catalog, error) {
	select {
	case <-d.ctrl.ready:
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(60 * time.Second):
		return nil, fmt.Errorf("model catalog request timed out")
	}
	d.ctrl.mu.Lock()
	retry := d.ctrl.catalog == nil && time.Since(d.ctrl.lastRefresh) >= time.Second
	d.ctrl.mu.Unlock()
	if retry {
		d.refreshCatalogPair(ctx)
	}
	d.ctrl.mu.Lock()
	defer d.ctrl.mu.Unlock()
	if d.ctrl.catalog == nil {
		return nil, d.ctrl.catalogErr
	}
	return d.ctrl.catalog, nil
}
