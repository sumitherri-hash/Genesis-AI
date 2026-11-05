"""Ethical go-to-market planning: drafts and experiments only; no auto-posting or outreach."""
from fastapi import HTTPException, Header, Request
from pydantic import BaseModel, Field
import app as core

class LaunchBrief(BaseModel):
    product_name: str = Field(min_length=2, max_length=120)
    product_description: str = Field(min_length=10, max_length=2500)
    audience_hint: str = Field(default='', max_length=500)
    value_proposition: str = Field(default='', max_length=700)

def install(app):
    @app.post('/api/growth/launch-pack')
    def launch_pack(data: LaunchBrief, request: Request):
        owner = core._request_user_or_admin(request)
        if not owner:
            raise HTTPException(403, 'Sign in to create a launch pack.')
        prompt = f'''Prepare a practical, ethical launch pack for this product.
Product: {data.product_name}
Description: {data.product_description}
Audience clue supplied by owner: {data.audience_hint or 'Not specified'}
Value proposition: {data.value_proposition or 'Derive a testable hypothesis; label it.'}
Return JSON with: positioning, likely_customer_segments (up to 3, based on product need not sensitive traits), user_pain_hypotheses, validation_questions (5), landing_page_copy, x_post_drafts (3), google_search_ads_drafts (2, clearly drafts), short_video_concepts (3), organic_distribution_channels (5), pricing_hypotheses (clearly unvalidated), success_metrics, risks_and_assumptions, 7_day_experiment_plan.
Rules: Do not promise revenue, invent customers or evidence, target minors with commercial persuasion, infer sensitive traits, scrape personal data, or recommend unsolicited bulk messages. Prefer public contextual channels and opt-in signups. Do not claim posts or ads were published or users contacted. Clearly distinguish hypotheses from verified evidence. No discriminatory or manipulative targeting.'''
        result = core.parse_json(core.llm([
            {'role':'system','content':'You are GENESIS Growth Research. Be evidence-led, privacy-preserving, and honest. Return valid JSON only.'},
            {'role':'user','content':prompt}
        ], .2))
        return {'status':'draft_only', 'requires_human_review':True, 'published':False,
                'customers_contacted':False, 'pack':result}
