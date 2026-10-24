# GENESIS AI Companion (Android source scaffold)

This is Android source code, not a prebuilt APK. Build it with Android Studio (JDK 17+, Android SDK 35, Gradle plugin 8.6.1). Open this `android/` folder as a project and build the `app` module.

## What it does
- Saves the server URL and, if selected, the GENESIS admin key in AndroidX EncryptedSharedPreferences.
- Sends only the one-sentence request the user types to `POST /api/tasks` over HTTPS.
- Opens the GENESIS dashboard, opens YouTube search, launches YouTube/Gmail if installed, and opens Android's share chooser only after the user taps.
- Shows the returned task ID so the user can track work on the dashboard.

## Privacy boundaries
- Requests only `INTERNET` permission.
- Does not request contacts, photos/media, SMS, call logs, microphone, camera, location, notifications, or Accessibility Service.
- Does not inspect other apps' screens or content and cannot silently control all installed apps.
- The admin key is encrypted at rest using AndroidX Security Crypto. It is sent only as `X-GENESIS-KEY` to the HTTPS server the user configured.
- The AI provider key belongs on the server in Render environment variables. Never enter it in this companion.

## Limitations
- This is a source scaffold; no signed APK is included and it has not been installed on a physical phone.
- Android intents can open apps or pass content with user interaction, but cannot automate arbitrary apps. Each deeper integration must use the app's official API or a separately designed, opt-in Android capability.
- Study lesson generation still needs a valid compatible AI provider key configured on the server. Current output is a browser-playable lesson, not an exported MP4.
