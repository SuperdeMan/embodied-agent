"""Data Engine: recording -> dataset -> training -> eval loop (docs/architecture.md §4.6).

M1 shipped the episode recorder/loader; M2 adds scripted-expert collection and the
LeRobot conversion path (docs/decisions.md D003, D012, D014).
"""

from embodied.data_engine.collect import COLLECTOR_ID, CollectReport, collect_episodes
from embodied.data_engine.lerobot_convert import ConvertReport, convert_episodes
from embodied.data_engine.recorder import (
    EVENT_KINDS,
    LEROBOT_FIELD_MAP,
    SCHEMA_VERSION,
    Episode,
    EpisodeRecorder,
    EpisodeWriter,
    load_episode,
    to_lerobot,
)

__all__ = [
    "COLLECTOR_ID",
    "CollectReport",
    "ConvertReport",
    "convert_episodes",
    "EVENT_KINDS",
    "Episode",
    "EpisodeRecorder",
    "EpisodeWriter",
    "LEROBOT_FIELD_MAP",
    "SCHEMA_VERSION",
    "collect_episodes",
    "load_episode",
    "to_lerobot",
]
