package sage

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/vxcontrol/langchaingo/llms"
)

// --- Mock client -------------------------------------------------------------

// mockClient is a test-only SageClient that records calls instead of hitting
// a real SAGE node. It lives next to the real Client in this same package so
// it can use the unexported types freely; it implements SageClient via the
// public method signatures.
type mockClient struct {
	mu sync.Mutex

	enabled bool

	// programmable responses
	embedResp  []float32
	embedErr   error
	recallResp *RecallResponse
	recallErr  error
	rememberFn func(req RememberRequest) (*RememberResponse, error)

	// recorded calls
	embedCalls    []string
	recallCalls   []RecallRequest
	semanticCalls []RecallRequest
	rememberCalls []RememberRequest
	rememberDone  chan struct{} // closed by the test once Remember has been observed
	rememberOnce  sync.Once
}

func newMockClient() *mockClient {
	return &mockClient{
		enabled:      true,
		rememberDone: make(chan struct{}),
	}
}

func (m *mockClient) IsEnabled() bool { return m != nil && m.enabled }

func (m *mockClient) Embed(_ context.Context, text string) ([]float32, error) {
	m.mu.Lock()
	m.embedCalls = append(m.embedCalls, text)
	m.mu.Unlock()
	if m.embedErr != nil {
		return nil, m.embedErr
	}
	return m.embedResp, nil
}

func (m *mockClient) Recall(_ context.Context, req RecallRequest) (*RecallResponse, error) {
	m.mu.Lock()
	m.recallCalls = append(m.recallCalls, req)
	m.mu.Unlock()
	if m.recallErr != nil {
		return nil, m.recallErr
	}
	return m.recallResp, nil
}

func (m *mockClient) RecallSemantic(_ context.Context, req RecallRequest) (*RecallResponse, error) {
	m.mu.Lock()
	m.semanticCalls = append(m.semanticCalls, req)
	m.mu.Unlock()
	if m.recallErr != nil {
		return nil, m.recallErr
	}
	return m.recallResp, nil
}

func (m *mockClient) Remember(_ context.Context, req RememberRequest) (*RememberResponse, error) {
	m.mu.Lock()
	m.rememberCalls = append(m.rememberCalls, req)
	m.mu.Unlock()
	m.rememberOnce.Do(func() { close(m.rememberDone) })
	if m.rememberFn != nil {
		return m.rememberFn(req)
	}
	return &RememberResponse{Success: true, Status: "committed"}, nil
}

// snapshot returns the recorded slices under the lock.
func (m *mockClient) snapshot() (embeds []string, recalls, semantics []RecallRequest, remembers []RememberRequest) {
	m.mu.Lock()
	defer m.mu.Unlock()
	embeds = append([]string(nil), m.embedCalls...)
	recalls = append([]RecallRequest(nil), m.recallCalls...)
	semantics = append([]RecallRequest(nil), m.semanticCalls...)
	remembers = append([]RememberRequest(nil), m.rememberCalls...)
	return
}

// --- Helpers -----------------------------------------------------------------

func humanMsg(text string) llms.MessageContent {
	return llms.MessageContent{
		Role:  llms.ChatMessageTypeHuman,
		Parts: []llms.ContentPart{llms.TextContent{Text: text}},
	}
}

func aiToolCallMsg(callID, toolName, args string) llms.MessageContent {
	return llms.MessageContent{
		Role: llms.ChatMessageTypeAI,
		Parts: []llms.ContentPart{llms.ToolCall{
			ID:           callID,
			Type:         "function",
			FunctionCall: &llms.FunctionCall{Name: toolName, Arguments: args},
		}},
	}
}

func toolRespMsg(callID, toolName, content string) llms.MessageContent {
	return llms.MessageContent{
		Role: llms.ChatMessageTypeTool,
		Parts: []llms.ContentPart{llms.ToolCallResponse{
			ToolCallID: callID,
			Name:       toolName,
			Content:    content,
		}},
	}
}

