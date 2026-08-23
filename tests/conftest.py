import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_scorer_state():
    """Reset scorer's module-level Redis client between tests."""
    from brain import scorer
    old = scorer.get_redis_client()
    yield
    scorer.set_redis_client(old)


@pytest.fixture
def fake_redis():
    from brain import scorer
    import fakeredis
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    scorer.set_redis_client(r)   # compute_score circuit checks use this
    return r
