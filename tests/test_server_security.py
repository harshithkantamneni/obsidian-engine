"""Security tests for server/webhook_server.py — auth, setup wizard, dashboard key."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import server.webhook_server as ws  # noqa: E402

REMOTE = {"REMOTE_ADDR": "203.0.113.7"}
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}
KEY = "correct-horse-battery-staple"

SAMPLE_YAML = """\
# Top comment — must survive
profile: documentary  # active profile

# ── Providers ──
providers:
  llm:
    name: anthropic   # llm comment
  tts:
    name: elevenlabs
  upload:
    name: local
    options:
      # output_dir: "./outputs/final"
  music:
    name: auto                           # "epidemic_sound", "local", or "auto"

voice:
  name: should-not-change
"""


@pytest.fixture
def client():
    ws.app.config["TESTING"] = True
    with ws.app.test_client() as c:
        yield c


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(ws, "TRIGGER_KEY", "")


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(ws, "TRIGGER_KEY", KEY)


@pytest.fixture
def setup_files(tmp_path, monkeypatch):
    """Redirect .env / obsidian.yaml writes to tmp_path; restore os.environ."""
    env_path = tmp_path / ".env"
    yaml_path = tmp_path / "obsidian.yaml"
    yaml_path.write_text(SAMPLE_YAML)
    monkeypatch.setattr(ws, "ENV_PATH", env_path)
    monkeypatch.setattr(ws, "CONFIG_YAML_PATH", yaml_path)
    with patch.dict(os.environ):
        yield env_path, yaml_path


# ── Protected routes ─────────────────────────────────────────────────────────

class TestRequireKey:
    def test_rejects_when_key_unset_remote(self, client, no_key):
        r = client.get("/status", environ_base=REMOTE)
        assert r.status_code == 503
        assert "TRIGGER_KEY" in r.get_json()["message"]

    def test_rejects_when_key_unset_even_from_loopback(self, client, no_key):
        r = client.post("/kill", environ_base=LOCAL)
        assert r.status_code == 503

    def test_rejects_when_key_unset_with_empty_header(self, client, no_key):
        r = client.post("/queue/delete", headers={"X-Trigger-Key": ""},
                        json={"id": "x"}, environ_base=REMOTE)
        assert r.status_code == 503

    def test_rejects_wrong_key(self, client, with_key):
        r = client.get("/status", headers={"X-Trigger-Key": "nope"}, environ_base=REMOTE)
        assert r.status_code == 401

    def test_rejects_missing_key(self, client, with_key):
        r = client.get("/status", environ_base=REMOTE)
        assert r.status_code == 401

    def test_accepts_correct_header_key(self, client, with_key):
        r = client.get("/status", headers={"X-Trigger-Key": KEY}, environ_base=REMOTE)
        assert r.status_code == 200
        assert "running" in r.get_json()

    def test_query_param_key_rejected_on_non_stream_route(self, client, with_key):
        r = client.get(f"/status?key={KEY}", environ_base=REMOTE)
        assert r.status_code == 401

    def test_query_param_key_accepted_on_stream(self, client, with_key):
        r = client.get(f"/stream?key={KEY}", environ_base=REMOTE)
        assert r.status_code == 200
        assert "text/event-stream" in r.content_type
        r.close()

    def test_stream_wrong_query_key_rejected(self, client, with_key):
        r = client.get("/stream?key=wrong", environ_base=REMOTE)
        assert r.status_code == 401

    def test_setup_status_locked_remotely_without_key(self, client, no_key):
        r = client.get("/api/setup/status", environ_base=REMOTE)
        assert r.status_code == 503

    def test_setup_status_open_on_loopback_first_run(self, client, no_key):
        r = client.get("/api/setup/status", environ_base=LOCAL)
        assert r.status_code == 200
        data = r.get_json()
        assert "keys" in data and "available_profiles" in data
        assert data["trigger_key_configured"] is False

    def test_setup_status_requires_key_once_configured(self, client, with_key):
        r = client.get("/api/setup/status", environ_base=LOCAL)
        assert r.status_code == 401


# ── Setup save ───────────────────────────────────────────────────────────────

class TestSetupSave:
    def _save(self, client, payload, env=REMOTE):
        return client.post("/api/setup/save", json=payload,
                           headers={"X-Trigger-Key": KEY}, environ_base=env)

    def test_rejects_unknown_env_key(self, client, with_key, setup_files):
        env_path, _ = setup_files
        r = self._save(client, {"keys": {"PATH": "/tmp/evil", "FAL_KEY": "fal_ok"}})
        assert r.status_code == 400
        data = r.get_json()
        assert data["success"] is False
        assert any("PATH" in e for e in data["errors"])
        assert not env_path.exists()  # nothing written on validation failure

    def test_rejects_trigger_key_override(self, client, with_key, setup_files):
        r = self._save(client, {"keys": {"TRIGGER_KEY": "attacker"}})
        assert r.status_code == 400

    @pytest.mark.parametrize("bad", ["abc\nTRIGGER_KEY=x", "abc\rdef", "abc\x00def"])
    def test_rejects_newline_and_nul_values(self, client, with_key, setup_files, bad):
        env_path, _ = setup_files
        r = self._save(client, {"keys": {"FAL_KEY": bad}})
        assert r.status_code == 400
        assert not env_path.exists()

    def test_saves_allowed_key_stripped_and_keeps_comments(self, client, with_key, setup_files):
        env_path, _ = setup_files
        env_path.write_text("# my comment\nFAL_KEY=old\nOTHER=1\n")
        r = self._save(client, {"keys": {"FAL_KEY": "  fal_new  ", "PEXELS_API_KEY": "px"}})
        assert r.status_code == 200, r.get_json()
        text = env_path.read_text()
        assert "# my comment" in text
        assert "FAL_KEY=fal_new\n" in text
        assert "PEXELS_API_KEY=px\n" in text
        assert "OTHER=1" in text

    @pytest.mark.parametrize("bad", ["../../etc/passwd", "_template", "nonexistent_xyz",
                                     "documentary\nproviders: x", 123])
    def test_rejects_bad_profile(self, client, with_key, setup_files, bad):
        _, yaml_path = setup_files
        r = self._save(client, {"profile": bad})
        assert r.status_code == 400
        assert yaml_path.read_text() == SAMPLE_YAML

    def test_accepts_valid_profile_and_preserves_comments(self, client, with_key, setup_files):
        _, yaml_path = setup_files
        r = self._save(client, {"profile": "explainer"})
        assert r.status_code == 200, r.get_json()
        text = yaml_path.read_text()
        assert "profile: explainer  # active profile" in text
        assert "# Top comment — must survive" in text

    def test_accepts_custom_dotted_provider(self, client, with_key, setup_files):
        _, yaml_path = setup_files
        r = self._save(client, {"providers": {
            "llm": "my_pkg.llm.OllamaProvider", "music": "local", "sfx": "auto",
        }})
        assert r.status_code == 200, r.get_json()
        text = yaml_path.read_text()
        assert "    name: my_pkg.llm.OllamaProvider   # llm comment" in text
        assert '    name: local                           # "epidemic_sound"' in text
        # sfx section didn't exist — it gets added inside providers
        assert "  sfx:\n    name: auto" in text
        assert "name: should-not-change" in text  # other sections untouched
        assert "# ── Providers ──" in text

        import yaml
        parsed = yaml.safe_load(text)
        assert parsed["providers"]["llm"]["name"] == "my_pkg.llm.OllamaProvider"
        assert parsed["providers"]["sfx"]["name"] == "auto"
        assert parsed["providers"]["tts"]["name"] == "elevenlabs"

    @pytest.mark.parametrize("bad", ["evil\n  injected: true", "has space.Cls",
                                     "1bad.Cls", "notbuiltin", "../x.y"])
    def test_rejects_bad_provider_names(self, client, with_key, setup_files, bad):
        _, yaml_path = setup_files
        r = self._save(client, {"providers": {"llm": bad}})
        assert r.status_code == 400
        assert yaml_path.read_text() == SAMPLE_YAML

    def test_rejects_unknown_provider_type(self, client, with_key, setup_files):
        r = self._save(client, {"providers": {"database": "anthropic"}})
        assert r.status_code == 400

    def test_first_run_loopback_generates_trigger_key(self, client, no_key, setup_files):
        env_path, _ = setup_files
        r = client.post("/api/setup/save", json={"keys": {"FAL_KEY": "fal_abc"}},
                        environ_base=LOCAL)
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data["success"] is True
        assert data["trigger_key_generated"] is True
        new_key = data["trigger_key"]
        assert len(new_key) >= 32
        assert f"TRIGGER_KEY={new_key}\n" in env_path.read_text()
        assert ws.TRIGGER_KEY == new_key
        # API is now locked to that key
        r2 = client.get("/status", environ_base=LOCAL)
        assert r2.status_code == 401
        r3 = client.get("/status", headers={"X-Trigger-Key": new_key}, environ_base=LOCAL)
        assert r3.status_code == 200

    def test_first_run_save_blocked_remotely(self, client, no_key, setup_files):
        env_path, _ = setup_files
        r = client.post("/api/setup/save", json={"keys": {"FAL_KEY": "fal_abc"}},
                        environ_base=REMOTE)
        assert r.status_code == 503
        assert not env_path.exists()

    def test_key_not_regenerated_when_configured(self, client, with_key, setup_files):
        r = self._save(client, {"keys": {"FAL_KEY": "fal_abc"}})
        assert r.status_code == 200
        assert "trigger_key" not in r.get_json()
        assert "TRIGGER_KEY" not in setup_files[0].read_text()


# ── Dashboard key injection / login ──────────────────────────────────────────

class TestDashboardKey:
    def test_key_not_injected_for_remote_unauthenticated(self, client, with_key, monkeypatch):
        monkeypatch.setattr(ws, "DASHBOARD_PASSWORD", "")
        r = client.get("/", environ_base=REMOTE)
        assert r.status_code == 200
        assert KEY not in r.get_data(as_text=True)

    def test_key_injected_for_loopback(self, client, with_key, monkeypatch):
        monkeypatch.setattr(ws, "DASHBOARD_PASSWORD", "")
        r = client.get("/", environ_base=LOCAL)
        assert KEY in r.get_data(as_text=True)

    def test_key_injected_for_logged_in_remote(self, client, with_key, monkeypatch):
        monkeypatch.setattr(ws, "DASHBOARD_PASSWORD", "pw")
        ws._login_attempts.clear()
        r = client.get("/", environ_base=REMOTE)
        assert r.status_code == 302  # login required
        r = client.post("/login", data={"password": "pw"}, environ_base=REMOTE)
        assert r.status_code == 302
        r = client.get("/", environ_base=REMOTE)
        assert KEY in r.get_data(as_text=True)

    def test_login_without_password_remote_does_not_loop(self, client, monkeypatch):
        monkeypatch.setattr(ws, "DASHBOARD_PASSWORD", "")
        r = client.get("/login", environ_base=REMOTE)
        assert r.status_code == 403
        r = client.get("/login", environ_base=LOCAL)
        assert r.status_code == 302

    def test_login_rate_limited(self, client, monkeypatch):
        monkeypatch.setattr(ws, "DASHBOARD_PASSWORD", "right")
        ws._login_attempts.clear()
        env = {"REMOTE_ADDR": "198.51.100.9"}
        for _ in range(ws.LOGIN_MAX_ATTEMPTS):
            r = client.post("/login", data={"password": "wrong"}, environ_base=env)
            assert r.status_code == 200
        r = client.post("/login", data={"password": "right"}, environ_base=env)
        assert r.status_code == 429
        ws._login_attempts.clear()

    def test_session_cookie_flags(self):
        assert ws.app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert ws.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_js_escape(self):
        assert ws._js_string_escape('a"b\'c</script>') == 'a\\"b\\u0027c\\u003c/script\\u003e'


# ── Misc hardening ───────────────────────────────────────────────────────────

class TestMisc:
    def test_audit_clean_strips_crlf(self):
        assert "\n" not in ws._audit_clean("a\nb\rc")
        assert "\r" not in ws._audit_clean("a\nb\rc")

    def test_topic_newlines_removed(self):
        topic, err = ws._validate_topic("The Dark\nHistory\r\nof Things")
        assert err is None
        assert "\n" not in topic and "\r" not in topic

    def test_default_host_is_loopback(self):
        # Module default when HOST is unset in the environment
        if not os.getenv("HOST"):
            assert ws.HOST == "127.0.0.1"

    def test_kill_sets_flag_before_terminate(self, client, with_key):
        seen = {}

        class FakeProc:
            def terminate(self):
                seen["killed_flag"] = ws._state.get("_killed")

            def wait(self, timeout=None):
                return 0

        with ws._lock:
            ws._state["running"] = True
        old_proc = ws._proc
        ws._proc = FakeProc()
        try:
            r = client.post("/kill", headers={"X-Trigger-Key": KEY})
            assert r.status_code == 200
            assert seen["killed_flag"] is True
            assert ws._state["last_status"] == "killed"
        finally:
            ws._proc = old_proc
            with ws._lock:
                ws._state["running"] = False
                ws._state.pop("_killed", None)
                ws._state["last_status"] = "idle"

    def test_profile_loader_rejects_traversal(self):
        from core.profile import _load_profile
        prof = _load_profile("../obsidian")
        doc = _load_profile("documentary")
        assert prof == doc
