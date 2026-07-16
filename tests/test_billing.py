"""Testes dos planos de billing."""

from __future__ import annotations

from app.billing.plans import PLANS, get_plan


def test_plans_exist():
    assert set(PLANS) == {"free", "pro", "enterprise"}


def test_get_plan_default_and_fallback():
    assert get_plan(None).key == "free"
    assert get_plan("inexistente").key == "free"
    assert get_plan("pro").price_brl == 249.0


def test_limits_increase_with_tier():
    assert PLANS["free"].rpm < PLANS["pro"].rpm < PLANS["enterprise"].rpm
    assert PLANS["free"].monthly_quota < PLANS["enterprise"].monthly_quota


def test_max_api_keys_defined_and_increases_with_tier():
    # Anti-abuso: todo plano define um teto de chaves ativas, crescente por tier.
    assert PLANS["free"].max_api_keys >= 1
    assert PLANS["free"].max_api_keys < PLANS["pro"].max_api_keys < PLANS["enterprise"].max_api_keys
