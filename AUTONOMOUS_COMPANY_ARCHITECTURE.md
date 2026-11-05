# GENESIS — High-Level Autonomous Company Architecture

Status: implementation blueprint for the existing GENESIS repository. This document describes target behavior; it is not a claim that every capability is already deployed or verified.

## Product objective

GENESIS is an AI-operated digital product company. It should behave like a fast, organized, measurable company: understand a request, respond immediately, do useful work, produce a visible deliverable, learn from permitted feedback, and improve its products. It must identify itself as AI and must never pretend to be a human employee.

## 1. Fast response and visible output

Use a two-speed interaction pipeline:
1. Immediate response: acknowledge the request and show a short, useful first response as soon as possible.
2. Fast primary execution: one lead agent produces the first usable answer or draft without waiting for all departments.
3. Parallel specialist work: relevant agents work in the background only when they add value. Do not call all 12 agents for every simple request.
4. Progress visibility: show task ID, current stage, elapsed time, and the latest partial result. Save partial results so a refresh or reconnect does not lose them.
5. Deliverable-first UI: display the actual answer, preview, and files in the result area. Never leave the user with only a spinner or working message.
6. Recovery: retry transient provider errors with bounded backoff; preserve the task and explain blockers clearly. Do not retry indefinitely or hide failures.

Speed targets are engineering goals, not guarantees: first acknowledgement under 1 second; first useful result as soon as the provider responds; simple requests should use one model call where possible. Measure median and 95th-percentile time-to-first-result and time-to-final-result.

## 2. Company leadership and specialist agents

The existing 12-worker council is the specialist pool. Executive titles coordinate those workers; they are not extra independent model instances unless separately implemented.

- CEO / Strategy: mission, priorities, portfolio decisions, evidence-based recommendations.
- COO / Operations: task routing, deadlines, queues, handoffs, reliability.
- CTO / Engineering: architecture, implementation, technical quality.
- CFO / Economics: cost, pricing assumptions, unit economics; never invent actual financial results.
- CMO / Growth: positioning, customer research, ethical campaigns and acquisition experiments.
- Research & Innovation: problem discovery, public-source research, evidence grading.
- Customer Experience: customer questions, support drafts, feedback themes and service improvements.
- Product & Design: user journeys, requirements, prototypes and usability.
- Engineering: apps, websites, SaaS prototypes, tests, documentation and maintenance.
- Quality & Security: acceptance tests, privacy checks, threat analysis and release gates.

Every agent must have a defined scope, structured input/output, measurable completion criteria, task ownership, status, retry policy and audit trail. Agents should pass structured work to each other rather than merely produce disconnected chat messages.

## 3. Customer understanding and memory

Build a consent-based customer insight profile, not covert surveillance.

Permitted memory can include explicitly provided product preferences and choices; feedback and support themes; opted-in interaction history needed to continue a service; disclosed, minimized and aggregated product usage metrics; and customer-stated goals and unresolved requests.

For each memory item store its source, timestamp, purpose, consent/legal basis, confidence, retention period and deletion state. Distinguish facts from inferred preferences. Inferences must be tentative and correctable. Provide customers with clear notice, access, correction, export and deletion controls. Never infer or store sensitive traits, collect unnecessary personal data, scrape contacts, or use hidden tracking.

Use customer insight to improve relevance, usability and support—not to pressure, manipulate, exploit vulnerabilities, or fabricate testimonials. Marketing must be enthusiastic and persuasive while remaining truthful, respectful and transparent.

## 4. Product factory: apps, websites and SaaS

When GENESIS identifies a worthwhile company opportunity, it should define the customer problem and supporting evidence; research alternatives and competitors using permitted sources; state assumptions and validate willingness to pay; write a product brief, user stories and acceptance criteria; generate a working prototype with source files, setup instructions and a preview; run automated tests, security checks and quality review; prepare pricing hypotheses, onboarding, support material and a launch plan; and track real outcomes and recommend improvements.

The builder should create maintainable project files—not merely describe what could be built. Keep generated projects isolated from production. No automatic production deployment, public publishing, customer outreach, paid services, purchases or contracts without the founder's explicit approval.

## 5. Orchestration and operating loop

request -> classify -> prioritize -> assign lead -> execute -> specialist review -> quality gate -> deliver -> feedback -> learn

- Route simple questions directly to the lead model.
- Use specialist agents selectively for complex work.
- Run independent research, design, engineering and QA tasks in parallel when provider capacity permits.
- Use dependency-aware handoffs for tasks that must happen in sequence.
- Persist job state and partial outputs in durable storage.
- Recover queued/running jobs after process restarts.
- Maintain an audit trail of agent decisions, tool calls, approvals and deliverables.
- Keep a company backlog with impact, confidence, effort, risk and owner.
- Run scheduled company reviews only when hosting is awake and able to run them; do not claim 24/7 operation on a host that sleeps.

## 6. Approval and safety boundaries

GENESIS may autonomously research, reason, draft, code in an isolated workspace, test, summarize and recommend. Founder approval is required before spending money or starting paid services; publishing or deploying to production; sending messages or contacting customers/partners; creating accounts or connecting third-party services; signing agreements or making legal/financial commitments; and deleting data or making destructive changes.

Use least-privilege credentials, per-user authorization, rate limits, secret redaction and audit logs. Never ask users to paste API keys or passwords into chat. Do not claim work was completed unless a tool or test confirms it.

## 7. Performance scorecard

Track and display time to acknowledgement, first useful result and final result; task completion and failure rates; retries and provider-quota counts; output acceptance and revision rate; QA pass rate and escaped defects; cost per completed task and model calls per task; customer satisfaction and opt-in retention; validated problems, prototypes shipped, active users and verified revenue (only when actual records exist).

Do not optimize for enthusiasm alone. Optimize for useful, correct, fast and trusted outcomes.

## 8. Phased implementation order

**P0 — Trustworthy fast delivery:** immediate acknowledgement, lead-agent fast path, visible partial/final outputs, reconnect recovery, clear errors.

**P1 — Reliable company orchestration:** selective agent routing, durable queue, dependency-aware handoffs, structured outputs, task audit trail and restart recovery.

**P2 — Product factory:** isolated app/site/SaaS generation, preview, automated tests, QA and downloadable source deliverables.

**P3 — Customer intelligence:** consent-based preferences, feedback analysis, correctable memory, retention/deletion controls and customer-facing support.

**P4 — Continuous company operations:** scheduled reviews, opportunity pipeline, performance dashboards, monitoring, backups and reliable always-on hosting.

## Current implementation caveats

The repository already contains a 12-role council, company-learning loop, task UI, intelligence feed scanner, customer-account foundation and optional neural model definitions. Those pieces must be tested as an integrated system. The existing Render Free deployment may sleep and its local SQLite storage is ephemeral; it cannot currently guarantee durable memory or continuous 24/7 operation. Optional PyTorch architectures are not operational for training/inference unless the runtime is installed. This blueprint does not mark these blockers as solved.