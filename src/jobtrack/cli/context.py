from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine

from jobtrack.config import Config, ensure_home, load_config
from jobtrack.core.db import init_db


@dataclass
class AppState:
    config: Config
    engine: Engine


_state: AppState | None = None


def get_state() -> AppState:
    global _state
    if _state is None:
        config = load_config()
        ensure_home(config)
        engine = init_db(config.db_path)
        _state = AppState(config=config, engine=engine)
    return _state
