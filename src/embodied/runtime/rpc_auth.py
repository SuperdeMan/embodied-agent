"""mTLS certificate pinning, role authorization, and audited RPC maintenance helpers.

mTLS 证书指纹固定、角色方法授权与 RPC 维护审计工具。
"""

from __future__ import annotations

import hashlib
import ipaddress
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from embodied.contracts.effect import effect_digest
from embodied.contracts.rpc_auth import (
    CONTROL_METHOD_POLICY,
    SAFETY_METHOD_POLICY,
    RpcAccessPolicy,
    RpcPeerRule,
    RpcTlsPaths,
)


class RpcUnauthenticated(RuntimeError):
    """Missing, unverified, or unknown mTLS peer identity. 缺失、未验证或未知的 mTLS 对端身份。"""


class RpcForbidden(RuntimeError):
    """Authenticated peer lacks role or scope for the requested RPC. 已认证对端缺少所请求 RPC 的角色或作用域。"""


@dataclass(frozen=True)
class RpcPrincipal:
    """Verified mTLS caller identity bound to leaf cert and policy digests.

    绑定叶证书与策略摘要的已认证 mTLS 调用方身份。
    """

    name: str
    role: str
    cert_sha256: str
    subject_hash: str
    policy_digest: str
    scopes: tuple[str, ...]
    spiffe_id: str | None = None
    issuer: str = "grpc_mtls"


def cert_fingerprint_sha256(pem: bytes | str) -> str:
    """Compute lowercase SHA-256 of the first X.509 certificate's DER encoding.

    计算首个 X.509 证书 DER 编码的小写 SHA-256 指纹。
    """
    text = pem.decode("ascii") if isinstance(pem, (bytes, bytearray)) else str(pem)
    begin = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    start = text.find(begin)
    finish = text.find(end, start) if start >= 0 else -1
    if start < 0 or finish < 0:
        raise ValueError("invalid PEM certificate")
    leaf = text[start : finish + len(end)]
    der = ssl.PEM_cert_to_DER_cert(leaf)
    return hashlib.sha256(der).hexdigest()


