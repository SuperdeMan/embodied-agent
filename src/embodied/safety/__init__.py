"""Safety layer (M0): permission engine + robot scope catalog.

Export surface is deliberately minimal — the Safety Guardian process lands
here in M1 from a separate work stream.


安全层包含权限引擎与本体 scope，独立 Guardian 在同层实现，导出保持最小。
"""
from .permission import AuthContext, Decision, PermissionEngine, check_permission
from .scopes import (
    ACTUATION_PREFIXES,
    ALL_SCOPES,
    THIRD_PARTY_DENY_PREFIXES,
    TRUST_LEVEL_CAPS,
    deny_third_party,
    is_scope_covered,
)

__all__ = [
    "ACTUATION_PREFIXES",
    "ALL_SCOPES",
    "AuthContext",
    "Decision",
    "PermissionEngine",
    "THIRD_PARTY_DENY_PREFIXES",
    "TRUST_LEVEL_CAPS",
    "check_permission",
    "deny_third_party",
    "is_scope_covered",
]
