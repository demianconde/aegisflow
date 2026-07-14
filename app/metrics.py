"""Métricas em memória expostas em /metrics (formato Prometheus)."""

from __future__ import annotations

_counters: dict[str, float] = {
    "aegis_requests_total": 0,
    "aegis_cache_hits_total": 0,
    "aegis_errors_total": 0,
    "aegis_prompt_tokens_total": 0,
    "aegis_completion_tokens_total": 0,
    "aegis_cost_saved_usd_total": 0.0,
}


def inc(name: str, value: float = 1) -> None:
    _counters[name] = _counters.get(name, 0) + value


def render_prometheus() -> str:
    lines = []
    for name, value in _counters.items():
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"
