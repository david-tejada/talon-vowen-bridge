# Talon Vowen Bridge

I've put together a reproducible workflow that lets me use Talon + a local
speech engine for commands while using modern online STT providers for
dictation.

The setup is:

- Talon + Local Engine: command recognition
- ElevenLabs, Groq, or Mistral: free-form dictation
- Vowen: currently bridges the microphone, online STT provider, and desktop

This lets me keep the reliability and low latency of local command recognition
while using newer online models for dictation. Of the providers I've tested,
ElevenLabs has given me the best transcription accuracy, with Groq and Mistral
also available as alternatives.

The dictation side can currently be used without paying anything: Vowen can be
used for free, and these online providers offer free-tier or free API usage
within their limits.

The main integration problem was preventing both recognizers from listening at
the same time. Vowen exposes its recording state through its CLI, so my Talon
script detects when dictation starts and automatically stops Talon from
listening. When dictation ends, Talon resumes listening for commands.

So in practice:

```text
commands → Talon + Local Engine
dictation → ElevenLabs / Groq / Mistral
```

I'm making the Talon script and setup reproducible so others can use the same
workflow.

Vowen is useful here, but it's not fundamentally required. If someone writes a
Talon script that directly records audio, sends it to an STT API, receives the
transcription, and inserts the text, Vowen could be removed entirely:

```text
Talon + Local Engine → commands
Talon → direct online STT API → dictation
```

The broader idea is a dual-recognizer architecture: one recognizer optimized
for commands and another for dictation, with Talon coordinating which one is
listening. I'm currently using it for Spanish, but the synchronization
mechanism itself is language-independent.

