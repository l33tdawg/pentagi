package sage

import (
	"context"

	"github.com/vxcontrol/langchaingo/llms"
)

// StepContext carries the per-step metadata that BeforeStep / AfterStep need
// to construct meaningful recall queries and storage domains.
//
// PentAGI's agent loop runs over llms.MessageContent slices (the langchaingo
// message type used throughout backend/pkg/providers/performer.go and
// backend/pkg/cast/chain_ast.go). The wrapper hooks operate on those slices
// directly so the same types flow through without any conversion shims.
type StepContext struct {
	AgentRole string // pentester | coder | memorist | searcher | adviser
	FlowID    int64
	TaskID    int64
	Target    string // host/url under test
}

// RecallMeta is what BeforeStep returns so AfterStep can correlate stored
// observations with the recall it ran.
type RecallMeta struct {
	Query string
	Hits  []MemoryResult // type from client.go
}

// BeforeStep auto-recalls memories relevant to the current step and prepends
// them as a system-role message to the chain. Model never decides to call
// recall — it sees the memories already injected. Bounded by env
// SAGE_WRAPPER_RECALL_BUDGET (default 800 tokens). On any SAGE error returns
// chain unchanged + empty meta — best-effort, agent must keep working.
func BeforeStep(
	ctx context.Context,
	client *Client,
	sc StepContext,
	chain []llms.MessageContent,
) ([]llms.MessageContent, RecallMeta, error) {
	// TODO(W1): build query from chain, call client.Embed + client.Recall, render hits
	return chain, RecallMeta{}, nil
}

// AfterStep extracts an observation from the most recent tool result and
// stores it. Fire-and-forget — runs in goroutine, errors logged not returned.
// Only stores high-signal events; skips noise.
func AfterStep(
	ctx context.Context,
	client *Client,
	sc StepContext,
	chain []llms.MessageContent,
	prev RecallMeta,
) error {
	// TODO(W1): inspect last tool call, decide whether to store, fire goroutine
	return nil
}
