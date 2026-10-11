#!/usr/bin/env python3
"""Legacy internal model metadata validation. All public card footers are disabled.

This module does not query Grok, credentials or private routing APIs.
Routing slots must never be interpreted as actual models.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional

OFFICIAL_MODELS_URL = "https://cursor.com/help/grok-bot/models"

ROUTED_MODEL_IDS = frozenset({
    "default", "premium",
    "auto", "auto-low", "auto-medium", "auto-high", "auto-smart",
})
# Routing config IDs — not models. Never surface as runtime model.
SAND_SLOT_IDS = frozenset({
    "sand-default", "sand-automation", "sand-cua",
})

OFFICIAL_ROUTING_SOURCE = "official-routing"
OFFICIAL_ROUTING_FOOTER = "模型：自动选择（具体模型未公开）"

TURN_MODEL_KEYS = (
    "resolved_model_id", "resolved_model",
    "turn_model_id", "turn_model",
    "runtime_model_id", "runtime_model",
    "actual_model_id", "actual_model",
)

EscapeFn = Callable[[str], str]


def _norm_id(raw: Optional[str]) -> str:
    return (raw or "").strip()


def escape_model_label(text: str) -> str:
    """Escape for Feishu lark-md: no raw @ / <at> / markdown links."""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"</?\s*at\b[^>]*>", "", s, flags=re.I)
    s = re.sub(r"</?\s*link\b[^>]*>", "", s, flags=re.I)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace('"', "&quot;").replace("'", "&#39;")
    s = s.replace("\n", " ").replace("\r", " ")
    s = re.sub(r"@\S+", lambda m: m.group(0).replace("@", "＠"), s)
    return s


def _is_routed_or_auto(model_id: str) -> bool:
    mid = model_id.strip().lower()
    if not mid:
        return True
    if mid in ROUTED_MODEL_IDS:
        return True
    if mid.startswith("auto/") or mid.startswith("auto-") or mid == "auto":
        return True
    if mid in ("…", "...", "auto/…", "auto/...", "unknown", "n/a", "na"):
        return True
    return False


def _is_sand_slot(model_id: str) -> bool:
    mid = _norm_id(model_id)
    if not mid:
        return False
    inner = mid[1:-1] if mid.startswith("<") and mid.endswith(">") else mid
    return inner.lower() in SAND_SLOT_IDS


def _is_concrete_model_id(model_id: str) -> bool:
    """True for a concrete non-routed, non-sand model id (future turn metadata)."""
    mid = _norm_id(model_id)
    if not mid or _is_routed_or_auto(mid) or _is_sand_slot(mid):
        return False
    if "…" in mid or mid in (".", "..", "..."):
        return False
    inner = mid[1:-1] if mid.startswith("<") and mid.endswith(">") else mid
    if _is_sand_slot(inner) or _is_routed_or_auto(inner):
        return False
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$", inner):
        return True
    return False


def _is_acceptable_config_label(raw: str) -> bool:
    """Explicit --model value: concrete id OR sand-* routing-config label."""
    mid = _norm_id(raw)
    if not mid or _is_routed_or_auto(mid):
        return False
    if "…" in mid or mid.lower() in ("unknown", "n/a", "na"):
        return False
    inner = mid[1:-1] if mid.startswith("<") and mid.endswith(">") else mid
    if _is_routed_or_auto(inner):
        return False
    if inner.lower() in SAND_SLOT_IDS:
        return True
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$", inner):
        return True
    return False


def _strip_angles(mid: str) -> str:
    mid = _norm_id(mid)
    if mid.startswith("<") and mid.endswith(">") and len(mid) > 2:
        return mid[1:-1].strip()
    return mid


def _result(
    model_id: Optional[str],
    model_name: Optional[str],
    source: str,
    *,
    notes: Optional[str] = None,
    model_kind: Optional[str] = None,
) -> Dict[str, Any]:
    mid = _norm_id(model_id) or None
    if mid:
        name = _norm_id(model_name) or mid
        if _is_routed_or_auto(name) or name.lower() in ("unknown", "n/a"):
            name = mid
    else:
        name = None
    out: Dict[str, Any] = {
        "model_id": mid,
        "model_name": name,
        "model_source": source,
        "official_docs": OFFICIAL_MODELS_URL,
    }
    if model_kind:
        out["model_kind"] = _norm_id(model_kind)
    if notes:
        out["notes"] = notes
    return out


def _official_routing(notes: Optional[str] = None) -> Dict[str, Any]:
    return _result(None, None, OFFICIAL_ROUTING_SOURCE, notes=notes)


def _from_explicit_value(
    raw: Optional[str],
    *,
    model_source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build config result from an explicit label string. Always model_kind=config."""
    raw = _norm_id(raw)
    if not raw:
        return None
    mid = _strip_angles(raw)
    if not _is_acceptable_config_label(mid) and not _is_acceptable_config_label(raw):
        return _official_routing(
            notes="explicit --model was routed/Auto-style; refused per official policy"
        )
    if not _is_acceptable_config_label(mid):
        mid = _strip_angles(raw)
    src = _norm_id(model_source) or "explicit"
    return _result(mid, mid, src, model_kind="config")


