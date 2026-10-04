"""Starts this project, or runs another everyday action, once setup has run.

run.bat and run.sh run this script with uv.
Without an action it builds and starts everything in Docker in the background.
It then waits until every service is ready and shows their status.
A bot starts together with the api-* services in its compose.stack.yml.

The script uses the standard library and the helpers in bootstrap.py, which it shares with setup.
It is identical in every bot and api-* repository.
"""

import argparse
import io
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from bootstrap import (
    DOCKER,
    REPO,
    Report,
    ask,
    docker_info,
    ensure_docker_running,
    env_value,
    is_installed,
    is_placeholder,
    prepare_windows_virtualization,
    print_problems,
    required_stack_secrets,
    run,
    section,
    stack_siblings,
)

ACTIONS = {
    "start": "Build and start everything in Docker, then wait until it is ready.",
    "stop": "Stop the containers. They stay stopped until the next start.",
    "status": "Show whether each container runs and is healthy.",
    "logs": "Follow the logs. Press Ctrl+C to stop following.",
    "update": "Pull the latest code, rebuild on fresh base images, and restart.",
    "local": "Run the project in this window without Docker. Press Ctrl+C to stop it.",
}

READY_TIMEOUT_SECONDS = 180
POLL_SECONDS = 2
# A bot has no health check, so one that crashes at startup still looks ready at first.
# Watching the restart counts for a while longer catches that.
SETTLE_SECONDS = 15

# The host side of a published port, such as "8001" in "8001:8000".
_HOST_PORT = re.compile(r"""["']?(\d+):8000["']?""")

SETUP_FIX = (
    "Run setup.bat on Windows or ./setup.sh elsewhere, then run this script again."
)


@dataclass(frozen=True)
class Project:
    """What kind of repository this is and how Docker Compose runs it.

    Attributes:
        root: The repository folder.
        is_bot: Whether the repository is a Discord bot.
        compose_file: The compose file that starts everything, relative to root.
        compose_text: The contents of the compose file.
    """

    root: Path
    is_bot: bool
    compose_file: str
    compose_text: str

    @property
    def compose(self) -> list[str]:
        """The start of every docker compose command for this project."""
        return ["docker", "compose", "-f", self.compose_file]


@dataclass(frozen=True)
class ServiceState:
    """One container as docker compose ps reports it.

    Attributes:
        service: The service name from the compose file.
        container: The container ID.
        state: The container state, such as running, restarting, or exited.
        health: The health check result, or an empty string without a health check.
    """

    service: str
    container: str
    state: str
    health: str

    @property
    def ready(self) -> bool:
        """Whether the container runs and its health check, if any, passes."""
        return self.state == "running" and self.health in ("", "healthy")

    @property
    def failed(self) -> bool:
        """Whether the container stopped, keeps restarting, or fails its health check."""
        return (
            self.state in ("exited", "dead", "restarting") or self.health == "unhealthy"
        )


def detect_project(root: Path) -> Project:
    """Works out whether root holds a bot or an API, and which compose file starts it."""
    is_bot = (root / "bot.py").exists()
    stack = root / "compose.stack.yml"
    compose_file = (
        "compose.stack.yml" if is_bot and stack.exists() else "docker-compose.yml"
    )
    compose_path = root / compose_file
    text = compose_path.read_text(encoding="utf-8") if compose_path.exists() else ""
    return Project(
        root=root, is_bot=is_bot, compose_file=compose_file, compose_text=text
    )


def parse_ps(output: str) -> list[ServiceState]:
    """Reads the output of docker compose ps --format json.

    Docker Compose before 2.21 prints one JSON array, and later versions print one object per line.

    Args:
        output: The command's standard output.

    Returns:
        One entry per container, in the order Docker Compose lists them.
    """
    text = output.strip()
    if not text:
        return []
    if text.startswith("["):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [
        ServiceState(
            service=str(row.get("Service", "")),
            container=str(row.get("ID", "")),
            state=str(row.get("State", "")).lower(),
            health=str(row.get("Health", "")).lower(),
        )
        for row in rows
    ]