def is_loopback_host(host: str) -> bool:
    """Return True only for exact loopback hostnames or loopback IP addresses.

    仅对精确回环主机名或回环 IP 地址返回 True。
    """
    raw = (host or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if raw.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def is_loopback_target(target: str) -> bool:
    """Check whether a gRPC host:port target refers strictly to loopback.

    检查 gRPC host:port 目标是否严格指向本机回环。
    """
    raw = (target or "").strip()
    if "://" in raw:
        parsed = urlsplit(raw)
        return is_loopback_host(parsed.hostname or "")
    parsed = urlsplit(f"//{raw}")
    return is_loopback_host(parsed.hostname or "")


def policy_digest_of(policy: RpcAccessPolicy) -> str:
    """Canonical SHA-256 digest of an RpcAccessPolicy. 计算 RpcAccessPolicy 的规范 SHA-256 摘要。"""
    body = {
        "api_version": policy.api_version,
        "ca_cert_sha256": policy.ca_cert_sha256,
        "peers": sorted(
            [
                {
                    "name": peer.name, "role": peer.role, "cert_sha256": peer.cert_sha256,
                    "spiffe_id": peer.spiffe_id, "scopes": sorted(peer.scopes),
                }
                for peer in policy.peers
            ],
            key=lambda item: item["cert_sha256"],
        ),
    }
    return effect_digest(body)


def load_rpc_policy(path: str | Path) -> RpcAccessPolicy:
    return RpcAccessPolicy.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_server_credentials(tls: RpcTlsPaths, policy: RpcAccessPolicy):
    """Build BoringSSL mTLS server credentials with pinned CA verification.

    构建强制校验根 CA 指纹的 BoringSSL mTLS 服务端凭据。
    """
    import grpc

    ca_bytes = Path(tls.ca_cert).read_bytes()
    if cert_fingerprint_sha256(ca_bytes) != policy.ca_cert_sha256:
        raise ValueError("rpc_ca_cert_fingerprint_mismatch")
    cert_bytes = Path(tls.cert).read_bytes()
    key_bytes = Path(tls.key).read_bytes()
    # 校验服务端证书结构完整且私钥非空。 / Validate server certificate structure and non-empty private key.
    cert_fingerprint_sha256(cert_bytes)
    if not key_bytes.strip():
        raise ValueError("empty server private key")
    return grpc.ssl_server_credentials(
        [(key_bytes, cert_bytes)],
        root_certificates=ca_bytes,
        require_client_auth=True,
    )


def load_client_credentials(tls: RpcTlsPaths, *, expected_ca_sha256: str | None = None):
    """Build BoringSSL mTLS client credentials from explicit PEM paths.

    按显式 PEM 路径构建 BoringSSL mTLS 客户端凭据。
    """
    import grpc

    ca_bytes = Path(tls.ca_cert).read_bytes()
    ca_fp = cert_fingerprint_sha256(ca_bytes)
    if expected_ca_sha256 is not None and ca_fp != expected_ca_sha256:
        raise ValueError("rpc_ca_cert_fingerprint_mismatch")
    cert_bytes = Path(tls.cert).read_bytes()
    key_bytes = Path(tls.key).read_bytes()
    cert_fingerprint_sha256(cert_bytes)
    if not key_bytes.strip():
        raise ValueError("empty client private key")
    return grpc.ssl_channel_credentials(
        root_certificates=ca_bytes,
        private_key=key_bytes,
        certificate_chain=cert_bytes,
    )


def build_client_channel(target: str, tls: RpcTlsPaths | None = None, *, server_name: str = ""):
    """Open an authenticated mTLS channel, refusing plaintext on non-loopback targets.

    打开 mTLS 通道；非回环目标缺少 TLS 凭据时直接拒绝。
    """
    import grpc

    if tls is None:
        if not is_loopback_target(target):
            raise ValueError("non_loopback_rpc_requires_mtls")
        return grpc.aio.insecure_channel(target)
    creds = load_client_credentials(tls)
    options = [("grpc.ssl_target_name_override", server_name)] if server_name else None
    return grpc.aio.secure_channel(target, creds, options=options)


class RpcAuthorizer:
    """Authenticate BoringSSL mTLS peers and enforce the hardcoded role/method matrix.

    校验 BoringSSL mTLS 对端并强制执行硬编码的角色方法权限矩阵。
    """

    def __init__(self, policy: RpcAccessPolicy, service: Literal["control", "safety"]):
        self.policy = policy
        self.service = service
        self.policy_digest = policy_digest_of(policy)
        self._peers: dict[str, RpcPeerRule] = {peer.cert_sha256: peer for peer in policy.peers}
        self._methods = CONTROL_METHOD_POLICY if service == "control" else SAFETY_METHOD_POLICY

    def authenticate(self, auth_context: dict[str, list[bytes]]) -> RpcPrincipal:
        if not auth_context or auth_context.get("transport_security_type") != [b"ssl"]:
            raise RpcUnauthenticated("mtls_transport_required")
        certs = auth_context.get("x509_pem_cert") or []
        if len(certs) != 1:
            raise RpcUnauthenticated("client_certificate_required")
        try:
            fingerprint = cert_fingerprint_sha256(certs[0])
        except ValueError:
            raise RpcUnauthenticated("invalid_client_certificate") from None
        rule = self._peers.get(fingerprint)
        if rule is None:
            raise RpcUnauthenticated("rpc_peer_not_allowed")
        if rule.spiffe_id is not None:
            expected = rule.spiffe_id.encode("ascii")
            uris = set(auth_context.get("peer_uri") or ()) | set(auth_context.get("x509_subject_alternative_name") or ())
            if expected not in uris:
                raise RpcUnauthenticated("rpc_spiffe_id_mismatch")
        subject_hash = hashlib.sha256(f"{rule.role}:{rule.name}:{rule.cert_sha256}".encode("ascii")).hexdigest()
        return RpcPrincipal(
            name=rule.name, role=rule.role, cert_sha256=rule.cert_sha256,
            subject_hash=subject_hash, policy_digest=self.policy_digest,
            scopes=tuple(sorted(rule.scopes)), spiffe_id=rule.spiffe_id,
        )

    def authorize(self, auth_context: dict[str, list[bytes]], method: str) -> RpcPrincipal:
        principal = self.authenticate(auth_context)
        requirement = self._methods.get(method)
        # 未在矩阵登记的方法一律默认拒绝。 / Any method absent from the matrix fails closed.
        if requirement is None:
            raise RpcForbidden("rpc_method_not_allowed")
        allowed_roles, required_scope = requirement
        if principal.role not in allowed_roles:
            raise RpcForbidden("rpc_role_not_allowed")
        if principal.role == "operator" and required_scope is not None and required_scope not in principal.scopes:
            raise RpcForbidden("rpc_scope_not_allowed")
        return principal


def record_rpc_audit(registry, record, kind: str, payload: dict) -> None:
    """Append bounded mTLS operator audit event into existing schema v2 operation_events.

    向既有 schema v2 的 operation_events 追加有界 mTLS 操作者审计事件。
    """
    ledger = registry.ledger
    with ledger._db:
        ledger._db.execute("BEGIN IMMEDIATE")
        current = ledger.get(record.operation_id)
        if (current is None or current.attempt_id != record.attempt_id
                or current.request_digest != record.request_digest):
            raise RuntimeError("rpc_audit_target_changed")
        size = ledger._db.execute(
            "SELECT COALESCE(SUM(length(payload_json)),0) FROM operation_events "
            "WHERE operation_id=? AND (event_type LIKE 'operator_%' OR event_type LIKE 'rpc_operator_%')",
            (record.operation_id,),
        ).fetchone()[0]
        if size > 262144:
            raise RuntimeError("rpc_audit_capacity")
        ledger._event(record.operation_id, record.attempt_id, kind, payload)
