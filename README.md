# GENESIS AI v0.8 — Company workflow + narrated MP4 prototype

GENESIS is a customer-facing digital-product prototype with a private virtual-company workflow and an optional narrated 720p MP4 renderer for study lessons.

## New in v0.8
- Generates a narrated H.264/AAC 1280×720 MP4 from AI-created lesson scenes using Edge TTS and the bundled FFmpeg binary.
- Creates a matching interactive lesson, recap, and quiz.
- Runs technical validation that checks for video and audio streams; this is not proof of factual accuracy or educational effectiveness.
- Adds a private internal company department map (research, product, engineering, creative, QA, sales, business model, security, operations).
- Customer-facing UI stays separate from internal departments and admin routes.
- MP4 download is served from a path-confined workspace endpoint.

## Requirements and limits
- A valid compatible AI provider API key is required for generation. Edge TTS needs outbound network access. If narration or rendering fails, the task reports a failure rather than falsely claiming an MP4 was created.
- Rendering many scenes may take minutes and consume CPU; it is not instantaneous.
- The department heads and specialist roles are AI role prompts, not actual employees, licensed doctors, or professional engineers. High-stakes work needs qualified human review.
- GENESIS can investigate market signals and create business hypotheses, but it cannot guarantee profit or autonomously make money without a real product, customers, payment and legal/tax setup, and human approval for financial commitments.
- Autopilot runs only while the service is awake. Render free hosting does not guarantee 24/7 availability. Reliable operation needs an always-on worker, durable storage/queue, monitoring, and budget.
- No access to personal phone data is required. The backend receives the submitted request and standard web metadata only.
- The public task limiter is in-memory and intended only as a prototype safeguard. Production requires durable queues, shared rate limiting, authentication, cost controls, and monitoring.

## Deploy on Render
1. Back up your current private repository.
2. Replace its root project files with the contents of this ZIP (keep `app.py`, `requirements.txt`, and `render.yaml` at repository root).
3. Commit and let your existing Render service deploy the commit.
4. In Render environment variables, configure a valid `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, and a strong `GENESIS_ADMIN_KEY`. Never commit secrets to GitHub.
5. Test `/api/health`, then request a short study video. Check task status and download the MP4 when completed.
6. If Render's native Python build fails to install or run the bundled video dependencies, use the included Dockerfile by switching the service to Docker deployment; don't create a second service until the current one is backed up.

## Local run
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Video quality scope
The renderer creates designed title/scene cards with spoken narration, not generative cinematic footage. It is a first working MP4 pipeline. It does not automatically source copyrighted YouTube videos, guarantee a voice/music license, or prove a lesson's factual correctness. More advanced visual generation, transitions, music licensing, and independent subject-matter review are future integrations.
