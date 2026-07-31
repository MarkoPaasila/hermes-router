# Invert complexity and capability so higher means more

**Status:** accepted (implemented)

Shipping code and docs scored request complexity **1 = hardest … 5 = easiest** and model capability **1 = strongest … 5 = weakest** (catalog match: `rating <= complexity`). We decided the ubiquitous language is the opposite: **Complexity 1 = easiest … 5 = hardest**, **Capability 1 = weakest … 5 = strongest**, so “higher” always means “more.” `CONTEXT.md` is the target; code, docs, and UI renumber together.

Persisted `router_state.json` ratings migrate via `scale_version` (v1 → v2 flips stored scores with `6 - n`).

## Considered options

- Keep shipping scales; only rename “difficulty” → “complexity”
- Invert both scales so higher = more (chosen)

## Consequences

- Selection/sort logic uses `capability >= complexity` on the new scale
- Wire JSON still exposes capability as `rating` inside `model_caps` for compatibility; UI labels say Capability
