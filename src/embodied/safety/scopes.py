# Ported from car-agent security/scopes.py @ f0b08f8, changes: scope catalog rewritten for the robot domain (arm/gripper/camera/mic/speaker/memory/net); single control-scope parent generalized to ACTUATION_PREFIXES; coverage/deny helpers unchanged.
"""Permission Scope 全集、trust_level 硬上限表、父子覆盖判定。

命名规则：<resource>.<action>[.<sub>]
父 scope 覆盖子：拥有 arm 即覆盖 arm.move。
"""

# ─── Scope 全集 ───
ARM_MOVE = "arm.move"
ARM_HOME = "arm.home"
GRIPPER_OPEN = "gripper.open"
GRIPPER_CLOSE = "gripper.close"
CAMERA_READ = "camera.read"
MIC_LISTEN = "mic.listen"
SPEAKER_SAY = "speaker.say"
MEMORY_READ = "memory.read"
MEMORY_WRITE = "memory.write"
NET_FETCH = "net.fetch"

ALL_SCOPES: set[str] = {
    ARM_MOVE, ARM_HOME, GRIPPER_OPEN, GRIPPER_CLOSE,
    CAMERA_READ, MIC_LISTEN, SPEAKER_SAY,
    MEMORY_READ, MEMORY_WRITE, NET_FETCH,
}

# 物理执行器（actuation）scope 前缀（origin 单一控制父 scope 的对应位）。
# third_party / tool 硬禁令据此判定。
ACTUATION_PREFIXES: tuple[str, ...] = ("arm", "gripper")

# ─── trust_level 硬上限 ───
# system: 全部；first_party: 除持续拾音外全部；third_party: 禁执行器/原始传感器/记忆写。
# mic.listen（持续拾音）是旁观者隐私最尖锐的 scope，first_party 也不给——
# 沿 origin 把「持续采集」类 scope 收在 system 的先例。
TRUST_LEVEL_CAPS: dict[str, set[str]] = {
    "system": set(ALL_SCOPES),
    "first_party": {
        ARM_MOVE, ARM_HOME, GRIPPER_OPEN, GRIPPER_CLOSE,
        CAMERA_READ, SPEAKER_SAY, MEMORY_READ, MEMORY_WRITE, NET_FETCH,
    },
    "third_party": {
        SPEAKER_SAY, MEMORY_READ, NET_FETCH,
    },
}

# third_party 强制禁止的 scope 前缀（即使 token/user_grants 授予了也不生效）
THIRD_PARTY_DENY_PREFIXES: set[str] = {
    "arm", "gripper", CAMERA_READ, MIC_LISTEN,
}


def is_scope_covered(required: str, effective: set[str]) -> bool:
    """判断 required scope 是否被 effective 集合覆盖（支持父子覆盖）。

    拥有 arm 覆盖 arm.move；
    拥有 gripper.open 不覆盖 gripper.close。
    """
    parts = required.split(".")
    return any(".".join(parts[:i]) in effective for i in range(len(parts), 0, -1))


def deny_third_party(scopes: set[str]) -> set[str]:
    """从 scope 集合中剔除 third_party 禁止的 scope。"""
    return {s for s in scopes if not any(s.startswith(p) for p in THIRD_PARTY_DENY_PREFIXES)}
