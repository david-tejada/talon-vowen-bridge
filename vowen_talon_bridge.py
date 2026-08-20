"""Suspend Talon's speech recognizer while Vowen is recording.

This is a self-contained Talon user module.  It reads Vowen's short-lived
authenticated localhost descriptor and polls ``GET /v1/status``.  No Vowen
token is stored in this source tree: the token is read from Vowen's local
``server.json`` at request time and is used only in the HTTP Authorization
header.

The bridge deliberately has two execution lanes:

* a daemon thread performs the potentially blocking localhost HTTP request;
* a Talon ``cron`` callback applies speech actions on Talon's own callback
  thread.

That boundary matters.  A network timeout must never freeze Talon's speech
dispatcher, and Talon actions should not be invoked from an arbitrary Python
thread.
"""

import builtins
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

from talon import Module, actions, app, cron, settings


mod = Module()

# These are user settings rather than hard-coded policy so timing can be tuned
# from a .talon file without changing the state machine itself.
mod.setting(
    "vowen_talon_bridge_enabled",
    type=bool,
    default=True,
    desc="Automatically suspend Talon speech while Vowen is recording",
)
mod.setting(
    "vowen_talon_bridge_poll_interval_ms",
    type=int,
    default=300,
    desc="Vowen status polling interval in milliseconds",
)
mod.setting(
    "vowen_talon_bridge_dispatch_interval_ms",
    type=int,
    default=50,
    desc="Talon-side state application interval in milliseconds",
)
mod.setting(
    "vowen_talon_bridge_request_timeout_ms",
    type=int,
    default=150,
    desc="Maximum time for one localhost Vowen status request",
)
mod.setting(
    "vowen_talon_bridge_failure_grace_ms",
    type=int,
    default=3000,
    desc="How long a confirmed Vowen recording survives API failures",
)


_DEFAULT_POLL_INTERVAL_MS = 300
_DEFAULT_DISPATCH_INTERVAL_MS = 50
_DEFAULT_REQUEST_TIMEOUT_MS = 150
_DEFAULT_FAILURE_GRACE_MS = 3000

# Vowen writes this descriptor on app start.  Its bearer token can rotate, so
# the client intentionally re-reads the file for every status request instead
# of caching a token that may already be invalid.
_SERVER_RELATIVE_PATH = Path("vowen") / "cli" / "server.json"
_STATUS_PATH = "/v1/status"


@dataclass(frozen=True)
class _VowenServer:
    endpoint: str
    token: str


@dataclass(frozen=True)
class _VowenStatus:
    recording: bool
    language: Optional[str]
    app_version: Optional[str]


@dataclass(frozen=True)
class _VowenObservation:
    status: Optional[_VowenStatus]
    error: Optional[str]
    observed_monotonic: float


@dataclass(frozen=True)
class _TalonSpeechSnapshot:
    """The state this bridge is allowed to restore.

    ``speech_enabled`` is the actual Talon listening state and is the only
    value the bridge changes.  ``speech.engine`` and ``speech.language`` are
    recorded for diagnostics: they describe Talon's active engine/context,
    but they are not independent on/off switches.  The bridge therefore does
    not overwrite another Talon context's engine or language choice.
    """

    speech_enabled: bool
    speech_engine: Any
    speech_language: Any
    captured_monotonic: float


class _VowenApiError(RuntimeError):
    """An expected local-API problem that is safe to summarize in the log."""


