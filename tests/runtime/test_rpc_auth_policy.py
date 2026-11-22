"""Pure RPC mTLS access policy and role-matrix contracts. 纯类型 RPC mTLS 访问策略与角色矩阵契约。"""

import inspect

import pytest
from pydantic import ValidationError

from embodied.contracts.rpc_auth import (
    CONTROL_METHOD_POLICY,
    SAFETY_METHOD_POLICY,
    RpcAccessPolicy,
    RpcPeerRule,
)
from embodied.runtime.rpc_auth import (
    RpcAuthorizer,
    RpcForbidden,
    RpcUnauthenticated,
    is_loopback_host,
    is_loopback_target,
    policy_digest_of,
)
from embodied.runtime.rpcgen import GEN_DIR, import_control, import_safety

CA_FP = "a" * 64
PEER_A = "1" * 64
PEER_B = "2" * 64


@pytest.mark.parametrize("bad_peer", [
    {"name": "op", "role": "operator", "cert_sha256": PEER_A},
    {"name": "ag", "role": "agent", "cert_sha256": PEER_A, "scopes": ["read_only"]},
    {"name": "gd", "role": "guardian", "cert_sha256": PEER_A, "scopes": ["halt"]},
    {"name": "bad", "role": "admin", "cert_sha256": PEER_A},
    {"name": "op", "role": "operator", "cert_sha256": PEER_A, "scopes": ["resume"]},
    {"name": "op", "role": "operator", "cert_sha256": "not-hex", "scopes": ["read_only"]},
    {"name": "op", "role": "operator", "cert_sha256": PEER_A, "scopes": ["read_only"], "spiffe_id": "https://wrong"},
    {"name": "op", "role": "operator", "cert_sha256": PEER_A, "scopes": ["read_only"], "extra": 1},
])
def test_invalid_peer_rule_is_rejected(bad_peer):
    with pytest.raises(ValidationError):
        RpcPeerRule.model_validate(bad_peer)


def test_policy_rejects_duplicates_and_bad_ca_fingerprint():
    peer1 = {"name": "a", "role": "agent", "cert_sha256": PEER_A}
    peer2 = {"name": "b", "role": "guardian", "cert_sha256": PEER_A}
    peer3 = {"name": "a", "role": "guardian", "cert_sha256": PEER_B}
    with pytest.raises(ValidationError):
        RpcAccessPolicy.model_validate({"ca_cert_sha256": CA_FP, "peers": [peer1, peer2]})
    with pytest.raises(ValidationError):
        RpcAccessPolicy.model_validate({"ca_cert_sha256": CA_FP, "peers": [peer1, peer3]})
    with pytest.raises(ValidationError):
        RpcAccessPolicy.model_validate({"ca_cert_sha256": "short", "peers": [peer1]})
    with pytest.raises(ValidationError):
        RpcAccessPolicy.model_validate({"ca_cert_sha256": CA_FP, "peers": []})


def test_loopback_host_and_target_classification():
    for host in ("127.0.0.1", "::1", "[::1]", "localhost", "LOCALHOST"):
        assert is_loopback_host(host)
    for host in ("0.0.0.0", "::", "10.0.0.1", "172.30.0.2", "192.168.1.5", "robot.local", ""):
        assert not is_loopback_host(host)
    for target in ("127.0.0.1:8391", "localhost:8391", "[::1]:8391"):
        assert is_loopback_target(target)
    for target in ("0.0.0.0:8391", "10.0.0.5:8391", "control.tailnet.ts.net:8391", ""):
        assert not is_loopback_target(target)


def test_authorizer_fails_closed_on_missing_ssl_or_unknown_method():
    policy = RpcAccessPolicy.model_validate({
        "ca_cert_sha256": CA_FP,
        "peers": [
            {"name": "agent", "role": "agent", "cert_sha256": PEER_A},
            {"name": "op", "role": "operator", "cert_sha256": PEER_B, "scopes": ["read_only"]},
        ],
    })
    authz = RpcAuthorizer(policy, "control")
    assert len(policy_digest_of(policy)) == 64
    with pytest.raises(RpcUnauthenticated, match="mtls_transport_required"):
        authz.authenticate({})
    with pytest.raises(RpcUnauthenticated, match="client_certificate_required"):
        authz.authenticate({"transport_security_type": [b"ssl"], "x509_pem_cert": []})
    with pytest.raises(RpcUnauthenticated, match="client_certificate_required"):
        authz.authenticate({"transport_security_type": [b"ssl"], "x509_pem_cert": [b"a", b"b"]})
    with pytest.raises(RpcUnauthenticated, match="invalid_client_certificate"):
        authz.authenticate({"transport_security_type": [b"ssl"], "x509_pem_cert": [b"not-a-cert"]})
    # 未知 RPC 方法在任何角色下都必须默认拒绝。 / Unknown RPC methods must fail closed for every role.
    authz.authenticate = lambda ctx: type("P", (), {"role": "operator", "scopes": ("reset_guard",)})()
    with pytest.raises(RpcForbidden, match="rpc_method_not_allowed"):
        authz.authorize({}, "UnregisteredMethod")


def test_method_policy_covers_every_proto_rpc_method():
    if not (GEN_DIR / "embodiedrpc").is_dir():
        pytest.skip("proto stubs not generated")
    _, control_grpc = import_control()
    _, safety_grpc = import_safety()
    control_methods = {
        name for name, member in inspect.getmembers(control_grpc.ControlServiceServicer, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    safety_methods = {
        name for name, member in inspect.getmembers(safety_grpc.SafetyServiceServicer, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert set(CONTROL_METHOD_POLICY) == control_methods
    assert set(SAFETY_METHOD_POLICY) == safety_methods
