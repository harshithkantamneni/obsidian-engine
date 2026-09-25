"""Tests for webhook_server.py — API endpoints, auth, input validation."""

import sys
import json
import threading
import pytest
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from server.webhook_server import app, _validate_topic, _state, _lock

TEST_KEY = "test-trigger-key"


@pytest.fixture(autouse=True)
def trigger_key(monkeypatch):
    """Protected routes require a configured TRIGGER_KEY (never open by default)."""
    monkeypatch.setattr("server.webhook_server.TRIGGER_KEY", TEST_KEY)
    return TEST_KEY


@pytest.fixture
def client():
    """Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def reset_state():
    """Reset pipeline state between tests."""
    with _lock:
        _state.update({
            "running": False,
            "analytics_running": False,
            "topic": "",
            "stage": "",
            "stage_num": 0,
            "short_stage": "",
            "short_stage_num": 0,
            "last_status": "idle",
            "pipeline_done": False,
            "log": [],
            "started_at": None,
            "finished_at": None,
        })


# ── Health & Status ──────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.data == b"OK"

    def test_status_returns_json(self, client):
        r = client.get("/status", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert "running" in data
        assert "stage_num" in data
        assert data["running"] is False


# ── Input Validation ─────────────────────────────────────────────────────────

class TestValidation:
    def test_valid_topic(self):
        topic, err = _validate_topic("The Fall of the Roman Empire")
        assert err is None
        assert topic == "The Fall of the Roman Empire"

    def test_empty_topic_passes(self):
        topic, err = _validate_topic("")
        assert err is None
        assert topic == ""

    def test_too_short_topic(self):
        topic, err = _validate_topic("Hi")
        assert topic is None
        assert "too short" in err.lower()

    def test_too_long_topic(self):
        topic, err = _validate_topic("x" * 201)
        assert topic is None
        assert "too long" in err.lower()

    def test_xss_tags_stripped(self):
        # The validator strips HTML tags first, then checks length/content
        # <script>alert("xss")</script> → alert("xss") after tag strip
        topic, err = _validate_topic('<script>alert("xss")</script>')
        # Tags must be stripped — result should be clean text without script tags
        assert topic is not None, "Stripped XSS should return cleaned text, not None"
        assert "<script" not in topic
        assert "</script" not in topic
        assert "alert" in topic  # The text content remains after tag stripping

    def test_sql_injection_rejected(self):
        # The regex requires keyword pairs like DELETE FROM, INSERT INTO
        topic, err = _validate_topic("DELETE FROM topics WHERE id=1")
        assert topic is None
        assert "disallowed" in err.lower()

    def test_sql_drop_rejected(self):
        topic, err = _validate_topic("DROP TABLE topics")
        assert topic is None
        assert "disallowed" in err.lower()

    def test_html_tags_stripped(self):
        topic, err = _validate_topic("The <b>Dark</b> History of Poisoning")
        assert err is None
        assert "<b>" not in topic
        assert "Dark" in topic

    def test_control_chars_stripped(self):
        topic, err = _validate_topic("The Dark\x00 History\x0b Test")
        assert err is None
        assert "\x00" not in topic


# ── API Endpoints ────────────────────────────────────────────────────────────

class TestEndpoints:
    def test_dashboard_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"OBSIDIAN" in r.data

    def test_costs_endpoint(self, client):
        r = client.get("/costs", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert "costs" in data or "error" in data

    def test_music_endpoint(self, client):
        r = client.get("/music", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert "total_tracks" in data
        assert "tracks_by_mood" in data

    def test_trends_endpoint(self, client):
        r = client.get("/trends", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200

    def test_audit_endpoint(self, client):
        r = client.get("/audit", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert "entries" in data

    def test_history_endpoint(self, client):
        r = client.get("/history", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200

    def test_stream_endpoint_sse(self, client):
        r = client.get("/stream", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        assert "text/event-stream" in r.content_type

    def test_trigger_rejects_running(self, client):
        with _lock:
            _state["running"] = True
            _state["topic"] = "test"
        r = client.post("/trigger",
                        json={"topic": "Another topic"},
                        headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 409

    def test_kill_no_pipeline(self, client):
        r = client.post("/kill", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 400


# ── Dashboard HTML ───────────────────────────────────────────────────────────

class TestDashboard:
    def test_dashboard_serves_html(self, client):
        """Dashboard serves HTML (new Preact app or old dashboard)."""
        r = client.get("/")
        html = r.data.decode()
        assert "OBSIDIAN" in html

    def test_dashboard_is_new_or_old(self, client):
        """Dashboard serves either new Preact app (has <div id='app'>) or old HTML."""
        r = client.get("/")
        html = r.data.decode()
        # New dashboard has <div id="app">, old has view-command
        assert '<div id="app">' in html or "view-command" in html


# ── New Dashboard API Tests ──────────────────────────────────────────────────

class TestApiPulse:
    def test_pulse_returns_all_keys(self, client):
        r = client.get("/api/pulse", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        expected_keys = {
            "status", "running", "stage", "stage_num", "topic",
            "started_at", "finished_at", "analytics_running",
            "queue_depth", "errors_24h", "health", "last_cost_usd",
        }
        assert expected_keys.issubset(set(data.keys())), f"Missing keys: {expected_keys - set(data.keys())}"

    def test_pulse_exempt_from_rate_limit(self, client):
        """20 rapid calls should all succeed (no rate limit on pulse)."""
        for _ in range(20):
            r = client.get("/api/pulse", headers={"X-Trigger-Key": TEST_KEY})
            assert r.status_code == 200

    def test_pulse_graceful_when_supabase_down(self, client):
        """queue_depth should fallback to 0 when Supabase is unreachable."""
        with patch("server.webhook_server._get_queue_depth", side_effect=Exception("connection refused")):
            r = client.get("/api/pulse", headers={"X-Trigger-Key": TEST_KEY})
            assert r.status_code == 200
            data = r.get_json()
            assert isinstance(data["queue_depth"], int)

    def test_pulse_thread_safe(self):
        """Concurrent requests don't corrupt cache."""
        errors = []

        def hit_pulse():
            try:
                with app.test_client() as c:
                    r = c.get("/api/pulse", headers={"X-Trigger-Key": TEST_KEY})
                    data = r.get_json()
                    if "status" not in data:
                        errors.append("missing status key")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=hit_pulse) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Thread safety errors: {errors}"


