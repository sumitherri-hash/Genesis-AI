import os
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keep test state isolated from any real local database.
os.environ['GENESIS_DB'] = os.path.join(tempfile.gettempdir(), 'genesis-ai-test.db')
os.environ['GENESIS_ADMIN_KEY'] = 'test-only-key'

import app as genesis


def test_health_route():
    result = genesis.health()
    assert result['ok'] is True
    assert 'llm_configured' in result


def test_slugify_is_safe():
    assert genesis.slugify('Study Planner 2026!') == 'study-planner-2026'
    assert '/' not in genesis.slugify('../../hello')


def test_admin_key_required(monkeypatch):
    monkeypatch.setattr(genesis, 'ADMIN_KEY', 'secret')
    try:
        genesis.require_admin(None)
    except Exception as exc:
        assert getattr(exc, 'status_code', None) == 401
    else:
        raise AssertionError('Missing key should be rejected')
    genesis.require_admin('secret')