func sysMessageText(msg llms.MessageContent) string {
	if msg.Role != llms.ChatMessageTypeSystem {
		return ""
	}
	var b strings.Builder
	for _, p := range msg.Parts {
		if tc, ok := p.(llms.TextContent); ok {
			b.WriteString(tc.Text)
		}
	}
	return b.String()
}

// --- BeforeStep tests --------------------------------------------------------

func TestBeforeStep_NilClient(t *testing.T) {
	chain := []llms.MessageContent{humanMsg("scan the host")}
	out, meta, err := BeforeStep(context.Background(), nil, StepContext{}, chain)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(out) != len(chain) {
		t.Fatalf("expected chain unchanged, got len %d (was %d)", len(out), len(chain))
	}
	if meta.Query != "" || len(meta.Hits) != 0 {
		t.Fatalf("expected empty meta, got %+v", meta)
	}
}

func TestBeforeStep_NoHits(t *testing.T) {
	mock := newMockClient()
	mock.recallResp = &RecallResponse{Memories: nil, TotalCount: 0}

	chain := []llms.MessageContent{humanMsg("scan target.local")}
	sc := StepContext{AgentRole: "pentester", Target: "target.local"}

	out, meta, err := BeforeStep(context.Background(), mock, sc, chain)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(out) != len(chain) {
		t.Fatalf("expected chain unchanged on zero hits, got len %d", len(out))
	}
	if len(meta.Hits) != 0 {
		t.Fatalf("expected meta.Hits empty, got %d", len(meta.Hits))
	}
	if meta.Query == "" {
		t.Fatalf("expected meta.Query populated even with zero hits")
	}

	_, _, semantics, _ := mock.snapshot()
	if len(semantics) != 1 {
		t.Fatalf("expected 1 RecallSemantic call, got %d", len(semantics))
	}
	if semantics[0].Domain != "pentest:pentester" {
		t.Fatalf("expected domain pentest:pentester, got %q", semantics[0].Domain)
	}
	if semantics[0].MaxResults != defaultRecallTopK {
		t.Fatalf("expected MaxResults=%d, got %d", defaultRecallTopK, semantics[0].MaxResults)
	}
	if semantics[0].MinConfidence != defaultMinConf {
		t.Fatalf("expected MinConfidence=%v, got %v", defaultMinConf, semantics[0].MinConfidence)
	}
}

func TestBeforeStep_HitsPrepended(t *testing.T) {
	mock := newMockClient()
	mock.recallResp = &RecallResponse{
		Memories: []MemoryResult{
			{MemoryID: "m1", Content: "port 22 has openssh 8.2", MemoryType: "observation", Confidence: 0.8},
			{MemoryID: "m2", Content: "subdomain enum found admin panel", MemoryType: "observation", Confidence: 0.75},
			{MemoryID: "m3", Content: "creds dump from prior run", MemoryType: "fact", Confidence: 0.95},
		},
		TotalCount: 3,
	}

	chain := []llms.MessageContent{humanMsg("enumerate target.local")}
	sc := StepContext{AgentRole: "pentester", Target: "target.local"}

	out, meta, err := BeforeStep(context.Background(), mock, sc, chain)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(out) != len(chain)+1 {
		t.Fatalf("expected chain to grow by 1, got len %d (was %d)", len(out), len(chain))
	}
	if out[0].Role != llms.ChatMessageTypeSystem {
		t.Fatalf("expected first message to be system role, got %q", out[0].Role)
	}
	body := sysMessageText(out[0])
	if !strings.Contains(body, "openssh") || !strings.Contains(body, "admin panel") {
		t.Fatalf("expected hits rendered into system message, got: %q", body)
	}
	if !strings.Contains(body, "ambient memories") {
		t.Fatalf("expected ambient-memory header in body: %q", body)
	}
	if len(meta.Hits) != 3 {
		t.Fatalf("expected 3 hits in meta, got %d", len(meta.Hits))
	}
}

