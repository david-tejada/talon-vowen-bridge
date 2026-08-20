"""Offline verification for the public Vowen/Talon bridge.

The real Talon runtime is intentionally not required here.  A small fake
module exercises the bridge's public-release assumptions without touching the
active Talon profile or a real Vowen token.
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = PACKAGE_ROOT / "vowen_talon_bridge.py"


class _FakeSpeech:
    def __init__(self):
        self.enabled_state = True

    def enabled(self):
        return self.enabled_state

    def enable(self):
        self.enabled_state = True

    def disable(self):
        self.enabled_state = False


class _FakeSettings:
    def __init__(self):
        self.values = {
            "speech.engine": "dragon",
            "speech.language": "es",
        }

    def get(self, name):
        return self.values.get(name)


class _FakeCron:
    def interval(self, _period, callback):
        return callback

    def cancel(self, _job):
        return None


class _FakeModule:
    def setting(self, *_args, **_kwargs):
        return None

    def action_class(self, cls):
        return cls


class _FakeApp:
    def __init__(self):
        self.notifications = []

    def register(self, *_args, **_kwargs):
        return None

    def notify(self, message):
        self.notifications.append(message)


class _BridgeModuleHarness:
    """Load the source with fake Talon objects and restore import state."""

    def __init__(self):
        self.speech = _FakeSpeech()
        self.settings = _FakeSettings()
        self.cron = _FakeCron()
        self.app = _FakeApp()

        fake_actions = types.SimpleNamespace(speech=self.speech)
        fake_talon = types.ModuleType("talon")
        fake_talon.Module = _FakeModule
        fake_talon.actions = fake_actions
        fake_talon.app = self.app
        fake_talon.cron = self.cron
        fake_talon.settings = self.settings

        self._previous_talon = sys.modules.get("talon")
        sys.modules["talon"] = fake_talon
        self.module_name = "public_vowen_talon_bridge_test"
        spec = importlib.util.spec_from_file_location(self.module_name, SOURCE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.module
        assert spec.loader is not None
        spec.loader.exec_module(self.module)

    def close(self):
        # Avoid leaving a fake singleton behind for another test or a caller
        # importing Talon in the same Python process.
        if getattr(builtins, "_talon_vowen_talon_bridge_singleton", None) is getattr(
            self.module, "bridge", None
        ):
            del builtins._talon_vowen_talon_bridge_singleton
        sys.modules.pop(self.module_name, None)
        if self._previous_talon is None:
            sys.modules.pop("talon", None)
        else:
            sys.modules["talon"] = self._previous_talon


class _StatusHandler(BaseHTTPRequestHandler):
    expected_token = "synthetic-test-token"
    response_body = {"recording": True, "language": "es", "appVersion": "test"}

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/v1/status":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {self.expected_token}":
            self.send_error(401)
            return

        payload = json.dumps(self.response_body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        # Keep test output deterministic and prevent request details from
        # becoming accidental publication artifacts.
        return None


class PublicBridgeTests(unittest.TestCase):
    def setUp(self):
        self.harness = _BridgeModuleHarness()

    def tearDown(self):
        self.harness.close()

    def test_source_is_valid_python_and_uses_only_expected_external_import(self):
        tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
        external_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                external_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                external_roots.add(node.module.split(".")[0])

        standard_library = {
            "__future__",
            "builtins",
            "dataclasses",
            "json",
            "os",
            "pathlib",
            "threading",
            "time",
            "typing",
            "urllib",
        }
        self.assertEqual(external_roots - standard_library, {"talon"})

    def test_local_api_is_authenticated_and_parsed_without_logging_token(self):
        server = HTTPServer(("127.0.0.1", 0), _StatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                descriptor_path = Path(temp_dir) / "server.json"
                descriptor_path.write_text(
                    json.dumps({"port": server.server_port, "token": _StatusHandler.expected_token}),
                    encoding="utf-8",
                )
                old_override = os.environ.get("VOWEN_TALON_SERVER_FILE")
                os.environ["VOWEN_TALON_SERVER_FILE"] = str(descriptor_path)
                try:
                    status = self.harness.module._VowenApiClient(1.0).read_status()
                finally:
                    if old_override is None:
                        os.environ.pop("VOWEN_TALON_SERVER_FILE", None)
                    else:
                        os.environ["VOWEN_TALON_SERVER_FILE"] = old_override

            self.assertTrue(status.recording)
            self.assertEqual(status.language, "es")
            self.assertEqual(status.app_version, "test")
            self.assertNotIn(_StatusHandler.expected_token, self.harness.module.bridge.describe())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)

    def test_recording_suspends_and_false_restores_previous_enabled_state(self):
        module = self.harness.module
        bridge = module._VowenTalonBridge()
        bridge._started = True

        now = time.monotonic()
        bridge._publish_success(module._VowenStatus(True, "es", "test"), now)
        bridge._apply_pending()
        self.assertFalse(self.harness.speech.enabled_state)

        bridge._publish_success(module._VowenStatus(False, "es", "test"), time.monotonic())
        bridge._apply_pending()
        self.assertTrue(self.harness.speech.enabled_state)
        self.assertIsNone(bridge._snapshot)

    def test_initially_disabled_talon_stays_disabled_after_recording(self):
        module = self.harness.module
        self.harness.speech.enabled_state = False
        bridge = module._VowenTalonBridge()
        bridge._started = True

        bridge._publish_success(module._VowenStatus(True, None, None), time.monotonic())
        bridge._apply_pending()
        bridge._publish_success(module._VowenStatus(False, None, None), time.monotonic())
        bridge._apply_pending()

        self.assertFalse(self.harness.speech.enabled_state)

    def test_public_tree_has_no_personal_absolute_paths_or_runtime_secrets(self):
        forbidden_absolute_path = re.compile(r"(?i)(?:[A-Z]:\\(?:Users|Documents|Desktop)\\|/Users/)")
        runtime_secret_file = re.compile(r"(?i)(?:^|[\\/])(?:server\.json|talon\.log|talon_state\.json)$")

        for path in PACKAGE_ROOT.rglob("*"):
            # A clone contains binary Git metadata under .git.  The audit is
            # about files that would be distributed as the release payload,
            # not the repository's local object database and index.
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(PACKAGE_ROOT)
            # The test source contains the audit regex itself, so the release
            # payload is audited separately from the verification machinery.
            if "tests" in relative.parts:
                continue
            self.assertIsNone(runtime_secret_file.search(str(relative)))
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(forbidden_absolute_path.search(text), str(relative))


if __name__ == "__main__":
    unittest.main(verbosity=2)
