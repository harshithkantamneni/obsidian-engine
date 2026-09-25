"""Security tests for server/webhook_server.py — auth, setup wizard, dashboard key."""

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

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

    @pytest.mark.parametrize("bad", [
        "abc\nTRIGGER_KEY=x", "abc\rdef", "abc\x00def",
        "abc\x0bdef", "abc\x0cdef", "abc\x1cdef", "abc\x1ddef", "abc\x1edef",
        "abc\x85def", "abc\u2028def", "abc\u2029def",
        "${OTHER}", "abc${OTHER}", "abc$def", "abc def", "abc\tdef", "caf\u00e9",
        '"abc', "'abc", "abc\"", 'a\\nb', "a`b`", pytest.param("x" * 4097, id="4097-chars"),
        # at either end too, not just in the middle
        "\x0babc", "abc\x0c", "\x1cabc", "abc\x85", "\u2028abc", "abc\u2029",
    ])
    def test_rejects_values_outside_printable_ascii(self, client, with_key, setup_files, bad):
        env_path, _ = setup_files
        r = self._save(client, {"keys": {"FAL_KEY": bad}})
        assert r.status_code == 400
        assert not env_path.exists()

    @pytest.mark.parametrize("good", [
        "sk-proj-AbC_123", "https://abc.supabase.co", "eyJhbGciOi.eyJzdWIi.sig-_",
        "-1001234567890", "fal:key/with+chars=", pytest.param("x" * 4096, id="4096-chars"),
    ])
    def test_accepts_printable_ascii_values(self, client, with_key, setup_files, good):
        env_path, _ = setup_files
        r = self._save(client, {"keys": {"FAL_KEY": good}})
        assert r.status_code == 200, r.get_json()
        assert f"FAL_KEY={good}\n" in env_path.read_text()

    def test_value_charset_is_printable_ascii_minus_banned(self):
        allowed = {chr(c) for c in range(0x21, 0x7F)} - set("$\"'\\`")
        points = list(range(0x3000)) + [0xD800, 0xFEFF, 0xFF04, 0x1F600, 0xE0001, 0x10FFFF]
        for cp in points:
            ch = chr(cp)
            keys, _, _, errors = ws._validate_setup_payload({"keys": {"FAL_KEY": "a" + ch + "b"}})
            assert (not errors) == (ch in allowed), f"U+{cp:04X}"
            if not errors:
                assert keys == {"FAL_KEY": "a" + ch + "b"}

    @pytest.mark.parametrize("value", ["", "   ", "\t\r\n"])
    def test_empty_values_leave_env_untouched(self, client, with_key, setup_files, value):
        env_path, _ = setup_files
        r = self._save(client, {"keys": {"FAL_KEY": value}})
        assert r.status_code == 200, r.get_json()
        assert not env_path.exists()

    @pytest.mark.parametrize("value", ["#abc", "abc#x", "=x", "a=b", "export", "FAL_KEY=x",
                                       "TRIGGER_KEY=x", "a/b+c:d@e,f;g<h>i?j[k]l{m}n|o~p"])
    def test_saved_values_load_back_exactly(self, client, with_key, setup_files, value):
        env_path, _ = setup_files
        env_path.write_text("A=1\nB=two\n")
        r = self._save(client, {"keys": {"FAL_KEY": value}})
        assert r.status_code == 200, r.get_json()
        assert dotenv_values(env_path, interpolate=False) == {"A": "1", "B": "two", "FAL_KEY": value}

    @pytest.mark.parametrize("before", [
        "FAL_KEY=old1\nB=2\nFAL_KEY=old2\n",
        "FAL_KEY=old1\nB=2\nexport\tFAL_KEY=old2\n",
        "FAL_KEY=old1\nB=2\n'FAL_KEY'=old2\n",
    ], ids=["duplicate", "export-tab", "quoted-key"])
    def test_save_replaces_every_binding_of_the_key(self, client, with_key, setup_files, before):
        env_path, _ = setup_files
        env_path.write_text(before)
        r = self._save(client, {"keys": {"FAL_KEY": "new"}})
        assert r.status_code == 200, r.get_json()
        assert env_path.read_text() == "FAL_KEY=new\nB=2\n"
        assert dotenv_values(env_path, interpolate=False) == {"FAL_KEY": "new", "B": "2"}

    def test_save_replaces_a_multiline_value_as_one_binding(self, client, with_key, setup_files):
        env_path, _ = setup_files
        env_path.write_text('FAL_KEY="abc\nOTHER=x"\nB=2\n')
        r = self._save(client, {"keys": {"FAL_KEY": "new"}})
        assert r.status_code == 200, r.get_json()
        assert dotenv_values(env_path, interpolate=False) == {"FAL_KEY": "new", "B": "2"}

    def test_save_leaves_lines_inside_other_values_alone(self, client, with_key, setup_files):
        env_path, _ = setup_files
        env_path.write_text('N="first\nFAL_KEY=inside\nlast"\nB=2\n')
        r = self._save(client, {"keys": {"FAL_KEY": "new"}})
        assert r.status_code == 200, r.get_json()
        values = dotenv_values(env_path, interpolate=False)
        assert values == {"N": "first\nFAL_KEY=inside\nlast", "B": "2", "FAL_KEY": "new"}

    def test_concurrent_saves_are_serialized(self, with_key, setup_files, monkeypatch):
        env_path, _ = setup_files
        real_write = ws._atomic_write_text
        state = {"active": 0, "peak": 0}
        guard = threading.Lock()

        def slow_write(*args, **kwargs):
            with guard:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            try:
                return real_write(*args, **kwargs)
            finally:
                with guard:
                    state["active"] -= 1

        monkeypatch.setattr(ws, "_atomic_write_text", slow_write)
        statuses = []

        def save(value):
            with ws.app.test_client() as c:
                statuses.append(self._save(c, {"keys": {"FAL_KEY": value}}).status_code)

        threads = [threading.Thread(target=save, args=(f"v{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert statuses == [200] * 4
        assert state["peak"] == 1
        values = dotenv_values(env_path, interpolate=False)
        assert list(values) == ["FAL_KEY"] and values["FAL_KEY"] in {"v0", "v1", "v2", "v3"}

    def test_atomic_write_uses_a_unique_temp_file(self, client, with_key, setup_files):
        env_path, _ = setup_files
        (env_path.parent / (env_path.name + ".tmp")).mkdir()  # the old fixed temp name
        r = self._save(client, {"keys": {"FAL_KEY": "new"}})
        assert r.status_code == 200, r.get_json()
        assert dotenv_values(env_path, interpolate=False) == {"FAL_KEY": "new"}
        assert sorted(p.name for p in env_path.parent.iterdir()) == [".env", ".env.tmp", "obsidian.yaml"]

    @pytest.mark.parametrize("before, after", [
        ("A=1\n\nFAL_KEY=old\nB=2\n", "A=1\n\nFAL_KEY=new\nB=2\n"),
        ("A=1\n\n\n\tFAL_KEY=old\n", "A=1\n\n\nFAL_KEY=new\n"),
        ("FAL_KEY=a\n\n\nB=2\n\nFAL_KEY=b\n", "FAL_KEY=new\n\n\nB=2\n\n"),
    ], ids=["blank-before", "blank-and-indent", "blank-before-duplicate"])
    def test_save_keeps_blank_lines(self, client, with_key, setup_files, before, after):
        env_path, _ = setup_files
        env_path.write_text(before)
        r = self._save(client, {"keys": {"FAL_KEY": "new"}})
        assert r.status_code == 200, r.get_json()
        assert env_path.read_text() == after

    def test_resaving_a_wizard_created_env_keeps_its_header(self, client, with_key, setup_files):
        env_path, _ = setup_files
        assert self._save(client, {"keys": {"FAL_KEY": "one"}}).status_code == 200
        first = env_path.read_text()
        assert self._save(client, {"keys": {"FAL_KEY": "two"}}).status_code == 200
        assert env_path.read_text() == first.replace("FAL_KEY=one", "FAL_KEY=two")

    def test_env_is_never_readable_by_others(self, client, with_key, setup_files):
        env_path, yaml_path = setup_files
        yaml_path.chmod(0o644)
        r = self._save(client, {"keys": {"FAL_KEY": "new"}, "providers": {"llm": "openai"}})
        assert r.status_code == 200, r.get_json()
        assert env_path.stat().st_mode & 0o777 == 0o600
        assert yaml_path.stat().st_mode & 0o777 == 0o644
        for before, after in ((0o640, 0o640), (0o644, 0o640), (0o666, 0o660)):
            env_path.chmod(before)
            assert self._save(client, {"keys": {"FAL_KEY": "newer"}}).status_code == 200
            assert env_path.stat().st_mode & 0o777 == after

    def test_failed_write_leaves_no_temp_file_and_original_intact(self, client, with_key,
                                                                  setup_files, monkeypatch):
        env_path, _ = setup_files
        env_path.write_text("FAL_KEY=old\n")

        def broken_fdopen(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(ws.os, "fdopen", broken_fdopen)
        r = self._save(client, {"keys": {"FAL_KEY": "new"}})
        assert r.get_json()["success"] is False
        assert env_path.read_text() == "FAL_KEY=old\n"
        assert sorted(p.name for p in env_path.parent.iterdir()) == [".env", "obsidian.yaml"]

    def test_in_place_fallback_when_replace_fails(self, client, with_key, setup_files, monkeypatch):
        env_path, _ = setup_files
        env_path.write_text("FAL_KEY=old\n")
        env_path.chmod(0o644)
        inode = env_path.stat().st_ino

        def busy_replace(*args, **kwargs):
            raise OSError(16, "Device or resource busy")

        monkeypatch.setattr(ws.os, "replace", busy_replace)
        r = self._save(client, {"keys": {"FAL_KEY": "new"}})
        assert r.status_code == 200, r.get_json()
        assert env_path.read_text() == "FAL_KEY=new\n"
        assert env_path.stat().st_ino == inode
        assert env_path.stat().st_mode & 0o777 == 0o640
        assert sorted(p.name for p in env_path.parent.iterdir()) == [".env", "obsidian.yaml"]

    def test_save_keeps_other_lines_byte_for_byte(self, client, with_key, setup_files):
        env_path, _ = setup_files
        original = "# note\nNOTE=a\u2028b\nPEM=line1\x0bline2\nFAL_KEY=old\n"
        env_path.write_text(original)
        r = self._save(client, {"keys": {"FAL_KEY": "new"}})
        assert r.status_code == 200, r.get_json()
        assert env_path.read_text() == original.replace("FAL_KEY=old", "FAL_KEY=new")

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

    @pytest.mark.parametrize("line", ["export TRIGGER_KEY=kept-key", "TRIGGER_KEY=old\nTRIGGER_KEY=kept-key",
                                      "TRIGGER_KEY=kept-key  # note"])
    def test_first_run_adopts_existing_key_as_loaders_read_it(self, client, no_key, setup_files, line):
        env_path, _ = setup_files
        env_path.write_text(line + "\n")
        r = client.post("/api/setup/save", json={"keys": {"FAL_KEY": "fal_abc"}}, environ_base=LOCAL)
        assert r.status_code == 401  # the key exists now, so it must be presented
        assert ws.TRIGGER_KEY == "kept-key"
        assert "FAL_KEY" not in dotenv_values(env_path, interpolate=False)
        r = client.post("/api/setup/save", json={"keys": {"FAL_KEY": "fal_abc"}},
                        headers={"X-Trigger-Key": "kept-key"}, environ_base=LOCAL)
        assert r.status_code == 200, r.get_json()
        assert "trigger_key_generated" not in r.get_json()
        values = dotenv_values(env_path, interpolate=False)
        assert values["TRIGGER_KEY"] == "kept-key" and values["FAL_KEY"] == "fal_abc"

    def test_first_run_request_needs_the_key_once_one_is_set(self, client, no_key,
                                                             setup_files, monkeypatch):
        """A request admitted as first run, whose save runs after another save
        set a key, must present that key."""
        env_path, _ = setup_files
        real_validate = ws._validate_setup_payload

        def key_set_meanwhile(data):
            monkeypatch.setattr(ws, "TRIGGER_KEY", KEY)
            return real_validate(data)

        monkeypatch.setattr(ws, "_validate_setup_payload", key_set_meanwhile)
        r = client.post("/api/setup/save", json={"keys": {"FAL_KEY": "fal_abc"}}, environ_base=LOCAL)
        assert r.status_code == 401
        assert not env_path.exists()

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


# ── DNS rebinding / cross-site POST ──────────────────────────────────────────

EVIL_BASE = "http://evil.example:8080"


class TestRebindingAndCrossSite:
    def test_first_run_save_rejected_for_foreign_host(self, client, no_key, setup_files):
        env_path, _ = setup_files
        r = client.post("/api/setup/save", json={"keys": {"FAL_KEY": "x"}},
                        environ_base=LOCAL, base_url=EVIL_BASE)
        assert r.status_code == 503
        assert not env_path.exists()
        assert ws.TRIGGER_KEY == ""

    def test_dashboard_key_not_injected_for_foreign_host(self, client, with_key, monkeypatch):
        monkeypatch.setattr(ws, "DASHBOARD_PASSWORD", "")
        r = client.get("/", environ_base=LOCAL, base_url=EVIL_BASE)
        assert KEY not in r.get_data(as_text=True)

    @pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost", "[::1]:8080"])
    def test_loopback_hosts_still_trusted(self, client, with_key, monkeypatch, host):
        monkeypatch.setattr(ws, "DASHBOARD_PASSWORD", "")
        r = client.get("/", environ_base=LOCAL, base_url=f"http://{host}")
        assert KEY in r.get_data(as_text=True)

    def test_text_plain_post_rejected(self, client, no_key, setup_files):
        env_path, _ = setup_files
        r = client.post("/api/setup/save", data="{}", content_type="text/plain",
                        environ_base=LOCAL)
        assert r.status_code == 403
        assert not env_path.exists()

    def test_cross_origin_post_rejected(self, client, with_key, setup_files):
        r = client.post("/api/setup/save", json={"keys": {"FAL_KEY": "x"}},
                        headers={"X-Trigger-Key": KEY, "Origin": "https://evil.example"},
                        environ_base=LOCAL)
        assert r.status_code == 403

    def test_same_origin_post_allowed(self, client, with_key, setup_files):
        r = client.post("/api/setup/save", json={"keys": {"FAL_KEY": "x"}},
                        headers={"X-Trigger-Key": KEY, "Origin": "http://localhost"},
                        environ_base=LOCAL)
        assert r.status_code == 200, r.get_json()


# ── Setup status: keys are required only for configured providers ────────────

_WIZARD_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
                "FAL_KEY", "PEXELS_API_KEY", "EPIDEMIC_SOUND_API_KEY")
_OLLAMA_YAML = ("providers:\n  llm:\n    name: openai\n    options:\n"
                "      base_url: http://localhost:11434/v1\n")


class TestSetupRequiredKeys:
    def _status(self, client, yaml_path, monkeypatch, providers_yaml, env=None):
        yaml_path.write_text(providers_yaml)
        monkeypatch.delenv("OBSIDIAN_CONFIG", raising=False)
        for k in _WIZARD_KEYS:
            monkeypatch.delenv(k, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        r = client.get("/api/setup/status", environ_base=LOCAL)
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        return data, {k["key"]: k for k in data["keys"]}

    @pytest.mark.parametrize("providers_yaml, required", [
        # nothing configured: the built-in defaults apply
        ("profile: documentary\n",
         {"ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        (SAMPLE_YAML,
         {"ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        ("providers:\n  llm:\n    name: openai\n",
         {"OPENAI_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        (_OLLAMA_YAML,
         {"ELEVENLABS_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        ("providers:\n  llm:\n    name: my_pkg.llm.MyLLM\n  tts:\n    name: openai\n",
         {"OPENAI_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        ("providers:\n  tts:\n    name: openai\n    options: {api_key_env: MY_TTS_KEY}\n",
         {"ANTHROPIC_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        ("providers:\n  tts:\n    name: epidemic_sound\n  music:\n    name: auto\n",
         {"ANTHROPIC_API_KEY", "EPIDEMIC_SOUND_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        ("providers:\n  llm:\n    provider: openai\n",
         {"OPENAI_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        # base_url plus an explicit key env: a hosted OpenAI-compatible server
        ("providers:\n  llm:\n    name: openai\n    options:\n"
         "      base_url: https://example.test/v1\n      api_key_env: OPENAI_API_KEY\n",
         {"OPENAI_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        # the base_url exemption is for the openai LLM only
        ("providers:\n  tts:\n    name: openai\n    options: {base_url: http://x/v1}\n",
         {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
        # options without a provider name are ignored, as in the registry
        ("providers:\n  tts:\n    options: {api_key_env: MY_EL}\n",
         {"ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "PEXELS_API_KEY"}),
    ], ids=["no-providers", "sample", "openai", "ollama", "custom-llm-openai-tts",
            "tts-api_key_env", "epidemic-tts", "provider-alias", "base_url-with-key-env",
            "tts-base_url", "options-without-name"])
    def test_required_follows_configured_providers(self, client, no_key, setup_files,
                                                   monkeypatch, providers_yaml, required):
        _, yaml_path = setup_files
        data, keys = self._status(client, yaml_path, monkeypatch, providers_yaml)
        assert {k for k, v in keys.items() if v["required"]} == required
        assert data["setup_complete"] is False

    def test_complete_without_anthropic_key_when_llm_is_not_anthropic(
            self, client, no_key, setup_files, monkeypatch):
        _, yaml_path = setup_files
        data, keys = self._status(client, yaml_path, monkeypatch, _OLLAMA_YAML, env={
            "ELEVENLABS_API_KEY": "el", "FAL_KEY": "fal_x", "PEXELS_API_KEY": "px"})
        assert keys["ANTHROPIC_API_KEY"]["configured"] is False
        assert data["setup_complete"] is True

    def test_default_llm_still_needs_anthropic_key(self, client, no_key, setup_files, monkeypatch):
        _, yaml_path = setup_files
        env = {"ELEVENLABS_API_KEY": "el", "FAL_KEY": "fal_x", "PEXELS_API_KEY": "px"}
        data, _ = self._status(client, yaml_path, monkeypatch, SAMPLE_YAML, env=env)
        assert data["setup_complete"] is False
        env["ANTHROPIC_API_KEY"] = "sk-ant"
        data, _ = self._status(client, yaml_path, monkeypatch, SAMPLE_YAML, env=env)
        assert data["setup_complete"] is True

    def test_key_entries_keep_their_shape(self, client, no_key, setup_files, monkeypatch):
        _, yaml_path = setup_files
        data, _ = self._status(client, yaml_path, monkeypatch, SAMPLE_YAML)
        assert {"keys", "profile", "available_profiles", "providers",
                "available_providers", "setup_complete",
                "trigger_key_configured"} <= set(data)
        for k in data["keys"]:
            assert set(k) == {"key", "label", "required", "help", "category", "configured"}
            assert isinstance(k["required"], bool)

    def test_obsidian_config_env_is_honored(self, client, no_key, setup_files,
                                            monkeypatch, tmp_path):
        _, yaml_path = setup_files
        alt = tmp_path / "alt.yaml"
        alt.write_text(_OLLAMA_YAML)
        data, keys = self._status(client, yaml_path, monkeypatch, SAMPLE_YAML)
        assert keys["ANTHROPIC_API_KEY"]["required"] is True
        monkeypatch.setenv("OBSIDIAN_CONFIG", str(alt))
        data = client.get("/api/setup/status", environ_base=LOCAL).get_json()
        keys = {k["key"]: k for k in data["keys"]}
        assert data["providers"]["llm"] == "openai"
        assert keys["ANTHROPIC_API_KEY"]["required"] is False

    def test_status_follows_saved_providers_without_restart(
            self, client, with_key, setup_files, monkeypatch):
        monkeypatch.delenv("OBSIDIAN_CONFIG", raising=False)
        for k in _WIZARD_KEYS:
            monkeypatch.delenv(k, raising=False)
        hdr = {"X-Trigger-Key": KEY}
        before = client.get("/api/setup/status", headers=hdr, environ_base=LOCAL).get_json()
        assert before["providers"]["llm"] == "anthropic"
        r = client.post("/api/setup/save", json={"providers": {"llm": "openai"}},
                        headers=hdr, environ_base=LOCAL)
        assert r.status_code == 200, r.get_json()
        after = client.get("/api/setup/status", headers=hdr, environ_base=LOCAL).get_json()
        required = {k["key"] for k in after["keys"] if k["required"]}
        assert after["providers"]["llm"] == "openai"
        assert "OPENAI_API_KEY" in required
        assert "ANTHROPIC_API_KEY" not in required

    def test_saves_openai_key(self, client, with_key, setup_files):
        env_path, _ = setup_files
        r = client.post("/api/setup/save", json={"keys": {"OPENAI_API_KEY": "sk-test"}},
                        headers={"X-Trigger-Key": KEY}, environ_base=REMOTE)
        assert r.status_code == 200, r.get_json()
        assert "OPENAI_API_KEY=sk-test\n" in env_path.read_text()

    def test_validate_openai_key(self, client, no_key, monkeypatch):
        import requests

        seen = {}

        class _Resp:
            status_code = 200

        def fake_get(url, headers=None, timeout=None, **kw):
            seen["url"], seen["auth"] = url, (headers or {}).get("Authorization")
            return _Resp()

        monkeypatch.setattr(requests, "get", fake_get)
        r = client.post("/api/setup/validate",
                        json={"key": "OPENAI_API_KEY", "value": "sk-x"}, environ_base=LOCAL)
        assert r.get_json()["valid"] is True
        assert seen == {"url": "https://api.openai.com/v1/models", "auth": "Bearer sk-x"}
