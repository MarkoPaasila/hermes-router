# Models Dashboard Combined Table + Bar Modal

**Date:** 2026-07-30  
**Status:** Approved  
**Approach:** Frontend-only join of status + rate-limits; shared bar modal

## Problem

The dashboard Models page had three disconnected panels: an unused Provider Model chooser, a capabilities table without rate data, and a separate model token-buckets table. Operators needed one place to see model capabilities, headroom, and limiting factor, and to inspect all buckets without a dense column table.

## Goals

1. Remove the top **Provider Model** chooser (no dashboard model override UI).
2. Combine capabilities and token-bucket data into **one row per provider × model**.
3. Show **headroom** (min across keys) and **limiting factor** (binding of the min-headroom key-group).
4. Replace key text with **colored status dots**.
5. Make **model** the visual primary; provider secondary.
6. Drop the **Buckets** count column; row click opens a modal with all keys’ buckets.
7. Modal uses **bars** (used inside / beside, cap at end), ordered minute→month, with a separator between RP\* and TP\*.

## Non-goals

- Backend API shape changes for `/v1/status` or `/v1/rate-limits`.
- Removing CLI / `POST /v1/config/model/<provider>` (dashboard UI only).
- Changing the Providers page summary table columns (Binding / Headroom / Buckets count stay).
- One row per key×model.

## Page structure

`#models` has a single **Models** panel:

- Intro copy + “Show dormant / orphan groups” toggle
- One table (no separate capabilities or token-buckets panels)
- No Provider Model select/save form

## Combined table

| Column | Content |
|---|---|
| Model | Strong/mono model id (primary) |
| Provider | Muted secondary |
| Key | Colored status dots (`keyDots`); match by key hint |
| Rating | Existing pips |
| Tools / Reasoning | Existing yes/no pills |
| Limiting factor | `binding` of the key-group with lowest headroom (else —) |
| Headroom | Bar + %; **min** headroom across that model’s key-groups (null → —) |

**Join rules**

- Always include configured models from `/v1/status`.
- Attach model-scope groups from `/v1/rate-limits` by `(provider, model)`.
- With orphan toggle on, also include orphan model groups not in the configured roster.
- Unmatched rate-group hints → muted dot with `…hint` tooltip.
- Default sort: headroom ascending. Sortable: model, provider, limiting factor, headroom, rating.
- Row click → modal with **all** groups for that provider×model (sorted by key_hint).

## Buckets modal

Shared `#rl-detail-modal` (Models + Providers) drops the column table.

- **Models (multi-key):** stacked sections; title = model · provider; each section = key dot + `…hint` + **Clear learned state** for that group id.
- **Providers:** same bar layout; one group as today.
- Top-level Clear removed when more than one key section.

**Per key, bucket bars**

1. RP\* block, visual separator, TP\* block.
2. Within each block: `M → H → D → W → Mo`.
3. Row: label · track · fill (`used/cap`) · **used** inside fill when wide enough (~≥28px), else beside track · **cap** after track.
4. Inactive buckets visible but muted.

## Out of scope / docs

- Keep `/v1/config/model` for CLI.
- Docs that say the dashboard saves model overrides must be updated to describe the combined table + bar modal.
