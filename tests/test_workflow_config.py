"""
Validates GitHub Actions workflow YAML configuration for Strategy C.

Catches secret-name mistakes that actionlint cannot (it has no visibility
into which secrets are defined in the repo). Specific rules:
  - Supabase env vars must map to secrets.SUPABASE_URL_C / SUPABASE_KEY_C
  - TENANT_ID must map to secrets.TENANT_ID_C in session run steps
  - No step should reference bare secrets.SUPABASE_URL or SUPABASE_KEY
"""
import pytest
import yaml
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).parent.parent / ".github" / "workflows"
SESSION_WORKFLOWS = {"premarket.yml", "intraday.yml", "eod.yml"}


def _load_workflows() -> dict[str, dict]:
    return {
        f.name: yaml.safe_load(f.read_text())
        for f in WORKFLOWS_DIR.glob("*.yml")
    }


def _step_envs(workflow: dict) -> list[tuple[str, str, str]]:
    """Yield (step_name, env_key, env_value) for every step env entry."""
    results = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            for key, value in (step.get("env") or {}).items():
                name = step.get("name") or step.get("uses") or "unnamed"
                results.append((name, key, str(value or "")))
    return results


def _run_steps(workflow: dict) -> list[tuple[str, dict]]:
    """Yield (step_name, env_dict) for steps that execute Python session scripts."""
    results = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            if "sessions/" in run or "premarket.py" in run or "intraday.py" in run or "eod.py" in run:
                name = step.get("name") or "unnamed"
                results.append((name, step.get("env") or {}))
    return results


class TestSupabaseSecretNames:

    def test_no_bare_supabase_url_secret(self):
        """SUPABASE_URL must map to secrets.SUPABASE_URL_C, never secrets.SUPABASE_URL."""
        for wf_name, wf in _load_workflows().items():
            for step_name, key, value in _step_envs(wf):
                if key == "SUPABASE_URL":
                    assert "secrets.SUPABASE_URL_C" in value, (
                        f"{wf_name} / '{step_name}': SUPABASE_URL maps to '{value}' "
                        f"— must use secrets.SUPABASE_URL_C not secrets.SUPABASE_URL"
                    )

    def test_no_bare_supabase_key_secret(self):
        """SUPABASE_KEY must map to secrets.SUPABASE_KEY_C, never secrets.SUPABASE_KEY."""
        for wf_name, wf in _load_workflows().items():
            for step_name, key, value in _step_envs(wf):
                if key == "SUPABASE_KEY":
                    assert "secrets.SUPABASE_KEY_C" in value, (
                        f"{wf_name} / '{step_name}': SUPABASE_KEY maps to '{value}' "
                        f"— must use secrets.SUPABASE_KEY_C not secrets.SUPABASE_KEY"
                    )


class TestTenantIdInSessionWorkflows:

    def test_tenant_id_present_in_session_run_steps(self):
        """premarket, intraday, and eod run steps must have TENANT_ID -> secrets.TENANT_ID_C."""
        for wf_name in SESSION_WORKFLOWS:
            wf_path = WORKFLOWS_DIR / wf_name
            if not wf_path.exists():
                continue
            wf = yaml.safe_load(wf_path.read_text())
            run_steps = _run_steps(wf)
            assert run_steps, f"{wf_name}: no session run step found"
            for step_name, env in run_steps:
                assert "TENANT_ID" in env, (
                    f"{wf_name} / '{step_name}': missing TENANT_ID env var"
                )
                assert "secrets.TENANT_ID_C" in str(env["TENANT_ID"]), (
                    f"{wf_name} / '{step_name}': TENANT_ID maps to '{env['TENANT_ID']}' "
                    f"— must use secrets.TENANT_ID_C"
                )


class TestNoWrongStrategySecrets:

    def test_no_strategy_a_or_b_supabase_secrets(self):
        """C workflows must not accidentally reference A/B Supabase secrets (no suffix = wrong)."""
        forbidden = ["secrets.SUPABASE_URL }}", "secrets.SUPABASE_KEY }}"]
        for wf_name, wf in _load_workflows().items():
            for step_name, key, value in _step_envs(wf):
                for f in forbidden:
                    assert f not in value, (
                        f"{wf_name} / '{step_name}': '{value}' looks like an A/B secret "
                        f"(missing _C suffix)"
                    )

    def test_all_session_workflows_exist(self):
        """Sanity check — the three session workflows must exist."""
        for name in SESSION_WORKFLOWS:
            assert (WORKFLOWS_DIR / name).exists(), f"Missing workflow: {name}"
