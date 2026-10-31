# android

Kotlin/Compose client for Hermes Talk.

Planned modules:

- `app` — call UI, transcript, settings
- `rtvi` — WebRTC/RTVI client wrapper
- `wakeword` — on-device keyword spotting
- `attachments` — picker, share-sheet target, upload queue
- `auth` — device token storage (EncryptedSharedPreferences)

The app talks to the relay over the LAN only; no Google Play Services dependency is required for the core
call path.
