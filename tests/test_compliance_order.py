"""Wave 1 ordering: compliance fixes must reach Scene Breakdown (stage 7).

Scene-aware audio narrates stage 7's per-scene ``narration`` field, so the
compliance ``safe_script`` has to be applied to ctx.script BEFORE stage 7 runs.
Also covers the pronunciation respellings being applied to the stage 8 scene
input (audio only) in phase_prod.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.context import PipelineContext
from pipeline.runner import StageRunner

ORIGINAL = "The original script with flagged wording. " * 40
SAFE = "word " * 501  # _run_compliance requires >500 words to accept a safe_script


def _ctx(tmp_path: Path, **overrides) -> PipelineContext:
    script = {"full_script": ORIGINAL, "topic": "Test Topic"}
    defaults = dict(
        topic="Test Topic",
        slug="test-topic",
        ts="20260924",
        state_path=tmp_path / "state.json",
        state={"completed_stages": [1, 2, 3, 4, 5], "stage_4": script},
        script=script,
        verification={"ok": True},
        angle={"angle": "x"},
    )
    defaults.update(overrides)
    ctx = PipelineContext(**defaults)
    seen = {}

    def _scenes(script_arg, verification):
        seen["stage7_full_script"] = script_arg["full_script"]
        return {"scenes": [{"narration": "a"}, {"narration": "b"}, {"narration": "c"}]}

    def _seo(script_arg, verification, angle):
        seen["stage6_full_script"] = script_arg["full_script"]
        return {"recommended_title": "Title", "title_variants": []}

    a06 = MagicMock()
    a06.run = MagicMock(side_effect=_seo)
    a07 = MagicMock()
    a07.run = MagicMock(side_effect=_scenes)
    ctx.agents = {"a06": a06, "a07": a07}
    ctx._seen = seen  # test-only side channel
    return ctx


def _run_wave1(ctx, compliance_return=None, compliance_side_effect=None, tmp_path=None):
    from pipeline import phase_prod
    runner = StageRunner(ctx)
    mock_cc = MagicMock(return_value=compliance_return, side_effect=compliance_side_effect)
    with patch("core.compliance_checker.run", mock_cc), \
         patch.object(phase_prod, "OUTPUT_DIR", tmp_path):
        phase_prod._run_wave1(ctx, runner)
    return mock_cc


class TestComplianceBeforeScenes:

    def test_stage7_receives_safe_script(self, tmp_path):
        ctx = _ctx(tmp_path)
        _run_wave1(ctx, compliance_return={
            "risk_level": "red",
            "flags": [{"severity": "high", "category": "violence", "text_excerpt": "x"}],
            "safe_script": SAFE,
        }, tmp_path=tmp_path)

        assert ctx._seen["stage7_full_script"] == SAFE
        assert ctx._seen["stage6_full_script"] == SAFE
        assert ctx.script["full_script"] == SAFE

    def test_patched_script_persisted_for_resume(self, tmp_path):
        ctx = _ctx(tmp_path)
        _run_wave1(ctx, compliance_return={
            "risk_level": "red", "flags": [{"category": "c"}], "safe_script": SAFE,
        }, tmp_path=tmp_path)

        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["stage_4"]["full_script"] == SAFE
        assert saved["compliance"]["safe_script_applied"] is True
        assert saved["compliance"]["risk_level"] == "red"
        # Stages 6 and 7 completed after the patch
        assert 6 in saved["completed_stages"] and 7 in saved["completed_stages"]

    def test_compliance_runs_before_stage7(self, tmp_path):
        ctx = _ctx(tmp_path)
        order = []
        ctx.agents["a07"].run.side_effect = lambda s, v: order.append("stage7") or {"scenes": []}

        def _cc(script, topic):
            order.append("compliance")
            return {"risk_level": "green", "flags": []}

        _run_wave1(ctx, compliance_side_effect=_cc, tmp_path=tmp_path)
        assert order == ["compliance", "stage7"]

    def test_green_leaves_script_unchanged(self, tmp_path):
        ctx = _ctx(tmp_path)
        _run_wave1(ctx, compliance_return={"risk_level": "green", "flags": []}, tmp_path=tmp_path)
        assert ctx._seen["stage7_full_script"] == ORIGINAL
        assert ctx.state["compliance"] == {"risk_level": "green", "flag_count": 0}

    def test_red_without_safe_script_raises_before_stage7(self, tmp_path):
        ctx = _ctx(tmp_path)
        with pytest.raises(RuntimeError, match="Compliance gate FAILED"):
            _run_wave1(ctx, compliance_return={
                "risk_level": "red",
                "flags": [{"severity": "high", "category": "drugs", "text_excerpt": "bad"}],
                "safe_script": "",
            }, tmp_path=tmp_path)
        ctx.agents["a07"].run.assert_not_called()
        ctx.agents["a06"].run.assert_not_called()

    def test_red_with_too_short_safe_script_raises(self, tmp_path):
        ctx = _ctx(tmp_path)
        with pytest.raises(RuntimeError, match="Compliance gate FAILED"):
            _run_wave1(ctx, compliance_return={
                "risk_level": "red", "flags": [{"category": "c"}], "safe_script": "too short",
            }, tmp_path=tmp_path)
        ctx.agents["a07"].run.assert_not_called()

    def test_compliance_error_is_non_fatal(self, tmp_path):
        ctx = _ctx(tmp_path)
        _run_wave1(ctx, compliance_side_effect=RuntimeError("API down"), tmp_path=tmp_path)
        assert ctx._seen["stage7_full_script"] == ORIGINAL


# ═══════════════════════════════════════════════════════════════════════════════
# Pronunciation respellings reach scene-aware audio (not captions)
# ═══════════════════════════════════════════════════════════════════════════════

PMAP = {"Chanakya": "Chaa-nuh-kya", "Maurya": "Mowr-ya"}


class TestAudioRespelling:

    def test_undo_respellings_restores_original(self):
        from pipeline.phase_prod import _undo_respellings
        text = "Chaa-nuh-kya advised Chandragupta Mowr-ya. Chanakya again."
        out = _undo_respellings(text, PMAP)
        assert out == "Chanakya advised Chandragupta Maurya. Chanakya again."
        assert len(out.split()) == len(text.split())

    def test_scene_narration_respelled_first_occurrence_only(self):
        from pipeline.phase_prod import _respell_scenes_for_audio
        scenes = {"scenes": [
            {"narration": "Nothing here."},
            {"narration": "Chanakya speaks. Chanakya again."},
            {"narration": "Then chanakya leaves with Maurya."},
        ], "visual_bible": {"a": 1}}
        tts = {"full_script": "Chaa-nuh-kya speaks. Chanakya again. Chanakya leaves with Mowr-ya."}
        out = _respell_scenes_for_audio(scenes, tts, PMAP)

        assert out["scenes"][1]["narration"] == "Chaa-nuh-kya speaks. Chanakya again."
        assert out["scenes"][2]["narration"] == "Then chanakya leaves with Mowr-ya."
        assert out["visual_bible"] == {"a": 1}
        # Input (used for captions / footage / manifest) is untouched
        assert scenes["scenes"][1]["narration"] == "Chanakya speaks. Chanakya again."
        assert scenes["scenes"][2]["narration"] == "Then chanakya leaves with Maurya."

    def test_only_terms_the_agent_respelled(self):
        from pipeline.phase_prod import _respell_scenes_for_audio
        scenes = {"scenes": [{"narration": "Chanakya and Maurya."}]}
        tts = {"full_script": "Chanakya and Mowr-ya."}  # agent only respelled Maurya
        out = _respell_scenes_for_audio(scenes, tts, PMAP)
        assert out["scenes"][0]["narration"] == "Chanakya and Mowr-ya."

    def test_no_map_returns_input(self):
        from pipeline.phase_prod import _respell_scenes_for_audio
        scenes = {"scenes": [{"narration": "Chanakya"}]}
        assert _respell_scenes_for_audio(scenes, {"full_script": "Chaa-nuh-kya"}, {}) is scenes

    def test_wave2_display_script_has_original_spellings(self, tmp_path):
        from pipeline.phase_prod import _run_wave2
        agent = MagicMock()
        agent.PRONUNCIATION_MAP = PMAP
        agent.run = MagicMock(return_value={
            "full_script": "Chaa-nuh-kya advised the Mowr-ya king.",
            "changes_made": [],
        })
        ctx = PipelineContext(state_path=tmp_path / "s.json", state={"completed_stages": []},
                              script={"full_script": "Chanakya advised the Maurya king."},
                              agents={"a_tts_format": agent})
        runner = StageRunner(ctx)
        _run_wave2(ctx, runner)
        assert ctx.tts_script["full_script"] == "Chaa-nuh-kya advised the Mowr-ya king."
        assert ctx.display_script["full_script"] == "Chanakya advised the Maurya king."

    def test_stage8_gets_respelled_scenes(self, tmp_path):
        from pipeline import phase_prod
        agent = MagicMock()
        agent.PRONUNCIATION_MAP = PMAP
        scenes = {"scenes": [{"narration": "Chanakya waits."}, {"narration": "b"}, {"narration": "c"}]}
        ctx = PipelineContext(state_path=tmp_path / "s.json", state={"completed_stages": []},
                              agents={"a_tts_format": agent, "a09": MagicMock()},
                              scenes_data=scenes,
                              tts_script={"full_script": "Chaa-nuh-kya waits. b c"},
                              display_script={"full_script": "Chanakya waits. b c"})
        runner = StageRunner(ctx)
        captured = {}

        def _fake_run_stage(num, name, fn, *args):
            if num == 8:
                captured["args"] = args
                return {"total_duration_seconds": 10}
            return {"scenes": []}

        runner.run_stage = MagicMock(side_effect=_fake_run_stage)
        with patch.object(phase_prod, "check_audio", return_value=[]):
            phase_prod._run_wave3(ctx, runner)
        tts_arg, scene_arg, display_arg = captured["args"]
        assert scene_arg["scenes"][0]["narration"] == "Chaa-nuh-kya waits."
        assert display_arg["full_script"] == "Chanakya waits. b c"
        assert ctx.scenes_data["scenes"][0]["narration"] == "Chanakya waits."
