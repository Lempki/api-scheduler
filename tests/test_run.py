"""Tests for scripts/run.py, which run.bat and run.sh run."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
# run.py imports its helpers from bootstrap.py next to it, as it does when run as a script.
sys.path.insert(0, str(_SCRIPTS))
_SPEC = importlib.util.spec_from_file_location("run_script", _SCRIPTS / "run.py")
assert _SPEC is not None and _SPEC.loader is not None
run_script = importlib.util.module_from_spec(_SPEC)
sys.modules["run_script"] = run_script
_SPEC.loader.exec_module(run_script)

STACK = """\
services:
  bot:
    build: .
  media:
    build: ../api-media
    environment:
      API_SECRET: ${API_MEDIA_SECRET:?Set API_MEDIA_SECRET in .env}
"""


def test_no_action_means_start() -> None:
    assert run_script.parse_args([]) == "start"
    assert run_script.parse_args(["stop"]) == "stop"


@pytest.mark.parametrize("argv", [["help"], ["--help"]])
def test_help_lists_the_actions(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_script.parse_args(argv)

    assert exit_info.value.code == 0
    assert "update" in capsys.readouterr().out


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_script.parse_args(["launch"])

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "output",
    [
        '{"Service":"bot","ID":"abc","State":"running","Health":""}\n'
        '{"Service":"media","ID":"def","State":"running","Health":"healthy"}\n',
        '[{"Service":"bot","ID":"abc","State":"running","Health":""},'
        '{"Service":"media","ID":"def","State":"running","Health":"healthy"}]',
    ],
    ids=["one-object-per-line", "array"],
)
def test_parse_ps_reads_both_compose_formats(output: str) -> None:
    states = run_script.parse_ps(output)

    assert [(s.service, s.container, s.health) for s in states] == [
        ("bot", "abc", ""),
        ("media", "def", "healthy"),
    ]


def test_parse_ps_without_containers() -> None:
    assert run_script.parse_ps("") == []


@pytest.mark.parametrize(
    ("state", "health", "ready", "failed"),
    [
        ("running", "", True, False),
        ("running", "healthy", True, False),
        ("running", "starting", False, False),
        ("created", "", False, False),
        ("running", "unhealthy", False, True),
        ("restarting", "", False, True),
        ("exited", "", False, True),
    ],
)
def test_service_state(state: str, health: str, ready: bool, failed: bool) -> None:
    service = run_script.ServiceState("bot", "abc", state, health)

    assert service.ready is ready
    assert service.failed is failed


@pytest.mark.parametrize(
    ("compose", "port"),
    [
        ('    ports:\n      - "8001:8000"\n', 8001),
        ("    ports:\n      - 8004:8000\n", 8004),
        ("services:\n  api:\n    build: .\n", 8000),
    ],
)
def test_host_port(compose: str, port: int) -> None:
    assert run_script.host_port(compose) == port


def make_bot(root: Path, env: str) -> Path:
    bot = root / "discord-bot-x"
    bot.mkdir()
    (bot / "bot.py").write_text("", encoding="utf-8")
    (bot / "compose.stack.yml").write_text(STACK, encoding="utf-8")
    (bot / ".env").write_text(env, encoding="utf-8")
    return bot


def test_detect_project_picks_the_stack_for_a_bot(tmp_path: Path) -> None:
    project = run_script.detect_project(make_bot(tmp_path, ""))

    assert project.is_bot
    assert project.compose == ["docker", "compose", "-f", "compose.stack.yml"]


def test_detect_project_picks_docker_compose_for_an_api(tmp_path: Path) -> None:
    (tmp_path / "src" / "media_api").mkdir(parents=True)
    (tmp_path / "src" / "media_api" / "main.py").write_text("", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text(
        'ports:\n  - "8001:8000"\n', encoding="utf-8"
    )

    project = run_script.detect_project(tmp_path)

    assert not project.is_bot
    assert project.compose_file == "docker-compose.yml"
    assert run_script.api_package(tmp_path) == "media_api"


def test_check_setup_without_env(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, "")
    (bot / ".env").unlink()
    report = run_script.Report()

    assert not run_script.check_setup(run_script.detect_project(bot), report)
    assert ".env is missing" in report.problems[0].what


def test_check_setup_lists_every_missing_piece(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, "DISCORD_TOKEN=your-token-here\n")
    report = run_script.Report()

    assert not run_script.check_setup(run_script.detect_project(bot), report)
    assert [p.what.split()[0] for p in report.problems] == [
        "DISCORD_TOKEN",
        "API_MEDIA_SECRET",
        "api-media",
    ]


def test_check_setup_passes_when_everything_is_in_place(tmp_path: Path) -> None:
    bot = make_bot(tmp_path, "DISCORD_TOKEN=a.b.c\nAPI_MEDIA_SECRET=generated\n")
    (tmp_path / "api-media").mkdir()
    report = run_script.Report()

    assert run_script.check_setup(run_script.detect_project(bot), report)
    assert report.problems == []