class _VowenApiClient:
    """Read Vowen status without exposing the rotating bearer token."""

    def __init__(self, timeout_seconds: float):
        self._timeout_seconds = timeout_seconds

        # Do not send this request through a configured web proxy.  The
        # integration is intentionally limited to Vowen on 127.0.0.1.
        self._opener = build_opener(ProxyHandler({}))

    def _server_file_candidates(self):
        # An override makes offline diagnostics and future Vowen layout
        # changes possible.  Normal operation falls back to the standard
        # Windows per-user data roots.
        override = os.environ.get("VOWEN_TALON_SERVER_FILE")
        if override:
            yield Path(override)

        seen = set()
        for root in (os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")):
            if not root:
                continue
            candidate = Path(root) / _SERVER_RELATIVE_PATH
            key = str(candidate).casefold()
            if key not in seen:
                seen.add(key)
                yield candidate

    def _read_server(self) -> _VowenServer:
        last_error = None

        for path in self._server_file_candidates():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    descriptor = json.load(handle)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                # Vowen can replace the descriptor during a restart.  A later
                # poll should be allowed to recover instead of crashing Talon.
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            if not isinstance(descriptor, dict):
                last_error = "server.json no contiene un objeto JSON"
                continue

            port = descriptor.get("port")
            token = descriptor.get("token")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                last_error = "server.json contiene un puerto inválido"
                continue
            if not isinstance(token, str) or not token:
                last_error = "server.json no contiene un token válido"
                continue

            return _VowenServer(
                endpoint=f"http://127.0.0.1:{port}{_STATUS_PATH}",
                token=token,
            )

        if last_error:
            raise _VowenApiError(f"no se pudo leer server.json ({last_error})")
        raise _VowenApiError("server.json de Vowen no está disponible")

    def read_status(self) -> _VowenStatus:
        server = self._read_server()
        request = Request(
            server.endpoint,
            headers={
                # The token must never be logged or included in diagnostics.
                "Authorization": f"Bearer {server.token}",
                "Accept": "application/json",
            },
            method="GET",
        )

        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw_payload = response.read(64 * 1024)
        except HTTPError as exc:
            if exc.code == 401:
                raise _VowenApiError(
                    "Vowen rechazó el token; server.json puede estar rotando"
                ) from None
            raise _VowenApiError(f"API local de Vowen devolvió HTTP {exc.code}") from None
        except URLError as exc:
            raise _VowenApiError(f"API local de Vowen no disponible ({exc.reason})") from None
        except OSError as exc:
            raise _VowenApiError(f"falló la conexión local a Vowen ({exc})") from None

        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _VowenApiError(f"respuesta JSON inválida de Vowen ({exc})") from None

        if not isinstance(payload, dict) or not isinstance(payload.get("recording"), bool):
            raise _VowenApiError("respuesta de Vowen no contiene recording=true/false")

        language = payload.get("language")
        app_version = payload.get("appVersion")
        return _VowenStatus(
            recording=payload["recording"],
            language=language if isinstance(language, str) else None,
            app_version=app_version if isinstance(app_version, str) else None,
        )


def _setting_or_default(name: str, default: Any) -> Any:
    """Read a Talon setting while keeping startup resilient to load order."""

    try:
        value = settings.get(name)
    except Exception:
        return default
    return default if value is None else value


def _milliseconds_setting(name: str, default: int, minimum: int) -> float:
    """Return a validated setting as seconds for Python timers."""

    raw_value = _setting_or_default(name, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) / 1000.0


class _VowenTalonBridge:
    """State machine coordinating Vowen's recording bit and Talon speech."""

    _ERROR_LOG_INTERVAL_SECONDS = 5.0

    def __init__(self):
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._apply_job = None
        self._started = False

        # The worker publishes only the newest observation.  A status poll is
        # sampled state, not an event queue; applying the newest value avoids
        # replaying stale transitions after a short API outage.
        self._pending: Optional[_VowenObservation] = None
        self._last_success_monotonic: Optional[float] = None
        self._last_status: Optional[_VowenStatus] = None
        self._desired_recording: Optional[bool] = None

        # These fields are read and changed on Talon's cron callback thread.
        # The lock is still used when reporting them so diagnostics cannot
        # observe a half-written snapshot.
        self._snapshot: Optional[_TalonSpeechSnapshot] = None
        self._suspension_notification_sent = False

        self._last_error_key: Optional[str] = None
        self._last_error_logged_monotonic = 0.0

    def _log(self, message: str):
        print(f"[vowen_talon_bridge] {message}")

    def _notify(self, message: str):
        try:
            app.notify(message)
        except Exception as exc:
            # Notifications are convenience UI only; they must not break the
            # state machine if Talon is shutting down or the UI is unavailable.
            self._log(f"no se pudo mostrar la notificación ({exc})")

    def _publish_error(self, error: str, observed_monotonic: float):
        observation = _VowenObservation(
            status=None,
            error=error,
            observed_monotonic=observed_monotonic,
        )
        should_log = False
        with self._lock:
            if not self._started or self._stop_event.is_set():
                return
            self._pending = observation
            if (
                error != self._last_error_key
                or observed_monotonic - self._last_error_logged_monotonic
                >= self._ERROR_LOG_INTERVAL_SECONDS
            ):
                self._last_error_key = error
                self._last_error_logged_monotonic = observed_monotonic
                should_log = True

        if should_log:
            self._log(f"API de Vowen: {error}")

    def _publish_success(self, status: _VowenStatus, observed_monotonic: float):
        observation = _VowenObservation(
            status=status,
            error=None,
            observed_monotonic=observed_monotonic,
        )
        recovered = False
        with self._lock:
            if not self._started or self._stop_event.is_set():
                return
            self._pending = observation
            self._last_success_monotonic = observed_monotonic
            self._last_status = status
            if self._last_error_key is not None:
                self._last_error_key = None
                self._last_error_logged_monotonic = 0.0
                recovered = True

        if recovered:
            self._log("API local de Vowen disponible nuevamente")

    def _poll_loop(self, client: _VowenApiClient, interval_seconds: float):
        # Schedule against a monotonic deadline so request duration is part of
        # the polling period rather than silently adding to it.
        next_deadline = time.monotonic()
        while not self._stop_event.is_set():
            try:
                status = client.read_status()
            except Exception as exc:
                self._publish_error(
                    f"{type(exc).__name__}: {exc}",
                    time.monotonic(),
                )
            else:
                self._publish_success(status, time.monotonic())

            next_deadline += interval_seconds
            wait_seconds = max(0.0, next_deadline - time.monotonic())
            if self._stop_event.wait(wait_seconds):
                return

    def start(self):
        """Start polling once; repeated calls are intentionally harmless."""

        if not bool(_setting_or_default("user.vowen_talon_bridge_enabled", True)):
            self._log("integración desactivada por user.vowen_talon_bridge_enabled")
            return

        poll_interval = _milliseconds_setting(
            "user.vowen_talon_bridge_poll_interval_ms",
            _DEFAULT_POLL_INTERVAL_MS,
            minimum=50,
        )
        dispatch_interval = _milliseconds_setting(
            "user.vowen_talon_bridge_dispatch_interval_ms",
            _DEFAULT_DISPATCH_INTERVAL_MS,
            minimum=20,
        )
        request_timeout = _milliseconds_setting(
            "user.vowen_talon_bridge_request_timeout_ms",
            _DEFAULT_REQUEST_TIMEOUT_MS,
            minimum=50,
        )
        failure_grace = _milliseconds_setting(
            "user.vowen_talon_bridge_failure_grace_ms",
            _DEFAULT_FAILURE_GRACE_MS,
            minimum=500,
        )

        with self._lock:
            if self._started:
                return

            self._started = True
            self._stop_event.clear()
            self._pending = None
            self._last_success_monotonic = None
            self._last_status = None
            self._desired_recording = None
            self._last_error_key = None
            self._last_error_logged_monotonic = 0.0
            self._failure_grace_seconds = failure_grace

        try:
            # The worker does I/O; this cron job only transfers the newest
            # observation into Talon's action/state world.
            apply_job = cron.interval(
                f"{int(dispatch_interval * 1000)}ms",
                self._apply_pending,
            )
            client = _VowenApiClient(request_timeout)
            poll_thread = threading.Thread(
                target=self._poll_loop,
                args=(client, poll_interval),
                name="vowen-talon-bridge",
                daemon=True,
            )
        except Exception:
            with self._lock:
                self._started = False
                self._stop_event.set()
            raise

        with self._lock:
            self._apply_job = apply_job
            self._poll_thread = poll_thread
        poll_thread.start()

        self._log(
            "iniciada (Vowen cada "
            f"{int(poll_interval * 1000)} ms; aplicación Talon cada "
            f"{int(dispatch_interval * 1000)} ms; gracia de API "
            f"{int(failure_grace * 1000)} ms)"
        )

    def stop(self):
        """Stop polling and restore a captured Talon state if necessary."""

        with self._lock:
            was_started = self._started
            self._started = False
            self._stop_event.set()
            apply_job = self._apply_job
            poll_thread = self._poll_thread
            self._apply_job = None
            self._poll_thread = None
            self._pending = None
            self._desired_recording = None

        if apply_job is not None:
            cron.cancel(apply_job)

        # The normal request timeout is short, so this join prevents an old
        # worker from publishing into a subsequent start without blocking the
        # Talon thread for an unbounded duration.
        if (
            poll_thread is not None
            and poll_thread is not threading.current_thread()
            and poll_thread.is_alive()
        ):
            poll_thread.join(timeout=0.25)

        # A manual stop is an explicit request to disengage the integration;
        # restore the pre-Vowen state before returning control to the user.
        self._restore_snapshot("integración detenida")

        if was_started:
            self._log("detenida")

    def _safe_speech_enabled(self) -> Optional[bool]:
        try:
            return bool(actions.speech.enabled())
        except Exception as exc:
            self._log(f"no se pudo leer el estado de escucha de Talon ({exc})")
            return None

    def _capture_snapshot(self) -> Optional[_TalonSpeechSnapshot]:
        speech_enabled = self._safe_speech_enabled()
        if speech_enabled is None:
            return None

        # Engine and language are captured for an audit trail only.  They stay
        # owned by Talon's context system and are never overwritten here.
        return _TalonSpeechSnapshot(
            speech_enabled=speech_enabled,
            speech_engine=_setting_or_default("speech.engine", None),
            speech_language=_setting_or_default("speech.language", None),
            captured_monotonic=time.monotonic(),
        )

    def _ensure_speech_disabled(self) -> bool:
        current = self._safe_speech_enabled()
        if current is None:
            return False

        try:
            if current:
                actions.speech.disable()
            final_state = bool(actions.speech.enabled())
        except Exception as exc:
            self._log(f"no se pudo suspender la escucha de Talon ({exc})")
            return False

        if final_state:
            self._log("Talon siguió habilitado después de pedir suspensión; se reintentará")
            return False
        return True

    def _suspend_for_vowen(self):
        if self._snapshot is None:
            snapshot = self._capture_snapshot()
            if snapshot is None:
                return
            self._snapshot = snapshot
            self._suspension_notification_sent = False
            self._log(
                "Vowen está grabando; snapshot Talon="
                f"{snapshot.speech_enabled}, engine="
                f"{snapshot.speech_engine!r}, idioma="
                f"{snapshot.speech_language!r}"
            )

        # This runs on every Talon-side tick while Vowen reports true.  If a
        # user or another integration wakes Talon mid-recording, the bridge
        # takes it back to Sleep until Vowen stops.
        if self._ensure_speech_disabled() and not self._suspension_notification_sent:
            self._suspension_notification_sent = True
            self._notify("Vowen está grabando: Talon suspendido temporalmente")

    def _restore_snapshot(self, reason: str):
        snapshot = self._snapshot
        if snapshot is None:
            return

        current = self._safe_speech_enabled()
        if current is None:
            return

        try:
            if current != snapshot.speech_enabled:
                if snapshot.speech_enabled:
                    actions.speech.enable()
                else:
                    actions.speech.disable()
            restored_state = bool(actions.speech.enabled())
        except Exception as exc:
            self._log(f"no se pudo restaurar Talon ({exc}); se reintentará ({reason})")
            return

        if restored_state != snapshot.speech_enabled:
            self._log(
                "Talon no confirmó el estado anterior "
                f"({snapshot.speech_enabled}); se reintentará ({reason})"
            )
            return

        # Engine/language remain under Talon's context system.  If another
        # component changed either setting during recording, report it but do
        # not overwrite that newer decision during restoration.
        current_engine = _setting_or_default("speech.engine", None)
        current_language = _setting_or_default("speech.language", None)
        if (
            current_engine != snapshot.speech_engine
            or current_language != snapshot.speech_language
        ):
            self._log(
                "engine/idioma cambiaron durante Vowen; no se fuerzan valores "
                f"anteriores (ahora engine={current_engine!r}, "
                f"language={current_language!r})"
            )

        self._snapshot = None
        self._suspension_notification_sent = False
        self._notify("Vowen terminó: Talon volvió a su estado anterior")
        self._log(
            f"restaurado speech_enabled={snapshot.speech_enabled} ({reason})"
        )

    def _apply_pending(self):
        """Apply the newest API observation on Talon's own callback thread."""

        with self._lock:
            observation = self._pending
            self._pending = None
            if observation is not None and observation.status is not None:
                self._desired_recording = observation.status.recording

            desired_recording = self._desired_recording
            last_success = self._last_success_monotonic
            failure_grace = getattr(
                self,
                "_failure_grace_seconds",
                _DEFAULT_FAILURE_GRACE_MS / 1000.0,
            )

        # Once recording was positively observed, a transient API failure must
        # not immediately wake Talon.  After the bounded grace period, restore
        # Talon so a crashed Vowen cannot leave it asleep forever.
        if (
            desired_recording is True
            and last_success is not None
            and time.monotonic() - last_success > failure_grace
        ):
            with self._lock:
                if (
                    self._desired_recording is True
                    and self._last_success_monotonic == last_success
                ):
                    self._desired_recording = False
                    desired_recording = False
            if desired_recording is False:
                self._log(
                    "API de Vowen quedó sin respuesta; se restaura Talon "
                    f"después de {int(failure_grace * 1000)} ms de gracia"
                )

        if desired_recording is True:
            self._suspend_for_vowen()
        elif desired_recording is False:
            # Keeping this call active on every dispatch tick makes restoration
            # retryable if an action is momentarily unavailable.
            self._restore_snapshot("fin de grabación de Vowen")

    def describe(self) -> str:
        """Return a compact diagnostic string for Talon REPL/manual checks."""

        with self._lock:
            started = self._started
            desired = self._desired_recording
            last_success = self._last_success_monotonic
            status = self._last_status
            snapshot = self._snapshot

        age = None
        if last_success is not None:
            age = max(0.0, time.monotonic() - last_success)

        return (
            "vowen_talon_bridge("
            f"started={started}, desired_recording={desired}, "
            f"last_success_age_s={None if age is None else round(age, 3)}, "
            f"vowen_recording={None if status is None else status.recording}, "
            f"vowen_language={None if status is None else status.language!r}, "
            f"vowen_app_version={None if status is None else status.app_version!r}, "
            f"snapshot_speech_enabled={None if snapshot is None else snapshot.speech_enabled}, "
            f"snapshot_engine={None if snapshot is None else snapshot.speech_engine!r}, "
            f"snapshot_language={None if snapshot is None else snapshot.speech_language!r}"
            ")"
        )


# Talon can reload a user Python file without automatically stopping threads
# created by the previous version.  Keeping one singleton in builtins lets a
# reload cancel the old poller before creating a new one.  Thread inspection is
# only a fallback for a first install where the previous instance has not yet
# registered itself in builtins.
_BRIDGE_SINGLETON_KEY = "_talon_vowen_talon_bridge_singleton"
_previous_bridge = getattr(builtins, _BRIDGE_SINGLETON_KEY, None)

_previous_bridges = []
if _previous_bridge is not None:
    _previous_bridges.append(_previous_bridge)

for _thread in threading.enumerate():
    if _thread.name != "vowen-talon-bridge":
        continue
    # Python keeps the bound method in Thread._target while the thread lives.
    # It is an internal API, so every access is defensive and limited to this
    # reload-cleanup fallback.
    _target = getattr(_thread, "_target", None)
    _owner = getattr(_target, "__self__", None)
    if _owner is not None and _owner not in _previous_bridges:
        _previous_bridges.append(_owner)

for _old_bridge in _previous_bridges:
    try:
        _old_bridge.stop()
    except Exception as exc:
        # Reload should continue even if an old instance is already closing.
        print(f"[vowen_talon_bridge] no se pudo cerrar una instancia anterior ({exc})")

bridge = _VowenTalonBridge()
setattr(builtins, _BRIDGE_SINGLETON_KEY, bridge)


def _on_ready():
    bridge.start()


app.register("ready", _on_ready)


@mod.action_class
class UserActions:
    def vowen_talon_bridge_start():
        """Start or resume the Vowen/Talon bridge."""

        bridge.start()

    def vowen_talon_bridge_stop():
        """Stop the bridge and restore the captured Talon speech state."""

        bridge.stop()

    def vowen_talon_bridge_status() -> str:
        """Return bridge/API/snapshot state for the Talon REPL."""

        return bridge.describe()