func TestBeforeStep_TokenBudgetEnforced(t *testing.T) {
	t.Setenv("SAGE_WRAPPER_RECALL_BUDGET", "50") // 50 tokens * 4 chars = 200 chars total

	mock := newMockClient()
	huge := strings.Repeat("x", 4096)
	mock.recallResp = &RecallResponse{
		Memories: []MemoryResult{
			{MemoryID: "m1", Content: huge, MemoryType: "observation", Confidence: 0.8},
			{MemoryID: "m2", Content: huge, MemoryType: "observation", Confidence: 0.8},
		},
	}

	chain := []llms.MessageContent{humanMsg("query")}
	sc := StepContext{AgentRole: "pentester"}

	out, _, err := BeforeStep(context.Background(), mock, sc, chain)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(out) != len(chain)+1 {
		t.Fatalf("expected one prepended system message, got total %d", len(out))
	}
	body := sysMessageText(out[0])
	// Char budget = 50 * charsPerToken = 200; allow header + first hit truncated.
	const maxAllowed = 50 * charsPerToken
	if len(body) > maxAllowed {
		t.Fatalf("token budget not enforced: body len=%d > budget=%d", len(body), maxAllowed)
	}
}

func TestBeforeStep_RecallError_BestEffort(t *testing.T) {
	mock := newMockClient()
	mock.recallErr = errors.New("network down")

	chain := []llms.MessageContent{humanMsg("query")}
	out, meta, err := BeforeStep(context.Background(), mock, StepContext{AgentRole: "pentester"}, chain)
	if err != nil {
		t.Fatalf("BeforeStep must not return error on SAGE failure, got: %v", err)
	}
	if len(out) != len(chain) {
		t.Fatalf("expected chain unchanged, got len %d", len(out))
	}
	if len(meta.Hits) != 0 {
		t.Fatalf("expected empty meta on error")
	}
}

// --- AfterStep tests ---------------------------------------------------------

func TestAfterStep_LowSignalToolSkipped(t *testing.T) {
	mock := newMockClient()

	chain := []llms.MessageContent{
		humanMsg("read a file"),
		aiToolCallMsg("call-1", "file", `{"path":"/etc/passwd"}`),
		toolRespMsg("call-1", "file", "root:x:0:0:..."),
	}

	if err := AfterStep(context.Background(), mock, StepContext{}, chain, RecallMeta{}); err != nil {
		t.Fatalf("AfterStep unexpected error: %v", err)
	}

	// Allow any goroutine work to settle (there shouldn't be any).
	time.Sleep(20 * time.Millisecond)

	_, _, _, remembers := mock.snapshot()
	if len(remembers) != 0 {
		t.Fatalf("expected no Remember call for low-signal tool, got %d", len(remembers))
	}
}

func TestAfterStep_HighSignalStored(t *testing.T) {
	mock := newMockClient()

	chain := []llms.MessageContent{
		humanMsg("scan target"),
		aiToolCallMsg("call-1", "terminal", `{"input":"nmap -sV target.local"}`),
		toolRespMsg("call-1", "terminal", "Nmap scan report for target.local\n22/tcp open ssh OpenSSH 8.2"),
	}
	sc := StepContext{AgentRole: "pentester", Target: "target.local"}

	if err := AfterStep(context.Background(), mock, sc, chain, RecallMeta{}); err != nil {
		t.Fatalf("AfterStep unexpected error: %v", err)
	}

	select {
	case <-mock.rememberDone:
	case <-time.After(2 * time.Second):
		t.Fatalf("Remember was never called within timeout")
	}

	_, _, _, remembers := mock.snapshot()
	if len(remembers) != 1 {
		t.Fatalf("expected 1 Remember call, got %d", len(remembers))
	}
	r := remembers[0]
	if r.MemoryType != "observation" {
		t.Fatalf("expected memory_type=observation, got %q", r.MemoryType)
	}
	// storeDomain == recallDomain by design (so BeforeStep can recall what
	// AfterStep just stored). Target metadata travels in the content, not
	// the domain segment.
	if r.Domain != "pentest:pentester" {
		t.Fatalf("expected domain pentest:pentester, got %q", r.Domain)
	}
	if r.Confidence != 0.7 {
		t.Fatalf("expected confidence 0.7, got %v", r.Confidence)
	}
	if !strings.Contains(r.Content, "tool=terminal") {
		t.Fatalf("expected content to include tool name; got %q", r.Content)
	}
	if !strings.Contains(r.Content, "OpenSSH 8.2") {
		t.Fatalf("expected content to include tool result; got %q", r.Content)
	}
}

