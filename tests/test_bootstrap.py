"""Tests for scripts/bootstrap.py, which setup.bat and setup.sh run."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bootstrap.py"
_SPEC = importlib.util.spec_from_file_location("bootstrap", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(_SPEC)
sys.modules["bootstrap"] = bootstrap
_SPEC.loader.exec_module(bootstrap)

ENV = """\
# Required.
DISCORD_TOKEN=your-token-here

# Optional settings follow.
# LOCALE=silent
# API_MEDIA_URL=http://localhost:8001
# API_MEDIA_SECRET=your-secret-here
"""


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("DISCORD_TOKEN", "your-token-here"),
        ("LOCALE", None),
        ("MISSING", None),
    ],
)
def test_env_value_reads_only_active_lines(key: str, expected: str | None) -> None:
    assert bootstrap.env_value(ENV, key) == expected


def test_set_env_value_replaces_an_active_line() -> None:
    result = bootstrap.set_env_value(ENV, "DISCORD_TOKEN", "a.b.c")

    assert "DISCORD_TOKEN=a.b.c\n" in result
    assert "your-token-here" not in result


def test_set_env_value_uncomments_the_templates_line() -> None:
    result = bootstrap.set_env_value(ENV, "API_MEDIA_SECRET", "generated")

    lines = result.splitlines()
    assert "API_MEDIA_SECRET=generated" in lines
    assert lines.index("API_MEDIA_SECRET=generated") == ENV.splitlines().index(
        "# API_MEDIA_SECRET=your-secret-here"
    )


def test_set_env_value_appends_an_unknown_key() -> None:
    result = bootstrap.set_env_value(ENV, "NEW_KEY", "value")

    assert result.endswith("NEW_KEY=value\n")
    assert result.startswith(ENV)


def test_commented_value_reads_the_templates_example() -> None:
    assert bootstrap.commented_value(ENV, "API_MEDIA_URL") == "http://localhost:8001"
    assert bootstrap.commented_value(ENV, "DISCORD_TOKEN") is None


STACK = "API_SECRET: ${API_MEDIA_SECRET:?Set API_MEDIA_SECRET in .env}\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points the script at a bot folder inside tmp_path, next to which services can sit."""
    bot = tmp_path / "discord-bot-x"
    bot.mkdir()
    monkeypatch.setattr(bootstrap, "REPO", bot)
    return bot


def test_stack_secret_reuses_the_services_own_secret(repo: Path) -> None:
    service = repo.parent / "api-media"
    service.mkdir()
    (service / ".env").write_text(
        "API_SECRET=the-services-own-secret\n", encoding="utf-8"
    )

    result = bootstrap.fill_stack_secrets(ENV, STACK, bootstrap.Report())

    assert bootstrap.env_value(result, "API_MEDIA_SECRET") == "the-services-own-secret"


def test_stack_secret_is_generated_and_its_url_turned_on(repo: Path) -> None:
    result = bootstrap.fill_stack_secrets(ENV, STACK, bootstrap.Report())

    assert not bootstrap.is_placeholder(bootstrap.env_value(result, "API_MEDIA_SECRET"))
    assert bootstrap.env_value(result, "API_MEDIA_URL") == "http://localhost:8001"


def test_stack_secret_keeps_an_active_url(repo: Path) -> None:
    env = ENV.replace(
        "# API_MEDIA_URL=http://localhost:8001", "API_MEDIA_URL=http://media.lan"
    )

    result = bootstrap.fill_stack_secrets(env, STACK, bootstrap.Report())

    assert bootstrap.env_value(result, "API_MEDIA_URL") == "http://media.lan"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", True),
        ("your-secret-here", True),
        ("YOUR-TOKEN-HERE", True),
        ("changeme", True),
        ("a-real-generated-secret", False),
    ],
)
def test_is_placeholder(value: str | None, expected: bool) -> None:
    assert bootstrap.is_placeholder(value) is expected


