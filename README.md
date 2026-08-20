# Talon Vowen Bridge

Small Windows/Talon user module that temporarily suspends Talon's speech
recognition while Vowen reports that it is recording, then restores the exact
Talon listening state that existed before the recording began.

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

- Windows.
- Talon installed and running.
- Vowen installed and running with its local status API available.
- Permission for Talon to control its own speech input.

No third-party Python package is required by the bridge. It uses only Python's
standard library plus Talon's runtime API.

## Installation

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

## Before publishing

This section is for the person who will upload the repository publicly. It is
not required for someone who only wants to install and use the bridge.

1. Run the automatic tests:

   ```powershell
   .\tests\run_tests.ps1
   ```

   These tests already pass in this release candidate.

2. Look at the files that will be uploaded. The public package should contain
   only the bridge, its `.talon` settings file, the README, and the tests.

3. Do not upload any of these private runtime files or values:
   `server.json`, bearer tokens, Talon logs, recordings, screenshots with
   personal information, profiles, absolute personal paths, or
   `talon_state.json`.

4. If possible, copy the two bridge files into a separate, empty Talon user
   directory and reload Talon there. This is a clean-install check; it avoids
   confusing this bridge with another script in an existing profile.

5. If a real Vowen/Talon recording is tested, write down only the Vowen
   version and the result. Never publish the computer's path, token, log, or
   other private details.

6. This repository includes the MIT license in `LICENSE`. Change that file
   before publishing only if you want to use a different license.

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
Talon user script could integrate directly with the ElevenLabs and Grok APIs
and coordinate the required audio, recognition, response, and Talon speech
state transitions itself.

That would remove the Vowen dependency, but it would be a substantially larger
project: the Talon script would also own authentication, API contracts,
streaming or request lifecycle, retries, cancellation, privacy boundaries,
failure recovery, and long-term maintenance as both services evolve. In other
words, direct integration is possible; it is mainly more implementation and
maintenance work. This bridge exists as the narrower option while Vowen owns
the recording lifecycle.
