"""Tests for scripts/bootstrap.py, which setup.bat and setup.sh run."""

import importlib.util
import json
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


def release(folder: Path) -> Path:
    """Creates a folder that looks like an extracted api-media release."""
    folder.mkdir(parents=True)
    (folder / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    return folder


@pytest.fixture
def yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answers yes to every question."""
    monkeypatch.setattr(bootstrap, "ask", lambda _question: True)


@pytest.mark.usefixtures("yes")
def test_versioned_copy_next_to_the_bot_is_moved_into_place(repo: Path) -> None:
    release(repo.parent / "api-media-2.1.0")

    assert bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, bootstrap.Report())
    assert (repo.parent / "api-media" / "Dockerfile").is_file()
    assert not (repo.parent / "api-media-2.1.0").exists()


@pytest.mark.usefixtures("yes")
def test_copy_inside_an_extract_all_wrapper_is_unwrapped(repo: Path) -> None:
    release(repo.parent / "api-media-2.1.0" / "api-media-2.1.0")

    assert bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, bootstrap.Report())
    assert (repo.parent / "api-media" / "Dockerfile").is_file()
    assert not (repo.parent / "api-media-2.1.0").exists()


@pytest.mark.usefixtures("yes")
def test_wrapped_bot_gets_the_service_next_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = tmp_path / "discord-bot-x-1.0.0" / "discord-bot-x-1.0.0"
    bot.mkdir(parents=True)
    monkeypatch.setattr(bootstrap, "REPO", bot)
    release(tmp_path / "api-media-2.1.0" / "api-media-2.1.0")

    assert bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, bootstrap.Report())
    assert (bot.parent / "api-media" / "Dockerfile").is_file()
    assert not (tmp_path / "api-media-2.1.0").exists()


@pytest.mark.usefixtures("yes")
def test_newest_of_several_copies_is_used(repo: Path) -> None:
    release(repo.parent / "api-media-2.0.0")
    newest = release(repo.parent / "api-media-2.10.0")
    (newest / "marker").write_text("", encoding="utf-8")
    release(repo.parent / "api-media-2.9.1")

    assert bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, bootstrap.Report())
    assert (repo.parent / "api-media" / "marker").is_file()


@pytest.mark.usefixtures("yes")
def test_unrelated_folders_are_never_moved(repo: Path) -> None:
    (repo.parent / "api-media-notes").mkdir()
    (repo.parent / "api-media-2.1.0").mkdir()

    bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, bootstrap.Report())

    assert not (repo.parent / "api-media").exists()
    assert (repo.parent / "api-media-notes").is_dir()
    assert (repo.parent / "api-media-2.1.0").is_dir()


@pytest.mark.parametrize(
    ("folder_name", "version"),
    [
        ("api-media", ()),
        ("api-media-2.1.0", (2, 1, 0)),
        ("api-media-v2.1", (2, 1)),
        ("api-media-main", None),
        ("api-mediaserver", None),
        ("api-morshu-2.1.0", None),
    ],
)
def test_release_version(folder_name: str, version: tuple[int, ...] | None) -> None:
    assert bootstrap.release_version(folder_name, "api-media") == version


def test_downloaded_bot_runs_without_the_service_folder(repo: Path) -> None:
    report = bootstrap.Report()

    assert bootstrap.ensure_siblings(STACK_BUILDS_MEDIA, report)
    assert report.problems == []


@pytest.mark.parametrize(
    ("compose", "name"),
    [
        ("# A comment.\nname: api-media\n\nservices:\n", "api-media"),
        ("services:\n  bot:\n    build: .\n", "discord-bot-x"),
    ],
)
def test_project_name(compose: str, name: str) -> None:
    assert bootstrap.project_name(Path("discord-bot-x"), compose) == name


def test_only_a_git_clone_builds_its_images(tmp_path: Path) -> None:
    assert bootstrap.uses_published_images(tmp_path)
    (tmp_path / ".git").mkdir()
    assert not bootstrap.uses_published_images(tmp_path)


def test_cron_line_runs_the_update_in_the_project_folder() -> None:
    line = bootstrap.cron_line(Path("/srv/my bots/discord-bot-x"), "discord-bot-x")

    assert line.startswith(f"0 {bootstrap.UPDATE_HOUR} * * * cd '")
    assert "my bots" in line
    assert "&& ./run.sh update < /dev/null >> update.log 2>&1" in line


def test_without_cron_line_keeps_every_other_line() -> None:
    mine = bootstrap.cron_line(Path("/srv/discord-bot-x"), "discord-bot-x")
    other = bootstrap.cron_line(Path("/srv/api-media"), "api-media")
    crontab = f"MAILTO=me\n{mine}\n{other}\n"

    assert bootstrap.without_cron_line(crontab, "discord-bot-x") == (
        f"MAILTO=me\n{other}\n"
    )


class FakeResult:
    """Stands in for the result of subprocess.run."""

    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


def test_pull_signs_in_when_the_registry_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter([FakeResult(1, "error from registry: denied"), FakeResult(0)])
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *_args, **_kwargs: next(results)
    )
    sign_ins: list[bool] = []
    monkeypatch.setattr(
        bootstrap, "sign_in_to_registry", lambda _report: sign_ins.append(True) or True
    )
    report = bootstrap.Report()

    assert bootstrap.pull_images(["docker", "compose"], report)
    assert sign_ins == [True]
    assert report.problems == []


def test_pull_explains_a_refusal_without_a_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = FakeResult(1, "unauthorized: authentication required")
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *_args, **_kwargs: refused)
    monkeypatch.setattr(bootstrap, "sign_in_to_registry", lambda _report: False)
    report = bootstrap.Report()

    assert not bootstrap.pull_images(["docker", "compose"], report)
    assert "refused" in report.problems[0].what
    assert "private" in report.problems[0].fix


def test_pull_failure_without_a_refusal_asks_for_no_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offline = FakeResult(1, "dial tcp: lookup ghcr.io: no such host")
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *_args, **_kwargs: offline)
    monkeypatch.setattr(bootstrap, "sign_in_to_registry", pytest.fail)
    report = bootstrap.Report()

    assert not bootstrap.pull_images(["docker", "compose"], report)
    assert report.problems[0].what == "Downloading the images failed."


def test_sign_in_needs_someone_at_the_keyboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.sys.stdin, "isatty", lambda: False)

    assert not bootstrap.sign_in_to_registry(bootstrap.Report())


def windows(
    *,
    wsl: bool = True,
    hypervisor: bool = True,
    firmware: bool = True,
    restart: bool = False,
) -> object:
    return bootstrap.Virtualization(
        wsl_installed=wsl,
        hypervisor_running=hypervisor,
        firmware_enabled=firmware,
        restart_pending=restart,
    )


@pytest.mark.parametrize(
    ("state", "step"),
    [
        # A working machine reports the firmware as off while the hypervisor runs.
        (windows(firmware=False), "READY"),
        (windows(), "READY"),
        (windows(wsl=False), "INSTALL_WSL"),
        (windows(wsl=False, hypervisor=False, firmware=False), "INSTALL_WSL"),
        (windows(hypervisor=False, restart=True), "RESTART"),
        (windows(hypervisor=False, firmware=False, restart=True), "RESTART"),
        (windows(hypervisor=False, firmware=False), "FIRMWARE"),
        (windows(hypervisor=False), "INSTALL_WSL"),
    ],
)
def test_next_virtualization_step(state: object, step: str) -> None:
    assert (
        bootstrap.next_virtualization_step(state) is bootstrap.VirtualizationStep[step]
    )


def test_firmware_problem_explains_where_to_turn_it_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "read_virtualization",
        lambda: windows(hypervisor=False, firmware=False),
    )
    report = bootstrap.Report()

    assert not bootstrap.prepare_windows_virtualization(report)
    assert "UEFI Firmware Settings" in report.problems[0].fix


def test_wsl_is_installed_after_asking_and_then_needs_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "read_virtualization", lambda: windows(wsl=False))
    monkeypatch.setattr(bootstrap, "ask", lambda _question: True)
    monkeypatch.setattr(
        bootstrap, "run", lambda command: commands.append(command) or True
    )
    report = bootstrap.Report()

    assert not bootstrap.prepare_windows_virtualization(report)
    assert commands == [["wsl.exe", "--install", "--no-distribution"]]
    assert "restart" in report.problems[0].what


def test_declining_wsl_installs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "read_virtualization", lambda: windows(wsl=False))
    monkeypatch.setattr(bootstrap, "ask", lambda _question: False)
    monkeypatch.setattr(
        bootstrap, "run", lambda command: commands.append(command) or True
    )
    report = bootstrap.Report()

    assert not bootstrap.prepare_windows_virtualization(report)
    assert commands == []
    assert report.skipped


def test_ready_windows_lets_docker_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "read_virtualization", lambda: windows())

    assert bootstrap.prepare_windows_virtualization(bootstrap.Report())


@pytest.mark.parametrize(
    ("total_gb", "suggested"),
    [(31.7, 4), (15.8, 4), (8.0, 4), (6.0, 3), (3.9, 2), (None, 4)],
)
def test_suggested_wsl_memory(total_gb: float | None, suggested: int) -> None:
    assert bootstrap.suggested_wsl_memory_gb(total_gb) == suggested


@pytest.mark.parametrize(
    ("text", "setting"),
    [
        ("[wsl2]\nmemory=6GB\n", "6GB"),
        ("[wsl2]\nprocessors=2\nMemory = 8GB\n", "8GB"),
        ("[wsl2]\n# memory=6GB\n", None),
        ("[experimental]\nmemory=6GB\n", None),
        ("", None),
    ],
)
def test_wsl_memory_setting(text: str, setting: str | None) -> None:
    assert bootstrap.wsl_memory_setting(text) == setting


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "[wsl2]\nmemory=4GB\n"),
        ("[wsl2]\nprocessors=2\n", "[wsl2]\nmemory=4GB\nprocessors=2\n"),
        (
            "[experimental]\nsparseVhd=true\n",
            "[experimental]\nsparseVhd=true\n\n[wsl2]\nmemory=4GB\n",
        ),
    ],
)
def test_with_wsl_memory_keeps_every_other_line(text: str, expected: str) -> None:
    assert bootstrap.with_wsl_memory(text, 4) == expected


@pytest.mark.parametrize(
    ("answer", "size"), [("", 4), ("6", 6), (" 6GB ", 6), ("n", None), ("NO", None)]
)
def test_read_size_answer(answer: str, size: int | None) -> None:
    assert bootstrap.read_size_answer(answer, 4, 15.8) == size


@pytest.mark.parametrize("answer", ["six", "4.5", "1", "16", "-3"])
def test_read_size_answer_rejects_unusable_sizes(answer: str) -> None:
    with pytest.raises(ValueError):
        bootstrap.read_size_answer(answer, 4, 15.8)


@pytest.fixture
def wslconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points the script at a .wslconfig in tmp_path on a computer with 15.8 GB of RAM."""
    path = tmp_path / ".wslconfig"
    monkeypatch.setattr(bootstrap, "WSL_CONFIG", path)
    monkeypatch.setattr(bootstrap, "total_memory_gb", lambda: 15.8)
    return path


def answers(monkeypatch: pytest.MonkeyPatch, *replies: str) -> None:
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda _prompt: queue.pop(0))


