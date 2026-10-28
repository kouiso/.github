import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))  # scripts/


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """書き込みの 1 秒と上限の返事の待ち（60/120/240 秒）を本当には待たず、待った秒数だけ記録する。"""
    from scrum_keeper import job

    slept = []
    monkeypatch.setattr(job, "sleep_fn", slept.append)
    return slept