Project links: [Vowen](https://vowen.ai/), [Vowen documentation](https://docs.vowen.ai/introduction), and [Talon Voice](https://talonvoice.com/).

Small Talon user module, currently tested on Windows, that temporarily
suspends Talon's speech recognition while Vowen reports that it is recording,
then restores the exact Talon listening state that existed before the
recording began.

This repository intentionally contains only the bridge. It does not contain a
Talon profile, Dragon configuration, Parrot files, recordings, logs, models,
or any Vowen executable.

## What it does

The bridge has a deliberately narrow responsibility:

1. Read Vowen's local `server.json` descriptor.
2. Call Vowen's authenticated localhost `GET /v1/status` endpoint.
3. When `recording=true`, disable Talon's speech input.
4. When `recording=false`, restore the previous value of
   `actions.speech.enabled()`.

The bridge records `speech.engine` and `speech.language` for diagnostics, but
does not change them. This means it is independent of whether the Talon user
profile uses English, Spanish, Dragon, or another supported engine/context.

The network worker never calls Talon actions directly. It publishes the latest
observation, and a Talon `cron` callback applies it on Talon's own callback
thread. A short API failure is tolerated; after the configured grace period,
the bridge restores Talon so a crashed Vowen process cannot leave Talon asleep
forever.

## Compatibility and API status

This release was validated with **Vowen 0.53** on Windows. Other Vowen
versions may work, but the local status contract is not a public SDK and can
change between releases.

[Vowen supports macOS](https://docs.vowen.ai/faq) as well as Windows, but this
bridge has not been tested on macOS. macOS compatibility should therefore be
treated as unverified rather than guaranteed.

The bridge expects the following locally observed Vowen contract:

```text
Descriptor: %APPDATA%\vowen\cli\server.json
HTTP host:  127.0.0.1
Endpoint:   GET /v1/status
Auth:       Authorization: Bearer <token-from-server.json>
Required JSON field: recording (boolean)
Optional JSON fields: language (string), appVersion (string)
```

The endpoint is local and authenticated. Treat this as an unofficial client
integration whose compatibility depends on the Vowen version. The repository
does not include a real descriptor or token. Do not publish `server.json`,
logs, screenshots containing tokens, or copied API credentials.

The environment variable `VOWEN_TALON_SERVER_FILE` can point to a descriptor
at another path. This is useful for diagnostics and tests; normal users do not
need to set it.

## Requirements

- Windows 10 or later on x64 for the tested setup. Vowen also supports macOS
  on Apple Silicon and Intel, but this integration has not been tested there.
- [Talon Voice](https://talonvoice.com/) installed and running with a local
  command-recognition engine configured.
- [Vowen 0.53](https://vowen.ai/) installed and running with its local status
  API available.
- A microphone that is available to both Talon and Vowen.
- Internet access and an API key for a cloud transcription provider if you
  choose online dictation. Local Vowen models do not require an API key.
- Permission for Talon to control its own speech input.

No third-party Python package is required by the bridge. It uses only Python's
standard library plus Talon's runtime API.

## macOS status and likely adaptations

The synchronization logic is probably portable: it uses Talon's
`actions.speech.enabled()` and a local authenticated HTTP request, rather than
Windows-only audio or process APIs. The parts most likely to need adaptation
on macOS are:

1. **Vowen descriptor path.** The current code searches Windows
   `%APPDATA%`/`%LOCALAPPDATA%` roots. Vowen documents its macOS data directory
   as `~/Library/Application Support/vowen/`, so the client may need to also
   check `~/Library/Application Support/vowen/cli/server.json`.
2. **Talon installation path and command shell.** Talon documents `~/.talon`
   as its macOS home, so installation should copy the two files into
   `~/.talon/user` with `cp` or Finder rather than using the PowerShell example
   below.
3. **Permissions.** macOS may require microphone, Accessibility, or Input
   Monitoring permissions for Talon and Vowen. Those permissions must be
   granted to the applications themselves; this bridge cannot grant them.
4. **Runtime contract.** A macOS smoke test must confirm that Vowen 0.53
   exposes the same localhost endpoint, bearer-token descriptor, and boolean
   `recording` field. If that contract differs, only the discovery/parser
   layer should need adjustment.

For an initial diagnostic experiment, `VOWEN_TALON_SERVER_FILE` can point to
the macOS descriptor path before Talon starts. This is not a substitute for a
real macOS validation because Talon launched from Finder may not inherit a
shell environment variable.

## Configure a transcription provider

The bridge does not transcribe audio and does not create or store provider
credentials. Configure transcription inside Vowen first, then install this
bridge. Choose at least one provider supported by your Vowen version:

| Provider | Official transcription documentation | API-key page |
| --- | --- | --- |
| ElevenLabs | [Speech to Text](https://elevenlabs.io/docs/overview/capabilities/speech-to-text) | [ElevenLabs API keys](https://elevenlabs.io/app/developers/api-keys) |
| Groq | [Speech to Text](https://console.groq.com/docs/speech-to-text) | [Groq API keys](https://console.groq.com/keys) |
| Mistral | [Audio transcriptions](https://docs.mistral.ai/api/endpoint/audio/transcriptions) | [Mistral console](https://console.mistral.ai/) |
| Other Vowen provider | [Vowen transcription engines](https://docs.vowen.ai/introduction) | Use that provider's official console |

General setup:

1. Create an account with the provider and generate an API key. Use the
   provider's free tier or free credits when available; quotas and pricing can
   change independently of this repository.
2. In Vowen, select the provider and transcription model, then enter the key
   in Vowen's own settings.
3. Make one short Vowen-only dictation to confirm that the provider works
   before installing the Talon bridge.
4. Never paste the key into `vowen_talon_bridge.py`, a Talon profile, a log, or
   a public issue. The key belongs in Vowen's credential storage.

Vowen's documentation lists additional local and cloud transcription engines.
The exact provider names and free limits are controlled by Vowen and each
provider, so this bridge only requires that Vowen expose a working
`recording` status endpoint.

## Installation

### Windows

1. Download or clone this repository.
2. Copy the two integration files into Talon's user directory:

   ```powershell
   $TalonUser = Join-Path $env:APPDATA 'talon\user'
   Copy-Item '.\vowen_talon_bridge.py' $TalonUser
   Copy-Item '.\vowen_talon_bridge.talon' $TalonUser
   ```

   If your Talon user directory is elsewhere, copy the files there instead.
3. Reload Talon, or restart Talon.
4. Start Vowen and make a short recording.

### macOS (unverified)

Talon's documented macOS user directory is `~/.talon/user`:

```bash
TalonUser="$HOME/.talon/user"
mkdir -p "$TalonUser"
cp ./vowen_talon_bridge.py "$TalonUser/"
cp ./vowen_talon_bridge.talon "$TalonUser/"
```

Reload Talon, grant any requested macOS permissions, and test a short Vowen
recording. This procedure is provided as a starting point only; the
integration still needs a real macOS smoke test.

The module starts automatically on Talon's `ready` event. Repeated reloads are
handled by a process singleton that stops the previous polling thread before a
new one starts.

## Configuration

The defaults are intentionally conservative and can be overridden in the
`.talon` file or in another Talon settings file:

| Setting | Default | Minimum | Meaning |
| --- | ---: | ---: | --- |
| `user.vowen_talon_bridge_enabled` | `true` | — | Emergency enable/disable switch |
| `user.vowen_talon_bridge_poll_interval_ms` | `300` | `50` | Vowen API polling period |
| `user.vowen_talon_bridge_dispatch_interval_ms` | `50` | `20` | Talon-side application period |
| `user.vowen_talon_bridge_request_timeout_ms` | `150` | `50` | Timeout for one localhost request |
| `user.vowen_talon_bridge_failure_grace_ms` | `3000` | `500` | Time to keep Talon suspended after API loss |

The minimums protect Talon's callback thread and prevent an invalid setting
from turning into a busy loop.

## Verification in Talon

After reloading, use the Talon REPL:

```python
actions.user.vowen_talon_bridge_status()
actions.user.vowen_talon_bridge_stop()
actions.user.vowen_talon_bridge_start()
```

Expected behavior for a real short recording:

1. Before recording, note whether Talon is enabled or asleep.
2. Start recording in Vowen.
3. While Vowen reports `recording=true`, Talon's speech input becomes disabled.
4. Stop recording.
5. Talon returns to the exact enabled/disabled state from step 1.

The diagnostic string reports the last Vowen status and the captured Talon
snapshot. It intentionally never reports the bearer token.

## Offline tests

The tests do not require Talon or Vowen. They inject a small fake Talon module
and run a local HTTP server that behaves like the documented status endpoint.
They verify syntax, token handling, JSON parsing, state suspension/restoration,
and the public-tree secret/path audit.

From PowerShell, run:

```powershell
.\tests\run_tests.ps1
```

Or, if Python 3 is already on `PATH`:

```powershell
py -3 -m unittest discover -s tests -p 'test_*.py' -v
```

If Python is installed but is not on `PATH`, provide its executable explicitly:

```powershell
$env:VOWEN_TALON_PYTHON = 'C:\ruta\a\python.exe'
.\tests\run_tests.ps1
Remove-Item Env:VOWEN_TALON_PYTHON
```

The test token is synthetic and exists only inside the test process. It must
never be replaced with a real Vowen token.


## Scope limitations

- This bridge does not prove or control Dragon's internal normal/command/sleep
  substate. It controls only Talon's speech-enabled state.
- A command sent to another recognizer does not constitute confirmation that
  recognizer accepted the command.
- The Vowen endpoint may change because it is not treated here as a stable
  public SDK. If the endpoint or descriptor changes, update the client and the
  compatibility section together.

## Possible future alternative: direct Talon integration

Vowen is not a fundamental requirement for this overall workflow. A future
Talon user script could integrate directly with the ElevenLabs and Groq APIs
and coordinate the required audio, recognition, response, and Talon speech
state transitions itself.

That would remove the Vowen dependency, but it would be a substantially larger
project: the Talon script would also own authentication, API contracts,
streaming or request lifecycle, retries, cancellation, privacy boundaries,
failure recovery, and long-term maintenance as both services evolve. In other
words, direct integration is possible; it is mainly more implementation and
maintenance work. This bridge exists as the narrower option while Vowen owns
the recording lifecycle.
