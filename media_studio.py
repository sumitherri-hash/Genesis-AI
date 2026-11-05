"""Optional image/video generation adapter.

Uses a server-side Replicate API token and administrator-configured model slugs.
Generation is disabled by default and may incur provider charges. Never expose tokens
or trigger generation from background/autopilot jobs.
"""
import os, uuid, sqlite3, json, time
from datetime import datetime, timezone
from fastapi import HTTPException, Header, Request
from pydantic import BaseModel, Field
import requests
import app as core

def _now():
    return datetime.now(timezone.utc).isoformat()

def _db():
    with core.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS media_jobs(
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
            prompt TEXT NOT NULL, prediction_id TEXT NOT NULL,
            status TEXT NOT NULL, output TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")

class MediaRequest(BaseModel):
    kind: str
    prompt: str = Field(min_length=5, max_length=3000)
    style: str = Field(default='', max_length=500)
    intended_use: str = Field(default='', max_length=500)
    confirm_possible_costs: bool = False

def _enabled():
    return (
        os.getenv('GENESIS_MEDIA_ENABLED', 'false').lower() == 'true'
        and os.getenv('GENESIS_MEDIA_BILLING_APPROVED', 'false').lower() == 'true'
        and bool(os.getenv('REPLICATE_API_TOKEN', '').strip())
    )

def install(app):
    _db()

    @app.get('/api/media/capabilities')
    def media_capabilities():
        ready = _enabled()
        return {
            'image_generation': ready and bool(os.getenv('REPLICATE_IMAGE_MODEL', '').strip()),
            'video_generation': ready and bool(os.getenv('REPLICATE_VIDEO_MODEL', '').strip()),
            'provider': 'Replicate' if ready else None,
            'status': 'configured' if ready else 'not_configured',
            'requires_explicit_user_confirmation': True,
            'background_generation': False,
            'note': 'Provider models and account permissions must be configured. Some models may incur charges; no generation runs automatically.'
        }

    @app.post('/api/media/generate')
    def generate(data: MediaRequest, request: Request):
        owner = core._request_user_or_admin(request)
        if not owner:
            raise HTTPException(403, 'Sign in with a customer account to create media.')
        kind = data.kind.strip().lower()
        if kind not in ('image', 'video'):
            raise HTTPException(400, 'Choose image or video.')
        if not _enabled():
            raise HTTPException(503, 'Media generation is not configured. A provider token, approved model, and administrator billing approval are required.')
        if not data.confirm_possible_costs:
            raise HTTPException(400, 'Confirm that the selected provider may charge for generation before continuing.')
        model = os.getenv('REPLICATE_IMAGE_MODEL' if kind == 'image' else 'REPLICATE_VIDEO_MODEL', '').strip()
        if not model or '/' not in model:
            raise HTTPException(503, f'No {kind} model is configured.')
        prompt = data.prompt.strip()
        if data.style.strip():
            prompt += '\\nVisual style preference: ' + data.style.strip()
        if data.intended_use.strip():
            prompt += '\\nIntended use: ' + data.intended_use.strip()
        token = os.getenv('REPLICATE_API_TOKEN', '').strip()
        try:
            response = requests.post(
                'https://api.replicate.com/v1/models/' + model + '/predictions',
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
                json={'input': {'prompt': prompt}},
                timeout=25
            )
            if response.status_code not in (200, 201):
                raise HTTPException(502, 'Media provider rejected the request (HTTP ' + str(response.status_code) + ').')
            payload = response.json()
            prediction_id = str(payload.get('id', ''))
            if not prediction_id:
                raise HTTPException(502, 'Media provider did not return a job ID.')
        except requests.RequestException:
            raise HTTPException(502, 'Could not reach the media provider. No result was delivered.')
        job_id = str(uuid.uuid4())
        stamp = _now()
        with core.db() as c:
            c.execute('INSERT INTO media_jobs(id,owner_id,kind,prompt,prediction_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                      (job_id, owner, kind, prompt[:3500], prediction_id, str(payload.get('status', 'starting')), stamp, stamp))
        return {'job_id': job_id, 'kind': kind, 'status': payload.get('status', 'starting'), 'message': 'Generation submitted to the configured provider.'}

    @app.get('/api/media/jobs/{job_id}')
    def media_job(job_id: str, request: Request):
        owner = core._request_user_or_admin(request)
        with core.db() as c:
            row = c.execute('SELECT * FROM media_jobs WHERE id=?', (job_id,)).fetchone()
        if not row or not owner or row['owner_id'] != owner:
            raise HTTPException(404, 'Media job not found.')
        token = os.getenv('REPLICATE_API_TOKEN', '').strip()
        if not token:
            raise HTTPException(503, 'Media provider is no longer configured.')
        try:
            response = requests.get('https://api.replicate.com/v1/predictions/' + row['prediction_id'],
                headers={'Authorization': 'Bearer ' + token}, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException:
            raise HTTPException(502, 'Could not retrieve the provider job status.')
        status = str(payload.get('status', 'unknown'))
        output = payload.get('output')
        with core.db() as c:
            c.execute('UPDATE media_jobs SET status=?,output=?,updated_at=? WHERE id=?',
                      (status, json.dumps(output, ensure_ascii=False)[:12000] if output is not None else '', _now(), job_id))
        return {'job_id': job_id, 'kind': row['kind'], 'status': status,
                'output': output if status == 'succeeded' else None,
                'error': payload.get('error') if status == 'failed' else None,
                'note': 'Provider output is returned as supplied. Review it before public or commercial use.'}
