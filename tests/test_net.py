"""Testes da proteção anti-SSRF (validação de endpoint)."""

from __future__ import annotations

import pytest

from app.security.net import (
    _is_blocked_ip,
    host_in_allowlist,
    is_public_endpoint,
    validate_endpoint,
)


def test_blocked_ips():
    assert _is_blocked_ip("127.0.0.1")
    assert _is_blocked_ip("169.254.169.254")  # metadata cloud
    assert _is_blocked_ip("10.0.0.5")
    assert _is_blocked_ip("192.168.1.10")
    assert not _is_blocked_ip("8.8.8.8")


def test_is_public_endpoint_localhost_blocked():
    assert not is_public_endpoint("http://localhost:11434/v1")
    assert not is_public_endpoint("http://127.0.0.1:8000")
    assert not is_public_endpoint("http://169.254.169.254/latest/meta-data")


def test_validate_endpoint_allow_private_bypass():
    # allow_private=True não levanta, mesmo em rede privada
    validate_endpoint("http://localhost:11434/v1", allow_private=True)


def test_validate_endpoint_blocks_private():
    with pytest.raises(ValueError):
        validate_endpoint("http://192.168.0.1/v1", allow_private=False)


def test_host_allowlist_matches_host_and_port():
    allow = {"localhost:11434", "ollama"}
    # host:port no allowlist → precisa casar a porta
    assert host_in_allowlist("http://localhost:11434/v1", allow)
    assert not host_in_allowlist("http://localhost:8000/v1", allow)  # porta diferente
    # host sem porta no allowlist → casa qualquer porta
    assert host_in_allowlist("http://ollama:11434/api", allow)
    assert host_in_allowlist("http://ollama/api", allow)
    # host fora do allowlist
    assert not host_in_allowlist("http://192.168.0.1/v1", allow)
    # metadata da nuvem nunca passa (não está no allowlist)
    assert not host_in_allowlist("http://169.254.169.254/latest", allow)


def test_host_allowlist_empty_blocks_all():
    assert not host_in_allowlist("http://localhost:11434", set())