def _env_model_override() -> str:
    return (
        _norm_id(os.environ.get("FEISHU_PROGRESS_MODEL"))
        or _norm_id(os.environ.get("FEISHU_MODEL_ID"))
    )


def _from_explicit(
    override: Optional[str],
    *,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,  # ignored for kind — always config
    read_env: bool = True,
) -> Optional[Dict[str, Any]]:
    """Manual --model only. Always model_kind=config regardless of flags.

    When read_env=False, only the override argument is considered (frozen-snapshot
    paths must pass read_env=False so FEISHU_PROGRESS_MODEL cannot clobber).
    """
    del model_kind
    raw = _norm_id(override)
    if not raw and read_env:
        raw = _env_model_override()
        if raw and not _norm_id(model_source):
            model_source = _norm_id(os.environ.get("FEISHU_MODEL_SOURCE")) or "explicit"
    return _from_explicit_value(raw, model_source=model_source)


def _from_task_turn_context(existing: Optional[dict]) -> Optional[Dict[str, Any]]:
    """Task/turn runtime metadata bound to THIS task — never sand-* slots."""
    if not isinstance(existing, dict):
        return None
    blobs: List[dict] = [existing]
    for nest_key in ("turn", "runtime", "model", "resolution", "last_turn"):
        nested = existing.get(nest_key)
        if isinstance(nested, dict):
            blobs.append(nested)
    for blob in blobs:
        for key in TURN_MODEL_KEYS:
            mid = _strip_angles(_norm_id(blob.get(key)))
            if mid and _is_concrete_model_id(mid):
                src = _norm_id(blob.get("model_source")) or "task-turn"
                return _result(mid, mid, src, model_kind="turn-resolved")
            if mid and _is_sand_slot(mid):
                # sand-* is a routing config id, not a model — ignore.
                continue
        mid = _strip_angles(_norm_id(blob.get("model_id")))
        kind = _norm_id(blob.get("model_kind")).lower()
        src = _norm_id(blob.get("model_source"))
        # Frozen official-routing snapshot: keep it (caller may short-circuit earlier).
        if src == OFFICIAL_ROUTING_SOURCE and not mid:
            return _official_routing(notes="frozen task snapshot")
        # Config already persisted on task.
        if mid and kind == "config" and _is_acceptable_config_label(mid):
            name = _norm_id(blob.get("model_name")) or mid
            return _result(mid, name, src or "explicit", model_kind="config")
        # Concrete turn/runtime model (never sand-*).
        if mid and _is_concrete_model_id(mid) and kind in ("turn-resolved", "runtime", ""):
            name = _norm_id(blob.get("model_name")) or mid
            if _is_sand_slot(name) or _is_routed_or_auto(name) or name.lower() == "unknown":
                name = mid
            out_kind = kind if kind in ("turn-resolved", "runtime") else "turn-resolved"
            return _result(mid, name, src or "task-turn", model_kind=out_kind)
        if mid and _is_sand_slot(mid):
            continue
    return None


