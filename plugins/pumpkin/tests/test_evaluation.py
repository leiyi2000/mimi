from dataclasses import replace

from evaluate import EvaluationCases, EvaluationRunner


def test_evaluation_does_not_inherit_production_services_or_qq():
    env = EvaluationRunner.allowed_model_env({
        "PUMPKIN_MODEL": "fixture-model",
        "PUMPKIN_CHAT_API_KEY": "synthetic-key",
        "NAPCAT_HOST": "production",
        "PUMPKIN_DATABASE_URL": "production",
        "PUMPKIN_SEARCH_API_KEY": "production",
        "PUMPKIN_EVAL_JUDGE_API_KEY": "judge-only",
    })
    assert env == {
        "PUMPKIN_MODEL": "fixture-model",
        "PUMPKIN_CHAT_API_KEY": "synthetic-key",
    }


async def test_contract_replay_runs_real_plugin_through_fake_napcat():
    runner = EvaluationRunner(
        assistant_name="fixture-assistant", timeout=8, quiet_seconds=0.2,
    )
    cases = EvaluationCases.short_regressions()
    report = await runner.run([cases[0], cases[2], cases[-1]])
    assert report["mode"] == "contract_replay"
    assert report["capability_evidence"] is False
    assert [item["verdict"] for item in report["results"]] == ["passed"] * 3
    assert [item["output_messages"] for item in report["results"]] == [1, 2, 1]
    assert all("outputs" not in item for item in report["results"])


async def test_casual_regression_detects_visible_long_output():
    runner = EvaluationRunner(
        assistant_name="fixture-assistant", timeout=8, quiet_seconds=0.2,
    )
    case = replace(
        EvaluationCases.short_regressions()[0],
        fixture_replies=("x" * 60,),
        max_reply_chars=20,
    )
    result = await runner.run_case(case)
    assert result["verdict"] == "failed"
    assert "threshold" in result["reason"]
