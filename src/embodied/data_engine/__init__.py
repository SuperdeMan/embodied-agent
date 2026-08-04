"""Data Engine: recording -> dataset -> training -> eval loop (docs/architecture.md §4.6).

v0 ships the episode recorder/loader; LeRobot conversion is stubbed until M2
(docs/decisions.md D003, D012).
"""

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
    "EVENT_KINDS",
    "Episode",
    "EpisodeRecorder",
    "EpisodeWriter",
    "LEROBOT_FIELD_MAP",
    "SCHEMA_VERSION",
    "load_episode",
    "to_lerobot",
]
