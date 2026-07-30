# Models Dashboard Combined Table + Bar Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One useful Models page: no model override form; combined capabilities + rate headroom table; click opens a bar-based buckets modal (all keys stacked).

**Architecture:** Frontend-only changes inside `_DASHBOARD_HTML` in `router.py`. Join `/v1/status` model rows with `/v1/rate-limits` model-scope groups in JS. Shared `#rl-detail-modal` switches from a column table to bar rows; Models passes multiple groups, Providers still passes one.

**Tech Stack:** Flask-served HTML/CSS/JS in `router.py`; existing `/v1/status` + `/v1/rate-limits`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-30-models-dashboard-ui-design.md`
- One row per provider × model (not per key)
- Headroom = min across that model’s key-groups; limiting factor = `binding` of the min-headroom group
- Key column = colored status dots (reuse `keyDots`), not `…hint` text
- Model is visual primary; provider secondary/muted
- No Buckets count column; no top Provider Model chooser
- Modal: no columns; used inside bar (or beside if fill too small); cap at end; order M→H→D→W→Mo; RP\* then separator then TP\*
- Multi-key Models modal: stacked sections with per-section Clear
- Keep `/v1/config/model` API and CLI; only remove dashboard UI
- Update docs that claim the dashboard sets model overrides

---

## File Structure

| File | Responsibility |
|---|---|
| `router.py` | Dashboard HTML/CSS/JS: Models page, join renderer, bar modal |
| `documentation/configuration.md` | Models page wording |
| `documentation/monitoring.md` | Models page wording |
| `website/src/content/docs/configuration.md` | Mirror |
| `website/src/content/docs/monitoring.md` | Mirror |

See Cursor plan / chat for task checklist (strip page → join render → bar modal → docs).
