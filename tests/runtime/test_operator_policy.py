"""Operator policy must be explicit and narrowly bound. 操作者策略必须明确且严格绑定。"""

import pytest
from pydantic import ValidationError

from embodied.contracts.operator import OperatorPolicy


@pytest.mark.parametrize("change", [
    {"trusted_proxy": "0.0.0.0"}, {"trusted_proxy": "8.8.8.8"}, {"trusted_proxy": "172.30.0.0/16"},
    {"origin": "http://operator.test"}, {"origin": "https://operator.test/path"},
    {"origin": "https://user@operator.test"}, {"subjects": []}, {"subjects": ["user\x7f@example.test"]},
])
def test_invalid_or_broad_policy_is_rejected(change):
    policy = {"trusted_proxy": "172.30.0.1", "origin": "https://operator.test", "subjects": ["user@example.test"]}
    with pytest.raises(ValidationError):
        OperatorPolicy.model_validate({**policy, **change})