def resolve_model(
    *,
    override: Optional[str] = None,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,
    prefer_automation: bool = True,
    existing: Optional[dict] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Resolve model for a task.

    Cascade (DEFAULT has NO Statsig / transcript / profile / host settings):
      1) explicit --model → config
      2) task/turn concrete non-sand metadata (same task only)
      3) official-routing (null/null)
    """
    del prefer_automation  # routing slots are not models; unused
    probes: List[str] = []

    # 1) Explicit --model ARG only (never env here — env must not beat frozen snapshot)
    if _norm_id(override):
        hit = _from_explicit(
            override, model_source=model_source, model_kind=model_kind, read_env=False,
        )
        if hit:
            hit["probes"] = probes
            return hit

    # 2) Frozen / task-bound snapshot (including official-routing nulls)
    #    MUST run before env so FEISHU_PROGRESS_MODEL cannot rewrite a task.
    if isinstance(existing, dict) and not refresh:
        bound = existing.get("model_bound_to_task") or existing.get("model_source")
        if bound:
            src = _norm_id(existing.get("model_source"))
            mid = _norm_id(existing.get("model_id")) or None
            kind = _norm_id(existing.get("model_kind"))
            # Reject frozen sand-* pretending to be runtime → official-routing
            if mid and _is_sand_slot(mid) and kind in ("runtime", "turn-resolved"):
                probes.append("rejected frozen sand-* runtime slot")
                out = _official_routing(notes="rejected sand-* runtime snapshot")
                out["probes"] = probes
                return out
            elif src == OFFICIAL_ROUTING_SOURCE or (not mid and src):
                out = _official_routing(notes="frozen task snapshot")
                if kind:
                    # official-routing has no kind
                    pass
                out["probes"] = probes
                # Preserve config snapshot
                if kind == "config" and mid and _is_acceptable_config_label(mid):
                    name = _norm_id(existing.get("model_name")) or mid
                    out = _result(mid, name, src or "explicit", model_kind="config")
                    out["probes"] = probes
                elif mid and _is_concrete_model_id(mid):
                    name = _norm_id(existing.get("model_name")) or mid
                    out = _result(
                        mid, name, src or "task-turn",
                        model_kind=kind or "turn-resolved",
                    )
                    out["probes"] = probes
                return out
            elif kind == "config" and mid and _is_acceptable_config_label(mid):
                name = _norm_id(existing.get("model_name")) or mid
                out = _result(mid, name, src or "explicit", model_kind="config")
                out["probes"] = probes
                return out
            elif mid and _is_concrete_model_id(mid):
                name = _norm_id(existing.get("model_name")) or mid
                out = _result(
                    mid, name, src or "task-turn",
                    model_kind=kind or "turn-resolved",
                )
                out["probes"] = probes
                return out

    # 3) Env override only for NEW / unbound tasks (after frozen check)
    env_raw = _env_model_override()
    if env_raw:
        hit = _from_explicit(
            None, model_source=model_source or _norm_id(os.environ.get("FEISHU_MODEL_SOURCE")),
            model_kind=model_kind, read_env=True,
        )
        if hit and (hit.get("model_id") or hit.get("model_source") == OFFICIAL_ROUTING_SOURCE):
            hit["probes"] = probes + ["env override (new task)"]
            return hit

    # 4) Task/turn concrete non-sand metadata
    hit = _from_task_turn_context(existing)
    if hit:
        if hit.get("model_source") == OFFICIAL_ROUTING_SOURCE and not hit.get("model_id"):
            hit["probes"] = probes
            return hit
        if hit.get("model_id") and (
            hit.get("model_kind") == "config"
            or _is_concrete_model_id(hit["model_id"])
        ):
            hit["probes"] = probes
            return hit

    probes.append("no Statsig/profile/host in default path (official docs)")
    probes.append("skipped hardcoded vendor model id (not disclosed)")
    out = _official_routing(
        notes="Grok Bot does not expose per-request model (%s)" % OFFICIAL_MODELS_URL
    )
    out["probes"] = probes
    return out


def apply_model_to_state(
    state: dict,
    resolved: Optional[dict] = None,
    *,
    override: Optional[str] = None,
    model_source: Optional[str] = None,
    model_kind: Optional[str] = None,
    prefer_automation: bool = True,
    refresh: bool = False,
) -> dict:
    """Persist model snapshot on task state (including official-routing nulls).

    No explicit override/refresh + existing snapshot → preserve snapshot verbatim.
    Does NOT call resolve_model in that case (so FEISHU_PROGRESS_MODEL / global
    env cannot rewrite a frozen official-routing or config snapshot). Heartbeat
    paths must rely on this freeze.
    """
    if resolved is None:
        if (
            not override
            and not refresh
            and (state.get("model_bound_to_task") or state.get("model_source"))
        ):
            # Preserve frozen snapshot — ignore global env entirely.
            state["model_bound_to_task"] = True
            state.setdefault("model_docs", OFFICIAL_MODELS_URL)
            if not state.get("model_source"):
                state["model_source"] = OFFICIAL_ROUTING_SOURCE
            return state
        resolved = resolve_model(
            override=override,
            model_source=model_source,
            model_kind=model_kind,
            prefer_automation=prefer_automation,
            existing=state,
            refresh=refresh,
        )
    state["model_id"] = resolved.get("model_id")  # may be None
    state["model_name"] = resolved.get("model_name")  # may be None
    state["model_source"] = resolved.get("model_source") or OFFICIAL_ROUTING_SOURCE
    if resolved.get("model_kind"):
        state["model_kind"] = resolved["model_kind"]
    else:
        state.pop("model_kind", None)
    if resolved.get("notes"):
        state["model_notes"] = resolved["notes"]
    if resolved.get("probes"):
        state["model_probes"] = list(resolved["probes"])[:12]
    state["model_docs"] = OFFICIAL_MODELS_URL
    state["model_bound_to_task"] = True
    return state


def model_footer_display(
    state_or_resolved: Optional[dict],
    *,
    escape_fn: Optional[EscapeFn] = None,
) -> str:
    """Full content for element_id=model_footer. Never Auto/…/unknown/官方路由."""
    esc = escape_fn or escape_model_label
    if not state_or_resolved:
        return OFFICIAL_ROUTING_FOOTER
    mid = _norm_id(state_or_resolved.get("model_id"))
    name = _norm_id(state_or_resolved.get("model_name"))
    kind = _norm_id(state_or_resolved.get("model_kind")).lower()
    src = _norm_id(state_or_resolved.get("model_source"))

    # Manual config: only acceptable labels; malformed → truthful default
    if kind == "config" and mid:
        if _is_acceptable_config_label(mid):
            return "配置模型：%s（实际模型未公开）" % esc(_strip_angles(mid))
        # Invalid / malformed config id — do not render raw junk
        return OFFICIAL_ROUTING_FOOTER

    # sand-* must never appear as an "actual" runtime model
    if mid and _is_sand_slot(mid):
        return OFFICIAL_ROUTING_FOOTER
    if name and _is_sand_slot(name):
        return OFFICIAL_ROUTING_FOOTER

    # Future turn-bound concrete runtime / turn-resolved
    if mid and _is_concrete_model_id(mid) and kind in ("runtime", "turn-resolved"):
        label = esc(mid)
        return "模型：%s" % label

    if name and _is_concrete_model_id(name) and kind in ("runtime", "turn-resolved"):
        return "模型：%s" % esc(name)

    # official-routing / missing / anything else
    if src == OFFICIAL_ROUTING_SOURCE or not mid:
        return OFFICIAL_ROUTING_FOOTER
    return OFFICIAL_ROUTING_FOOTER


def model_footer_segment(state_or_resolved=None, *, escape_fn=None):
    return model_footer_display(state_or_resolved, escape_fn=escape_fn)


def format_footer_with_model(base_footer: str, state_or_resolved=None, *, escape_fn=None) -> str:
    """Status footer only — model belongs in element_id=model_footer, not prepended."""
    return (base_footer or "").strip()


def model_footer_element(
    state_or_resolved: Optional[dict],
    *,
    escape_fn: Optional[EscapeFn] = None,
) -> Optional[dict]:
    """Public cards currently omit model_footer (temporary hide). Returns None."""
    return None


if __name__ == "__main__":
    import pprint
    pprint.pp(resolve_model())