def test_enter_writes_the_suggested_cap(
    wslconfig: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers(monkeypatch, "")

    bootstrap.cap_wsl_memory(bootstrap.Report())

    assert wslconfig.read_text(encoding="utf-8") == "[wsl2]\nmemory=4GB\n"
    assert "15.8 GB of RAM" in capsys.readouterr().out


def test_own_size_is_written_after_a_wrong_answer(
    wslconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers(monkeypatch, "lots", "6")

    bootstrap.cap_wsl_memory(bootstrap.Report())

    assert wslconfig.read_text(encoding="utf-8") == "[wsl2]\nmemory=6GB\n"


def test_existing_cap_is_never_changed(
    wslconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wslconfig.write_text("[wsl2]\nmemory=12GB\n", encoding="utf-8")
    answers(monkeypatch)

    bootstrap.cap_wsl_memory(bootstrap.Report())

    assert wslconfig.read_text(encoding="utf-8") == "[wsl2]\nmemory=12GB\n"


def test_declining_writes_nothing(
    wslconfig: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers(monkeypatch, "n")
    report = bootstrap.Report()

    bootstrap.cap_wsl_memory(report)

    assert not wslconfig.exists()
    assert report.skipped


def test_parse_config_images_marks_the_projects_own_image(tmp_path: Path) -> None:
    bot = tmp_path / "discord-bot-x"
    config = {
        "name": "discord-bot-x",
        "services": {
            "bot": {
                "image": "ghcr.io/lempki/discord-bot-x:latest",
                "build": {"context": str(bot), "dockerfile": "Dockerfile"},
            },
            "media": {
                "image": "ghcr.io/lempki/api-media:latest",
                "build": {"context": str(tmp_path / "api-media")},
            },
            "built-only": {"build": {"context": str(bot)}},
        },
    }

    images = bootstrap.parse_config_images(json.dumps(config), bot)

    assert [(i.service, i.ref, i.own) for i in images] == [
        ("bot", "ghcr.io/lempki/discord-bot-x:latest", True),
        ("media", "ghcr.io/lempki/api-media:latest", False),
    ]
