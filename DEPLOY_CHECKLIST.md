# GENESIS AI v0.5 deployment checklist

- [ ] Replace the existing GitHub repository contents with this project; `app.py`, `requirements.txt`, and `render.yaml` must be at repository root.
- [ ] Confirm Render has a private `GENESIS_ADMIN_KEY`; if resetting it, save a new strong secret and redeploy.
- [ ] Add a valid compatible `LLM_API_KEY` in Render Environment (never in GitHub).
- [ ] Wait for deployment status to show Live/Deployed.
- [ ] Open `/api/health`; verify `ok: true` and `llm_configured: true`.
- [ ] Open dashboard and enter the admin key once. Enable remember only on your own device/browser.
- [ ] Submit a small request and confirm the task transitions from queued/running to completed or failed.
- [ ] Review output before using it; AI output and generated code are not guaranteed correct.
- [ ] Android companion source is in `android/`; build it with Android Studio. This ZIP does not include a signed APK.
