"""Data Engine: recording -> dataset -> training -> eval loop (docs/architecture.md §4.6).

M1 shipped the episode recorder/loader; M2 adds scripted-expert collection and the
LeRobot conversion path (docs/decisions.md D003, D012, D014).


数据引擎连接录制、转换、训练和评测；M1 提供记录器，M2 增加专家采集与 LeRobot 转换。
"""

from embodied.data_engine.collect import COLLECTOR_ID, CollectReport, collect_episodes
from embodied.data_engine.lerobot_convert import ConvertReport, convert_episodes
from embodied.data_engine.recorder import (
    DEFAULT_CHECKPOINT_EVERY,
    EVENT_KINDS,
    LEROBOT_FIELD_MAP,
    SCHEMA_VERSION,
    Episode,
    EpisodeRecorder,
    EpisodeWriter,
    audit_episode,
    load_episode,
    recover_episode,
    to_lerobot,
)

__all__ = [
    "COLLECTOR_ID",
    "CollectReport",
    "ConvertReport",
    "DEFAULT_CHECKPOINT_EVERY",
    "convert_episodes",
    "EVENT_KINDS",
    "Episode",
    "EpisodeRecorder",
    "EpisodeWriter",
    "LEROBOT_FIELD_MAP",
    "SCHEMA_VERSION",
    "audit_episode",
    "collect_episodes",
    "load_episode",
    "recover_episode",
    "to_lerobot",
]
