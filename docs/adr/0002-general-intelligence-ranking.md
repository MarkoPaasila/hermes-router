# General intelligence ranking replaces Capability

**Status:** accepted (implemented)

**Supersedes (partially):** [ADR-0001](0001-invert-complexity-and-capability-scales.md) capability scale — complexity 1–5 (higher = harder) remains; model strength is no longer 1–5 Capability.

## Context

Capability as a discrete 1–5 score was too coarse, overloaded with “feature capability” language, and hand-maintained from name patterns. We want a continuous **general intelligence ranking (GI)** grounded in external leaderboards, with operator overrides.

## Decision

- GI is **0–100** (higher = stronger).
- Defaults: checked-in `gi_rankings.json` = median of min–max-normalized LMSYS Arena and Artificial Analysis scores (maintainer script; no live fetch in the proxy).
- Resolve: dashboard override → snapshot → **0**.
- Complexity maps to a minimum GI (defaults 0/20/40/60/80); selection picks the cheapest candidate that clears the bar.
- Wire/status field: `gi` + `gi_source`. Drop `MODEL_QUALITY_RANKS`.
- Feature probing (tools/vision/reasoning) stays separate.

## Consequences

- Legacy 1–5 `rating` values in probe state are not mapped into GI.
- Operators set/clear scores from the Models modal (`gi_overrides.json`).
- Docs and `CONTEXT.md` use GI / feature probing instead of Capability for strength.
- Snapshot may include an `aliases` map (normalized catalog id → canonical key). The refresh
  script can propose aliases with a maintainer-only LLM (`--llm`); the proxy never calls
  leaderboards or LLMs for GI. With `--catalog`, refresh fails if coverage is below 80%.
  Matching also normalizes ids (strip `org/`, `:tag`, trailing `-free`/`_free`, quants)
  and uses longest **contained** snapshot key (never a longer sibling key containing the
  candidate), ignoring keys shorter than 4 characters. Specialty modality tokens
  (`image`, `veo`, `live`, `omni`, `translate`, `computer-use`) block chat-key inheritance
  so image/video/live SKUs stay at 0 unless exact or aliased. Snapshot and override files
  hot-reload when their mtime changes. GI is chat-Arena based; non-chat specialty models
  correctly default to 0.