func TestAfterStep_BarrierToolStoredAsFact(t *testing.T) {
	mock := newMockClient()

	chain := []llms.MessageContent{
		humanMsg("finish up"),
		aiToolCallMsg("c1", "done", `{"success":true}`),
		toolRespMsg("c1", "done", "task complete"),
	}

	if err := AfterStep(context.Background(), mock, StepContext{AgentRole: "primary_agent"}, chain, RecallMeta{}); err != nil {
		t.Fatalf("AfterStep unexpected error: %v", err)
	}

	select {
	case <-mock.rememberDone:
	case <-time.After(2 * time.Second):
		t.Fatalf("Remember was never called within timeout")
	}

	_, _, _, remembers := mock.snapshot()
	if len(remembers) != 1 {
		t.Fatalf("expected 1 Remember call, got %d", len(remembers))
	}
	if remembers[0].MemoryType != "fact" {
		t.Fatalf("expected memory_type=fact for barrier tool, got %q", remembers[0].MemoryType)
	}
}

func TestAfterStep_FailurePathLogsNoPanic(t *testing.T) {
	mock := newMockClient()

	var rememberCalled int32
	mock.rememberFn = func(req RememberRequest) (*RememberResponse, error) {
		atomic.AddInt32(&rememberCalled, 1)
		return nil, errors.New("simulated SAGE failure")
	}

	chain := []llms.MessageContent{
		humanMsg("scan"),
		aiToolCallMsg("c1", "terminal", `{"input":"id"}`),
		toolRespMsg("c1", "terminal", "uid=0(root) gid=0(root)"),
	}

	if err := AfterStep(context.Background(), mock, StepContext{AgentRole: "pentester"}, chain, RecallMeta{}); err != nil {
		t.Fatalf("AfterStep should not return error even when Remember fails: %v", err)
	}

	select {
	case <-mock.rememberDone:
	case <-time.After(2 * time.Second):
		t.Fatalf("expected Remember to be invoked even though it errors")
	}

	if atomic.LoadInt32(&rememberCalled) != 1 {
		t.Fatalf("expected exactly one Remember invocation, got %d", rememberCalled)
	}
}

func TestAfterStep_NoToolMessageInChain(t *testing.T) {
	mock := newMockClient()
	chain := []llms.MessageContent{humanMsg("hello")}

	if err := AfterStep(context.Background(), mock, StepContext{}, chain, RecallMeta{}); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	time.Sleep(20 * time.Millisecond)

	_, _, _, remembers := mock.snapshot()
	if len(remembers) != 0 {
		t.Fatalf("expected no Remember when chain has no tool exchange")
	}
}

// --- Internal helper checks --------------------------------------------------

func TestStoreDomain_MatchesRecallDomain(t *testing.T) {
	// Regression: storeDomain previously included a target segment that
	// recallDomain didn't, so AfterStep wrote to "pentest:<target>:<role>"
	// while BeforeStep queried "pentest:<role>" and never found those
	// writes. They must agree exactly.
	cases := []StepContext{
		{AgentRole: "pentester", Target: "172.28.0.10"},
		{AgentRole: "pentester", Target: ""},
		{AgentRole: "", Target: "host.local"},
		{AgentRole: "memorist", Target: "https://example.com/path?q=1"},
	}
	for _, sc := range cases {
		if got, want := storeDomain(sc), recallDomain(sc); got != want {
			t.Errorf("storeDomain(%+v) = %q, recallDomain = %q — must match", sc, got, want)
		}
	}
}