def host_port(compose_text: str) -> int:
    """Returns the host port that docker-compose.yml publishes the API on, or 8000 without one."""
    match = _HOST_PORT.search(compose_text)
    return int(match.group(1)) if match else 8000


def api_package(root: Path) -> str | None:
    """Returns the import package of an API, which is the directory under src that holds main.py."""
    mains = sorted(root.glob("src/*/main.py"))
    return mains[0].parent.name if mains else None


def check_setup(project: Project, report: Report) -> bool:
    """Checks that setup has prepared what the project needs to start.

    Returns:
        Whether everything is in place.
    """
    env_file = project.root / ".env"
    if not env_file.exists():
        report.problem(
            "This project has not been set up on this machine yet, because .env is missing.",
            "The project reads its settings from .env.",
            SETUP_FIX,
        )
        return False
    env = env_file.read_text(encoding="utf-8")
    ready = True
    key = "DISCORD_TOKEN" if project.is_bot else "API_SECRET"
    if is_placeholder(env_value(env, key)):
        report.problem(
            f"{key} is not set in .env.",
            "The bot cannot sign in to Discord."
            if project.is_bot
            else "The API refuses to start.",
            SETUP_FIX,
        )
        ready = False
    if project.compose_file != "compose.stack.yml":
        return ready
    for variable, _ in required_stack_secrets(project.compose_text):
        if is_placeholder(env_value(env, variable)):
            report.problem(
                f"{variable} is not set in .env.",
                "The Docker stack refuses to start without it.",
                SETUP_FIX,
            )
            ready = False
    for name in stack_siblings(project.compose_text):
        if not (project.root.parent / name).is_dir():
            report.problem(
                f"{name} is missing from the folder next to this one.",
                "The Docker stack builds it from there.",
                SETUP_FIX,
            )
            ready = False
    return ready


def report_missing_docker(report: Report) -> None:
    """Records that Docker is not installed."""
    report.problem(
        "Docker is not installed.",
        "Nothing can run in Docker.",
        "Run the setup script, which offers to install it. "
        "To run without Docker, use the local action instead.",
    )


def check_docker(report: Report) -> bool:
    """Makes sure Docker is installed and running, starting Docker Desktop when needed."""
    if not is_installed(DOCKER):
        report_missing_docker(report)
        return False
    if sys.platform == "win32" and not prepare_windows_virtualization(report):
        return False
    return ensure_docker_running(report, ask_first=False)


def docker_answers(report: Report) -> bool:
    """Returns whether Docker runs, without starting it.

    Stopping or inspecting containers needs no Docker Desktop start.
    While Docker is not running, none of the containers runs either.
    """
    if not is_installed(DOCKER):
        report_missing_docker(report)
        return False
    result = docker_info()
    if result is None or result.returncode != 0:
        report.ok("Docker is not running, so none of the containers runs either.")
        return False
    return True


