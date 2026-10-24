# Deployment checklist

- [ ] Create a private or public GitHub repository; never commit `.env` or API keys.
- [ ] Upload all files and keep `.gitignore` enabled.
- [ ] Connect repository to Render using `render.yaml`.
- [ ] Set `LLM_API_KEY`; set `LLM_BASE_URL`/`LLM_MODEL` for your provider if needed.
- [ ] Set a long, unique `GENESIS_ADMIN_KEY`; store it privately.
- [ ] Deploy and open `/api/health` to verify the service responds.
- [ ] Open dashboard; enter admin key; run one cycle manually.
- [ ] Review findings and generated files before enabling autopilot.
- [ ] Confirm hosting persistence/uptime expectations before relying on continuous operation.
- [ ] Add email/social integrations only later, using official APIs and explicit approvals.
