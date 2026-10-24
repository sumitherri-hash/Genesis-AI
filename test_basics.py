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


def test_public_status_hides_internal_workforce():
    result = genesis.status()
    assert result['ok'] is True
    assert 'agents' not in result
    assert 'logs' not in result


def test_privacy_policy_is_minimal_data():
    result = genesis.privacy()
    assert result['privacy_mode'] == 'minimal-data'
    assert result['personal_device_access'] is False
    assert result['permissions_requested_by_backend'] == []
    assert any('does not access phone contacts' in note for note in result['notes'])


def test_expert_review_skips_without_provider_key(monkeypatch):
    monkeypatch.setattr(genesis, 'LLM_API_KEY', '')
    result = genesis.expert_review('prototype', {'summary': 'example'})
    assert result['status'] == 'skipped'


def test_full_task_list_requires_admin(monkeypatch):
    monkeypatch.setattr(genesis, 'ADMIN_KEY', 'secret')
    try:
        genesis.tasks(None)
    except Exception as exc:
        assert getattr(exc, 'status_code', None) == 401
    else:
        raise AssertionError('Full task list must require admin authorization')


def test_internal_company_requires_admin(monkeypatch):
    monkeypatch.setattr(genesis, 'ADMIN_KEY', 'secret')
    try:
        genesis.internal_company(None)
    except Exception as exc:
        assert getattr(exc, 'status_code', None) == 401
    else:
        raise AssertionError('Internal company structure must require admin authorization')
    result = genesis.internal_company('secret')
    assert any(x['id'] == 'sales' for x in result['departments'])

def test_video_renderer_is_defined():
    assert callable(genesis.render_polished_mp4)