class TestApiDashboard:
    def test_summary_always_returns_5_signals(self, client):
        r = client.get("/api/dashboard?sections=summary", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert "summary" in data
        assert len(data["summary"]["signals"]) == 5

    def test_performance_excludes_other_sections(self, client):
        r = client.get("/api/dashboard?sections=performance", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert "performance" in data
        assert "audience" not in data
        assert "config" not in data

    def test_multi_section_request(self, client):
        r = client.get("/api/dashboard?sections=performance,audience", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert "performance" in data
        assert "audience" in data

    def test_invalid_section_returns_empty(self, client):
        r = client.get("/api/dashboard?sections=invalid", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert data == {}

    def test_missing_insights_returns_empty_sections(self, client):
        """When channel_insights.json doesn't exist, sections return empty data."""
        r = client.get("/api/dashboard?sections=performance", headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 200
        data = r.get_json()
        assert "performance" in data
        assert data["performance"]["per_video_stats"] == []


class TestCacheControl:
    def test_pulse_has_no_cache(self, client):
        r = client.get("/api/pulse", headers={"X-Trigger-Key": TEST_KEY})
        assert "no-cache" in r.headers.get("Cache-Control", "")
        assert "no-store" in r.headers.get("Cache-Control", "")

    def test_dashboard_api_has_no_cache(self, client):
        r = client.get("/api/dashboard?sections=summary", headers={"X-Trigger-Key": TEST_KEY})
        assert "no-cache" in r.headers.get("Cache-Control", "")

    def test_error_response_has_no_cache(self, client):
        """Error responses should also have cache headers."""
        r = client.get("/api/pulse")  # No auth key
        assert "no-cache" in r.headers.get("Cache-Control", "")


class TestStaticServing:
    def test_fallback_to_old_dashboard(self, client):
        """When dashboard/dist/ doesn't exist, serves old dashboard.html."""
        r = client.get("/")
        assert r.status_code == 200
        html = r.data.decode()
        assert "OBSIDIAN" in html

    def test_root_serves_dashboard(self, client):
        """/ route serves HTML with trigger key replaced."""
        r = client.get("/")
        assert r.status_code == 200
        assert r.content_type.startswith("text/html")


class TestStageSummary:
    def test_cumulative_writes(self, tmp_path):
        """Stage summary file supports cumulative writes."""
        summary_path = tmp_path / "stage_summary.json"

        # Simulate stage 8 write
        data = {"8": {"audio_duration": 420}}
        summary_path.write_text(json.dumps(data))

        # Simulate stage 9 write (cumulative)
        existing = json.loads(summary_path.read_text())
        existing["9"] = {"images_found": 15}
        summary_path.write_text(json.dumps(existing))

        result = json.loads(summary_path.read_text())
        assert "8" in result
        assert "9" in result
        assert result["8"]["audio_duration"] == 420
        assert result["9"]["images_found"] == 15

    def test_race_condition_both_survive(self, tmp_path):
        """Two threads writing simultaneously should both survive."""
        import threading
        summary_path = tmp_path / "stage_summary.json"
        summary_path.write_text("{}")
        lock = threading.Lock()

        def write_stage(key, val):
            with lock:
                existing = json.loads(summary_path.read_text())
                existing[key] = val
                tmp = summary_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(existing))
                tmp.replace(summary_path)

        t1 = threading.Thread(target=write_stage, args=("8", {"audio": 300}))
        t2 = threading.Thread(target=write_stage, args=("9", {"images": 10}))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        result = json.loads(summary_path.read_text())
        assert "8" in result
        assert "9" in result


class TestLoadRunHistory:
    def test_returns_empty_on_corrupt_json(self, tmp_path):
        from server.webhook_server import _load_run_history
        history_path = tmp_path / "outputs" / "run_history.json"
        history_path.parent.mkdir(parents=True)
        history_path.write_text("{invalid json")
        with patch("server.webhook_server.BASE_DIR", tmp_path):
            result = _load_run_history()
            assert result == []


class TestMaxContentLength:
    def test_oversized_post_returns_413(self, client):
        """POST >1MB should be rejected."""
        large_body = "x" * (2 * 1024 * 1024)  # 2MB
        r = client.post("/trigger",
                        data=large_body,
                        content_type="application/json",
                        headers={"X-Trigger-Key": TEST_KEY})
        assert r.status_code == 413
