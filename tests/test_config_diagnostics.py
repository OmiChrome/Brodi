import os
import subprocess
import sys

from brodi import config, diagnostics


def test_environment_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("BRODI_TEST_LOCAL", raising=False)
    monkeypatch.delenv("BRODI_TEST_FALLBACK", raising=False)
    monkeypatch.setenv("BRODI_TEST_EXISTING", "environment")
    (tmp_path / ".env.local").write_text("BRODI_TEST_LOCAL=local\nBRODI_TEST_EXISTING=local\n")
    (tmp_path / ".env").write_text("BRODI_TEST_LOCAL=fallback\nBRODI_TEST_FALLBACK=fallback\n")
    config.load_environment(tmp_path)
    assert os.environ["BRODI_TEST_LOCAL"] == "local"
    assert os.environ["BRODI_TEST_FALLBACK"] == "fallback"
    assert os.environ["BRODI_TEST_EXISTING"] == "environment"
    # Restore values introduced by dotenv, too.
    monkeypatch.delenv("BRODI_TEST_LOCAL")
    monkeypatch.delenv("BRODI_TEST_FALLBACK")


def test_package_import_does_not_load_agent_before_configuration():
    result = subprocess.run(
        [sys.executable, "-c", "import brodi, sys; assert 'brodi.agent' not in sys.modules"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_dotenv_profile_and_models_apply_before_runtime_imports(tmp_path):
    (tmp_path / ".env.local").write_text(
        "BRODI_CHROME_PROFILE_DIR=Profile QA\nBRODI_DECISIONS_MODEL=qa-routing-model\n"
    )
    env = dict(os.environ)
    env.pop("BRODI_CHROME_PROFILE_DIR", None)
    env.pop("BRODI_DECISIONS_MODEL", None)
    code = "from pathlib import Path; import sys; from brodi.config import load_environment; load_environment(Path(sys.argv[1])); from brodi import chrome; from brodi.decisions import DecisionsClient; assert chrome.profile_dir() == 'Profile QA'; assert DecisionsClient().model == 'qa-routing-model'"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_doctor_shows_status_without_exposing_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-value")
    monkeypatch.setattr(diagnostics.chrome, "find_chrome_executable", lambda: sys.executable)
    monkeypatch.setattr(diagnostics.browser_task, "access_error", lambda: None)
    checks = diagnostics.report()
    assert all(ok for _, ok, _ in checks)
    assert "private-test-value" not in str(checks)


def test_doctor_reports_browser_blocker_and_skips_missing_browser_probe(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(diagnostics.chrome, "find_chrome_executable", lambda: str(tmp_path / "missing.exe"))

    def no_browser_probe():
        raise AssertionError("A missing browser cannot be connected")

    monkeypatch.setattr(diagnostics.browser_task, "access_error", no_browser_probe)
    checks = {name: (ok, detail) for name, ok, detail in diagnostics.report()}
    assert checks["OpenAI key"][0] is False
    assert checks["Chrome"][0] is False
    assert "Install Chrome" in checks["Browser access"][1]


def test_cli_doctor_never_starts_agent(monkeypatch, capsys):
    import asyncio

    from brodi import cli

    monkeypatch.setattr(cli, "load_environment", lambda: None)
    monkeypatch.setattr(diagnostics, "report", lambda: [("Browser access", False, "enable debugging")])
    assert asyncio.run(cli._main(["--doctor"])) == 1
    assert "NEEDS SETUP Browser access" in capsys.readouterr().out
