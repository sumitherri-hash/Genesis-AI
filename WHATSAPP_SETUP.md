# GENESIS WhatsApp bridge

This adds an official WhatsApp Business Platform Cloud API webhook to the existing FastAPI service.

## What it does

- Receives individual text messages sent to the separate GENESIS business number.
- Replies using the language-model provider already configured for GENESIS.
- Accepts requests only from numbers listed in `WHATSAPP_ALLOWED_NUMBERS`.
- Validates Meta's `X-Hub-Signature-256` webhook signature.
- Deduplicates webhook message IDs and does not persist message bodies.
- Does not read the user's personal WhatsApp inbox, contacts, photos, microphone, location, or other phone data.
- Does not send messages to other people or take external actions.

Your existing personal WhatsApp account can be used to start a chat with the separate GENESIS business number. The personal account is the chat participant; it is not silently connected or read by this service.

## Required Render environment variables

Add these to the existing `genesis-ai` Render web service. Do not paste secrets into chat or commit them to GitHub.

- `WHATSAPP_VERIFY_TOKEN`: a long random string you choose; use the same value in Meta webhook configuration.
- `WHATSAPP_APP_SECRET`: the App Secret from your Meta developer app; used to validate webhook signatures.
- `WHATSAPP_ACCESS_TOKEN`: a server-side access token with WhatsApp messaging permission.
- `WHATSAPP_PHONE_NUMBER_ID`: the Phone Number ID for the separate WhatsApp Business Platform number.
- `WHATSAPP_ALLOWED_NUMBERS`: your own WhatsApp number in international digits-only format, e.g. country code followed by number, no plus sign. Add other numbers only when you explicitly want to authorize them.
- Optional `WHATSAPP_GRAPH_API_VERSION`: defaults to `v23.0`.

The existing GENESIS AI provider must also have a valid key and available quota. The WhatsApp bridge uses the existing `app.llm()` function; it does not create free model credits.

## Meta setup

Use Meta's official WhatsApp Business Platform / Cloud API setup. A business phone number and WhatsApp Business Account are required. An adult should help with any business account creation, phone-number registration, or verification. Do not use your personal number as the bot's sender number.

Configure the webhook callback URL as:

`https://genesis-ai-srdo.onrender.com/webhooks/whatsapp`

Use the same `WHATSAPP_VERIFY_TOKEN` value and subscribe the WhatsApp Business Account to the `messages` webhook field. Configure the Meta app's webhook App Secret in Render as `WHATSAPP_APP_SECRET`.

## Test and status

- Health/status: `GET /api/whatsapp/status`
- Meta callback: `GET /webhooks/whatsapp` and `POST /webhooks/whatsapp`

The GET route accepts Meta's exact dotted query parameter names. Message replies are sent only to allowlisted numbers and only after a user sends a message. Normal WhatsApp customer-service messaging windows and approved-template rules still apply.