def service_states(project: Project) -> list[ServiceState]:
    """Lists the project's containers, including stopped ones."""
    result = subprocess.run(
        [*project.compose, "ps", "--all", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return []
    return parse_ps(result.stdout)


def restart_counts(containers: list[str]) -> dict[str, int]:
    """Returns how often Docker has restarted each container."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}} {{.RestartCount}}", *containers],
        capture_output=True,
        text=True,
        check=False,
    )
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        container, _, count = line.partition(" ")
        if count.isdigit():
            counts[container[:12]] = int(count)
    return counts


def report_crash(project: Project, service: str, report: Report) -> None:
    """Shows the end of a failed service's log, stops the service, and records the problem.

    Docker would otherwise restart a crashing container over and over in the background.
    """
    print(f"  The last lines of {service}'s log:")
    run([*project.compose, "logs", "--tail", "30", service])
    subprocess.run(
        [*project.compose, "stop", service], capture_output=True, check=False
    )
    report.problem(
        f"{service} stopped or became unhealthy right after it started.",
        f"{service} is not running. It was stopped so that it does not restart over and over.",
        "Read its log above. A wrong value in .env is the most common cause. "
        "Fix it and run this script again.",
    )


def wait_until_ready(project: Project, report: Report) -> bool:
    """Waits until every container runs and passes its health check.

    Returns:
        Whether every container became ready and stayed up.
    """
    print("  Waiting for every service to report ready...")
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        states = service_states(project)
        failed = [s for s in states if s.failed]
        if failed:
            for state in failed:
                report_crash(project, state.service, report)
            return False
        if states and all(s.ready for s in states):
            break
        if time.monotonic() > deadline:
            waiting = (
                ", ".join(s.service for s in states if not s.ready) or "every service"
            )
            report.problem(
                f"{waiting} did not become ready within {READY_TIMEOUT_SECONDS // 60} minutes.",
                "The project may not work yet.",
                "Check the logs with the logs action. Then run this script again.",
            )
            return False
        time.sleep(POLL_SECONDS)

    containers = [s.container for s in states]
    before = restart_counts(containers)
    time.sleep(SETTLE_SECONDS)
    after = restart_counts(containers)
    crashed = [
        s
        for s in states
        if after.get(s.container[:12], 0) > before.get(s.container[:12], 0)
    ]
    for state in crashed:
        report_crash(project, state.service, report)
    if crashed:
        return False
    report.ok("Every service is ready.")
    return True


def show_status(project: Project) -> None:
    """Prints one line per container with its state, health, and published ports."""
    table = "table {{.Service}}\t{{.Status}}\t{{.Ports}}"
    subprocess.run([*project.compose, "ps", "--all", "--format", table], check=False)


def start(project: Project, report: Report, *, pull: bool = False) -> bool:
    """Builds and starts every container in the background and waits until they are ready.

    Args:
        project: The project to start.
        report: The report that records the outcome.
        pull: Whether to pull fresh base images while building.

    Returns:
        Whether everything started and became ready.
    """
    if not (check_setup(project, report) and check_docker(report)):
        return False
    section("Starting")
    build = [*project.compose, "build"]
    if pull and not run([*build, "--pull"]):
        report.problem(
            "Docker could not build the images.",
            "Nothing new was started.",
            "Read the error above, fix it, and run this script again.",
        )
        return False
    if not run([*project.compose, "up", "-d", "--build"]):
        report.problem(
            "Docker could not build or start the containers.",
            "The project is not running.",
            "Read the error above, fix it, and run this script again.",
        )
        return False
    ready = wait_until_ready(project, report)
    section("Status")
    show_status(project)
    if ready:
        print()
        print(
            "  Everything runs in the background and starts again whenever Docker starts."
        )
    return ready


def stop(project: Project, report: Report) -> bool:
    """Stops the containers without removing them or their data."""
    if not docker_answers(report):
        return not report.problems
    if run([*project.compose, "stop"]):
        report.ok("Stopped. The containers stay stopped, even after a restart.")
        return True
    report.problem(
        "Docker could not stop the containers.",
        "They may still be running.",
        "Read the error above, or stop them in Docker Desktop.",
    )
    return False


def status(project: Project, report: Report) -> bool:
    """Shows the state of every container."""
    if not docker_answers(report):
        return not report.problems
    show_status(project)
    return True


def logs(project: Project, report: Report) -> bool:
    """Follows the logs of every container until Ctrl+C."""
    if not docker_answers(report):
        return not report.problems
    try:
        run([*project.compose, "logs", "--follow", "--tail", "100"])
    except KeyboardInterrupt:
        print()
    return True


def update(project: Project, report: Report) -> bool:
    """Pulls the latest code of this repository and the ones its stack builds, then restarts."""
    section("Updating the code")
    folders = [project.root]
    if project.compose_file == "compose.stack.yml":
        folders += [
            project.root.parent / name for name in stack_siblings(project.compose_text)
        ]
    downloaded = [folder.name for folder in folders if not (folder / ".git").exists()]
    if downloaded:
        names = ", ".join(downloaded)
        report.problem(
            f"{names} is a downloaded copy, not a Git clone, so update cannot pull its code.",
            "Restarting now would rebuild the same code.",
            f"Download the latest release of {names} and copy its files over the folder, "
            "keeping .env. Then run start. A Git clone updates with this action alone.",
        )
        return False
    for folder in folders:
        print(f"  {folder.name}:")
        if not run(["git", "-C", str(folder), "pull", "--ff-only"]):
            report.problem(
                f"Updating {folder.name} failed.",
                "Restarting now would run code that is out of date.",
                "Read git's message above. Commit or stash local changes, "
                "or resolve a diverged branch, then run this script again.",
            )
            return False
    return start(project, report, pull=True)


def clashing_containers(project: Project) -> list[str]:
    """Lists the running containers that would clash with running the project without Docker.

    A bot in Docker would answer every command a second time.
    An API in Docker already holds the port that the local API needs.
    The services in a bot's stack publish no ports, so they never clash.
    """
    result = docker_info() if is_installed(DOCKER) else None
    if result is None or result.returncode != 0:
        return []
    running = [s.service for s in service_states(project) if s.state == "running"]
    return [s for s in running if s == "bot"] if project.is_bot else running


def run_local(project: Project, report: Report) -> bool:
    """Runs the bot or the API in this window, without Docker, until Ctrl+C."""
    if not check_setup(project, report):
        return False
    clashing = clashing_containers(project)
    if clashing:
        names = ", ".join(clashing)
        if ask(f"{names} already runs in Docker. Stop the Docker copy first?"):
            run([*project.compose, "stop", *clashing])
        else:
            report.skip(f"The Docker copy of {names} kept running.")

    section("Running without Docker")
    if project.is_bot:
        command = ["uv", "run", "python", "bot.py"]
        print(
            "  The bot reaches its services at the URLs in .env, such as http://localhost:8001."
        )
        print("  Start each service first with the run script in its own folder.")
    else:
        port = host_port(project.compose_text)
        package = api_package(project.root) or "app"
        command = ["uv", "run", "uvicorn", f"{package}.main:app", "--port", str(port)]
        print(f"  The API answers at http://localhost:{port}, with its docs at /docs.")
    print("  Press Ctrl+C to stop.")
    try:
        exit_code = subprocess.run(command, check=False).returncode
    except KeyboardInterrupt:
        return True
    if exit_code != 0:
        report.problem(
            f"The {'bot' if project.is_bot else 'API'} stopped with an error.",
            "It is not running.",
            "Read the error above, fix it, and run this script again.",
        )
        return False
    return True


def parse_args(argv: list[str]) -> str:
    """Returns the action to run, which is start when none is given."""
    epilog = "\n".join(f"  {name:<8}{text}" for name, text in ACTIONS.items())
    parser = argparse.ArgumentParser(
        prog="run",
        description="Starts this project, or runs another everyday action.",
        epilog=f"actions:\n{epilog}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=list(ACTIONS),
        metavar="action",
        help="One of the actions below. Without one, start runs.",
    )
    # "run help" reads more naturally than "run --help", so both work.
    if argv[:1] == ["help"]:
        argv = ["--help"]
    action: str = parser.parse_args(argv).action
    return action


def main(argv: list[str]) -> int:
    """Runs the requested action and returns the exit code for the run script."""
    action = parse_args(argv)
    # Docker writes straight to the terminal, so this script's lines must not wait in a buffer.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    project = detect_project(REPO)
    report = Report()
    print(f"=== {REPO.name}: {action} ===")
    handlers = {
        "start": start,
        "stop": stop,
        "status": status,
        "logs": logs,
        "update": update,
        "local": run_local,
    }
    succeeded = handlers[action](project, report)
    if report.problems or report.skipped:
        section("Summary")
        print_problems(report)
    return 0 if succeeded and not report.problems else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
        sys.exit(130)
