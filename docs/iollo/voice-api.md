# Recorded voice API

Set `voice.api_enabled: true` in the managed Hermes configuration to expose the
recording and read-aloud functions. Both routes require the box API bearer key,
even if other API routes are configured for unauthenticated local access.
`/v1/capabilities` reports `audio_api`; `realtime_voice` remains false.

- `POST /v1/voice/transcribe`: `{audio_base64, mime: "audio/wav"}`. Accepts at most
  2 MiB / 45 seconds of 16 kHz mono PCM16 WAV. Returns `{text}`.
- `POST /v1/voice/speak`: `{text, style?}`. Text is limited to 1,200 characters,
  style to 200. Returns `{audio_base64, mime: "audio/wav", text}`. Configure a native
  TTS provider that returns WAV; incompatible output fails rather than being mislabeled.

These routes call Hermes's native transcription and TTS dispatch. They neither submit
an agent turn nor mutate session history. Submit the resulting text through the same
existing chat/session route as typed text. Read-aloud receives the reply chosen by the
client. Provider normalization may remove Markdown from spoken text.

Temporary recording and output files are removed after dispatch. At most two audio
operations run concurrently per adapter. Active operations count as busy for readiness
and rollout purposes, including after client disconnection until synchronous provider
work completes. Cancellation stops client playback/delivery; it does not guarantee
cancellation of an already submitted provider request or its charge.

Iollo's relay plugin supplies the `iollo-voice` providers and gateway configuration;
this runtime API contains no Iollo/provider credentials or provider-specific requests.
