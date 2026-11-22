"""Operator policy must be explicit and narrowly bound. 操作者策略必须明确且严格绑定。"""

import pytest
from pydantic import ValidationError

from embodied.contracts.operator import OperatorPolicy
from embodied.runtime.operator import OperatorAuthority


@pytest.mark.parametrize("change", [
    {"trusted_proxy": "0.0.0.0"}, {"trusted_proxy": "8.8.8.8"}, {"trusted_proxy": "172.30.0.0/16"},
    {"origin": "http://operator.test"}, {"origin": "https://operator.test/path"},
    {"origin": "https://user@operator.test"}, {"subjects": []}, {"subjects": ["user\x7f@example.test"]},
    {"scopes": []}, {"scopes": ["resume"]}, {"scopes": ["reset"]}, {"scopes": ["reconcile_effect", "halt"]},
    {"scopes": ["*"]}, {"scopes": None}, {"scopes": "reconcile_stop"},
])
def test_invalid_or_broad_policy_is_rejected(change):
    policy = {"trusted_proxy": "172.30.0.1", "origin": "https://operator.test", "subjects": ["user@example.test"]}
    with pytest.raises(ValidationError):
        OperatorPolicy.model_validate({**policy, **change})


def test_scopes_default_to_effect_only_and_bind_policy_digest():
    base = {"trusted_proxy": "172.30.0.1", "origin": "https://operator.test", "subjects": ["user@example.test"]}
    default_policy = OperatorPolicy.model_validate(base)
    explicit_effect = OperatorPolicy.model_validate({**base, "scopes": ["reconcile_effect"]})
    with_stop = OperatorPolicy.model_validate({**base, "scopes": ["reconcile_effect", "reconcile_stop"]})
    stop_only = OperatorPolicy.model_validate({**base, "scopes": ["reconcile_stop"]})
    assert default_policy.scopes == frozenset({"reconcile_effect"})
    assert OperatorAuthority(None, default_policy).policy_digest == OperatorAuthority(None, explicit_effect).policy_digest
    assert OperatorAuthority(None, with_stop).policy_digest != OperatorAuthority(None, default_policy).policy_digest
    assert OperatorAuthority(None, stop_only).policy_digest != OperatorAuthority(None, with_stop).policy_digest
