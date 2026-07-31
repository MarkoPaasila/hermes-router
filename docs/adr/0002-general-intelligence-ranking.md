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
