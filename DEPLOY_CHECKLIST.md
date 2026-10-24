# GENESIS AI v0.8 deployment checklist

- [ ] Keep a backup of the current repo and Render environment.
- [ ] Replace root project files with v0.8 files; do not upload `.env`, database files, `workspace`, caches, or secrets.
- [ ] Confirm `app.py`, `requirements.txt`, `render.yaml` are at repository root.
- [ ] Configure valid LLM provider key and model on Render.
- [ ] Keep `GENESIS_ADMIN_KEY` private and unchanged unless intentionally rotated.
- [ ] Deploy and check `/api/health`.
- [ ] Request a short test video; wait for task status `completed`.
- [ ] Verify the MP4 download plays with both picture and narration.
- [ ] Check the factual content against a trusted textbook or subject expert.
- [ ] Remember: 24/7 availability is not guaranteed on free hosting.