@pytest.mark.parametrize(
    ("dockerfile", "windows", "expected"),
    [
        ("RUN apt-get install -y ffmpeg libopus0", False, ["FFmpeg", "Opus"]),
        ("RUN apt-get install -y ffmpeg libopus0", True, ["FFmpeg"]),
        ("COPY --from=denoland/deno:bin /deno /usr/local/bin/deno", False, ["Deno"]),
        ("FROM python:3.12-slim", False, []),
    ],
)
def test_tools_from_dockerfile(
    dockerfile: str, windows: bool, expected: list[str]
) -> None:
    tools = bootstrap.tools_from_dockerfile(dockerfile, windows=windows)

    assert [tool.name for tool in tools] == expected


def test_required_stack_secrets_skips_optional_ones() -> None:
    compose = """
      API_SECRET: ${API_MORSHU_SECRET:?Set API_MORSHU_SECRET in .env}
      API_SECRET: ${API_MEDIA_SECRET:-}
      API_SECRET: ${API_TEXT_TO_SPEECH_SECRET:?Set it}
      API_SECRET: ${API_MORSHU_SECRET:?Again}
    """

    assert bootstrap.required_stack_secrets(compose) == [
        ("API_MORSHU_SECRET", "api-morshu"),
        ("API_TEXT_TO_SPEECH_SECRET", "api-text-to-speech"),
    ]


def test_stack_siblings_lists_each_repository_once() -> None:
    compose = """
  bot:
    build: .
  morshu:
    build: ../api-morshu
  media:
    build: ../api-media
  again:
    build: ../api-media
"""

    assert bootstrap.stack_siblings(compose) == ["api-media", "api-morshu"]


def test_stack_siblings_skips_services_behind_a_profile() -> None:
    compose = """
services:
  bot:
    build: .
  morshu:
    build: ../api-morshu
  media:
    build: ../api-media
    profiles: ["media"]

volumes:
  bot-data:
"""

    assert bootstrap.stack_siblings(compose) == ["api-morshu"]


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            "https://github.com/Owner/discord-bot-x.git\n",
            "https://github.com/Owner/api-media.git",
        ),
        (
            "https://github.com/Owner/discord-bot-x",
            "https://github.com/Owner/api-media.git",
        ),
        (
            "git@github.com:Owner/discord-bot-x.git",
            "git@github.com:Owner/api-media.git",
        ),
    ],
)
def test_sibling_url_keeps_the_owner_and_the_protocol(
    origin: str, expected: str
) -> None:
    assert bootstrap.sibling_url(origin, "api-media") == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("", True), ("y", True), ("YES", True), ("n", False), ("maybe", False)],
)
def test_ask_treats_enter_as_yes(
    monkeypatch: pytest.MonkeyPatch, answer: str, expected: bool
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert bootstrap.ask("Install it?") is expected


def test_ask_without_input_answers_no(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)

    assert bootstrap.ask("Install it?") is False


STACK_BUILDS_MEDIA = "  media:\n    build: ../api-media\n"


def test_downloaded_copy_is_renamed_after_asking(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo.parent / "api-media-2.1.0").mkdir()
    monkeypatch.setattr(bootstrap, "ask", lambda _question: True)
    report = bootstrap.Report()

    assert bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, report)
    assert (repo.parent / "api-media").is_dir()
    assert not (repo.parent / "api-media-2.1.0").exists()


def test_two_downloaded_copies_are_never_guessed_between(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo.parent / "api-media-2.0.0").mkdir()
    (repo.parent / "api-media-2.1.0").mkdir()
    monkeypatch.setattr(bootstrap, "ask", lambda _question: True)
    report = bootstrap.Report()

    assert not bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, report)
    assert not (repo.parent / "api-media").exists()


def test_downloaded_bot_explains_how_to_get_a_missing_service(repo: Path) -> None:
    report = bootstrap.Report()

    assert not bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, report)
    assert "Download the latest release of api-media" in report.problems[0].fix
