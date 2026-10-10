"""Starts this project, or runs another everyday action, once setup has run.

run.bat and run.sh run this script with uv.
Without an action it starts everything in Docker in the background.
It then waits until every service is ready and shows their status.
A bot starts together with the api-* services in its compose.stack.yml.

A downloaded release runs the images that GitHub publishes for every release.
Its update action downloads the newest images and the files that belong to them.
When the new version fails to start, the update puts the previous one back.
A Git clone builds its images from its own files instead, and its update pulls the code.

The script uses the standard library and the helpers in bootstrap.py, which it shares with setup.
It is identical in every bot and api-* repository.
"""

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from bootstrap import (
    DOCKER,
    REPO,
    UPDATE_HOUR,
    Report,
    ask,
    docker_info,
    ensure_docker_running,
    env_value,
    fill_stack_secrets,
    image_id,
    is_installed,
    is_placeholder,
    missing_images,
    prepare_windows_virtualization,
    print_problems,
    project_name,
    pull_images,
    required_stack_secrets,
    run,
    schedule_updates,
    section,
    service_images,
    stack_siblings,
    unschedule_updates,
    updates_scheduled,
    uses_published_images,
)

ACTIONS = {
    "start": "Start everything in Docker, then wait until it is ready.",
    "stop": "Stop the containers. They stay stopped until the next start.",
    "status": "Show whether each container runs, its version, and automatic updates.",
    "logs": "Follow the logs. Press Ctrl+C to stop following.",
    "update": "Install the newest release, or pull the latest code in a Git clone.",
    "schedule": f"Install new releases automatically every night at {UPDATE_HOUR:02d}:00.",
    "unschedule": "Stop installing new releases automatically.",
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

# The files of a downloaded release that an update replaces with the copies in the new image.
# Settings, such as .env and the cookies folder, are never among them.
RELEASE_FILES = (
    ".env.template",
    "README.md",
    "compose.stack.yml",
    "docker-compose.yml",
    "run.bat",
    "run.sh",
    "scripts",
    "setup.bat",
    "setup.sh",
)

# Runs inside an image and writes the release files that it holds to standard output as tar.
_PACK_RELEASE_FILES = """import pathlib, sys, tarfile
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
    for name in sys.argv[1:]:
        if pathlib.Path(name).exists():
            archive.add(name, filter=lambda info: None if "__pycache__" in info.name else info)
"""

# The label in which each published image carries its release version.
VERSION_LABEL = "org.opencontainers.image.version"

# Where an update keeps what it needs to put the previous version back, inside the project folder.
UPDATE_FOLDER = ".update"


@dataclass(frozen=True)
class Project:
    """What kind of repository this is and how Docker Compose runs it.

    Attributes:
        root: The repository folder.
        is_bot: Whether the repository is a Discord bot.
        compose_file: The compose file that starts everything, relative to root.
        compose_text: The contents of the compose file.
        published: Whether the project runs the published images instead of building them.
    """

    root: Path
    is_bot: bool
    compose_file: str
    compose_text: str
    published: bool = False

    @property
    def compose(self) -> list[str]:
        """The start of every docker compose command for this project."""
        return ["docker", "compose", "-f", self.compose_file]

    @property
    def name(self) -> str:
        """The compose project name, which stays the same whatever the folder is called."""
        return project_name(self.root, self.compose_text)


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
        root=root,
        is_bot=is_bot,
        compose_file=compose_file,
        compose_text=text,
        published=uses_published_images(root),
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
    # A downloaded release runs the published images, so it builds nothing from the other folders.
    if project.published:
        return ready
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


def docker_quietly(*arguments: str) -> bool:
    """Runs a docker command without showing its output and returns whether it worked."""
    result = subprocess.run(["docker", *arguments], capture_output=True, check=False)
    return result.returncode == 0


def version_of(target: str) -> str:
    """Returns the release version of an image or container, or an empty string without one."""
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            f'{{{{index .Config.Labels "{VERSION_LABEL}"}}}}',
            target,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    version = result.stdout.strip() if result.returncode == 0 else ""
    return "" if version == "<no value>" else version


def show_status(project: Project) -> None:
    """Prints one line per container with its state, health, and published ports.

    The release version of each container and whether updates are automatic follow.
    """
    table = "table {{.Service}}\t{{.Status}}\t{{.Ports}}"
    subprocess.run([*project.compose, "ps", "--all", "--format", table], check=False)
    versions = [
        f"{state.service} {version}"
        for state in service_states(project)
        if (version := version_of(state.container))
    ]
    if versions:
        print(f"  Versions: {', '.join(versions)}")
    if project.published:
        if updates_scheduled(project.name):
            print(
                f"  New releases install automatically every night at {UPDATE_HOUR:02d}:00."
            )
        else:
            print("  New releases install when you run the update action.")


def launch(project: Project, report: Report, command: list[str]) -> bool:
    """Starts the containers with a docker compose up command and waits until they are ready.

    Returns:
        Whether everything started and became ready.
    """
    if not run(command):
        report.problem(
            "Docker could not start the containers.",
            "The project is not running.",
            "Read the error above, fix it, and run this script again.",
        )
        return False
    return wait_until_ready(project, report)


def show_result(project: Project, ready: bool) -> None:
    """Shows the status after a start, and that it lasts when everything is ready."""
    section("Status")
    show_status(project)
    if ready:
        print()
        print(
            "  Everything runs in the background and starts again whenever Docker starts."
        )


def start(project: Project, report: Report, *, pull: bool = False) -> bool:
    """Starts every container in the background and waits until they are ready.

    A downloaded release starts from the published images, which it downloads when missing.
    A Git clone builds its images first.

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
    if project.published:
        missing = missing_images(project.compose, project.root)
        if missing and not pull_images(project.compose, report, missing):
            return False
        ready = launch(project, report, [*project.compose, "up", "-d", "--no-build"])
        show_result(project, ready)
        return ready
    build = [*project.compose, "build"]
    if pull and not run([*build, "--pull"]):
        report.problem(
            "Docker could not build the images.",
            "Nothing new was started.",
            "Read the error above, fix it, and run this script again.",
        )
        return False
    ready = launch(project, report, [*project.compose, "up", "-d", "--build"])
    show_result(project, ready)
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


def running_images(project: Project) -> dict[str, str]:
    """Returns the ID of the image that each service's container was created from."""
    images: dict[str, str] = {}
    for state in service_states(project):
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", state.container],
            capture_output=True,
            text=True,
            check=False,
        )
        image = result.stdout.strip()
        if result.returncode == 0 and image:
            images[state.service] = image
    return images


@dataclass
class UpdateState:
    """What earlier updates learned, kept in the update folder between runs.

    Attributes:
        broken: The IDs of images that failed to start, which later updates skip.
    """

    broken: set[str] = field(default_factory=set)

    @staticmethod
    def path(root: Path) -> Path:
        """Returns the file that holds the state of the project in root."""
        return root / UPDATE_FOLDER / "state.json"

    @classmethod
    def load(cls, root: Path) -> "UpdateState":
        """Reads the state, which is empty before the first update or when the file is damaged."""
        try:
            data = json.loads(cls.path(root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        broken = data.get("broken", []) if isinstance(data, dict) else []
        return cls(broken={str(image) for image in broken})

    def save(self, root: Path) -> None:
        """Writes the state."""
        path = self.path(root)
        path.parent.mkdir(exist_ok=True)
        data = {"broken": sorted(self.broken)}
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@dataclass
class FileBackup:
    """The release files that an update replaced, so that the previous ones can be put back.

    Attributes:
        folder: Where the previous copies wait.
        saved: The files that the update replaced, relative to the project folder.
        added: The files that the update added, relative to the project folder.
    """

    folder: Path
    saved: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


def read_release_files(image: str) -> bytes | None:
    """Copies the release files out of an image as a tar archive, or returns None on failure."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "--workdir",
            "/app",
            image,
            "-c",
            _PACK_RELEASE_FILES,
            *RELEASE_FILES,
        ],
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def is_release_file(name: str) -> bool:
    """Returns whether an archive path is a release file that stays inside the project folder."""
    parts = Path(name).parts
    return bool(parts) and parts[0] in RELEASE_FILES and ".." not in parts


def replace_release_files(archive: bytes, root: Path) -> FileBackup:
    """Writes the release files from an archive into the project folder.

    Only files that differ are written, so a running run.bat stays untouched in most updates.
    The copies that they replace are saved first, so that restore_release_files can put them back.

    Args:
        archive: The tar archive from read_release_files.
        root: The project folder.

    Returns:
        What was replaced and added.
    """
    backup = FileBackup(folder=root / UPDATE_FOLDER / "previous")
    shutil.rmtree(backup.folder, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        changed: list[tarfile.TarInfo] = []
        for member in tar.getmembers():
            if not (member.isfile() and is_release_file(member.name)):
                continue
            content = tar.extractfile(member)
            current = root / member.name
            if current.is_file():
                if content is not None and content.read() == current.read_bytes():
                    continue
                saved = backup.folder / member.name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, saved)
                backup.saved.append(member.name)
            else:
                backup.added.append(member.name)
            changed.append(member)
        tar.extractall(root, members=changed, filter="data")
    return backup


def restore_release_files(backup: FileBackup, root: Path) -> None:
    """Puts back the release files that replace_release_files replaced."""
    for name in backup.added:
        (root / name).unlink(missing_ok=True)
    for name in backup.saved:
        shutil.copy2(backup.folder / name, root / name)


def add_new_secrets(project: Project, report: Report) -> None:
    """Fills in the API secrets that a new compose.stack.yml requires and .env lacks."""
    if project.compose_file != "compose.stack.yml":
        return
    env_file = project.root / ".env"
    text = env_file.read_text(encoding="utf-8")
    filled = fill_stack_secrets(text, project.compose_text, report)
    if filled != text:
        # LF line endings, as setup writes them.
        env_file.write_text(filled, encoding="utf-8", newline="")


def describe(image: str | None) -> str:
    """Names the version of an image for the update's messages."""
    if image is None:
        return "an unknown version"
    version = version_of(image)
    return f"version {version}" if version else "an unnumbered version"


def previous_ref(ref: str, project_name: str) -> str:
    """Returns the name that keeps an image's running version while a project updates.

    Docker deletes an image that loses its last name, even while a container still runs it.
    Pulling a new version moves the name away, so the running version gets this extra name first.
    The project name keeps two projects that run the same image apart.

    Args:
        ref: The image name and tag, such as ghcr.io/lempki/api-media:latest.
        project_name: The compose project name.

    Returns:
        The same image name with a tag such as previous-discord-bot-morshu.
    """
    repository, _, tag = ref.rpartition(":")
    # A colon before the last slash belongs to a registry port, such as localhost:5000/name.
    if not repository or "/" in tag:
        repository = ref
    return f"{repository}:previous-{project_name}"


def keep_previous(before: dict[str, str | None], project_name: str) -> dict[str, str]:
    """Gives the running version of each image a second name, so that it survives a pull.

    Args:
        before: The image ID that each image name ran before the update, or None.
        project_name: The compose project name.

    Returns:
        The second name of each image name whose running version could be kept.
    """
    kept: dict[str, str] = {}
    for ref, old in before.items():
        name = previous_ref(ref, project_name)
        if old is not None and docker_quietly("tag", old, name):
            kept[ref] = name
    return kept


def forget_previous(kept: dict[str, str]) -> None:
    """Removes the second names, which deletes previous versions that nothing else uses."""
    for name in kept.values():
        docker_quietly("image", "rm", name)


def roll_back(
    root: Path,
    changed: dict[str, str],
    kept: dict[str, str],
    backup: FileBackup | None,
    report: Report,
) -> None:
    """Puts the previous images and release files back and starts them again.

    The new images are remembered as broken, so later updates skip them.

    Args:
        root: The project folder.
        changed: The new image ID for each image name that the update changed.
        kept: The second name that holds the previous version of each image name.
        backup: The release files that the update replaced, if it replaced any.
        report: The report that records the outcome.
    """
    section("Putting the previous version back")
    for ref in changed:
        if ref in kept:
            docker_quietly("tag", kept[ref], ref)
    forget_previous(kept)
    if backup is not None:
        restore_release_files(backup, root)
    state = UpdateState.load(root)
    state.broken.update(changed.values())
    state.save(root)
    project = detect_project(root)
    lost = [ref for ref in changed if ref not in kept]
    if lost:
        print(f"  The previous version of {', '.join(lost)} was gone already.")
    if launch(project, report, [*project.compose, "up", "-d", "--no-build"]):
        report.problem(
            "The new release failed to start, so the update was undone.",
            "The previous version runs again, so nothing changed for the people who use it.",
            "Read the log above for the cause. "
            "Later updates skip this release and try the next one that appears.",
        )
    else:
        report.problem(
            "The previous version did not start again either.",
            "The project is not running.",
            "Read the log above. Then run the start action, or the setup script.",
        )
    section("Status")
    show_status(project)


def update_release(project: Project, report: Report) -> bool:
    """Installs the newest published release, and puts the previous one back if it fails to start.

    The update downloads the newest image of every service.
    A new image of the project itself brings new release files, such as compose and run files.
    A new release that fails to start is undone and skipped until a newer one appears.

    Returns:
        Whether the newest release runs.
    """
    print(f"  Started at {time.strftime('%Y-%m-%d %H:%M')}.")
    if not (check_setup(project, report) and check_docker(report)):
        return False
    section("Downloading the newest release")
    images = service_images(project.compose, project.root)
    if not images:
        report.problem(
            f"Docker Compose could not read {project.compose_file}.",
            "The update cannot tell which images to download.",
            f"Read the error above. {SETUP_FIX}",
        )
        return False
    # Another project may have pulled the same image already, which moved its tag.
    # So the containers, not the tags, tell which version this project runs.
    running = running_images(project)
    before = {
        image.ref: running.get(image.service) or image_id(image.ref) for image in images
    }
    kept = keep_previous(before, project.name)
    if not pull_images(project.compose, report):
        forget_previous(kept)
        return False

    state = UpdateState.load(project.root)
    changed: dict[str, str] = {}
    for ref, old in before.items():
        new = image_id(ref)
        if new is None or new == old:
            continue
        if new in state.broken and ref in kept:
            # Keeping the running version stops a release that failed once from failing every night.
            report.ok(
                f"Skipped {describe(new)} of {ref}, which failed to start before. "
                f"{describe(kept[ref]).capitalize()} keeps running."
            )
            docker_quietly("tag", kept[ref], ref)
            continue
        print(f"  {ref}: {describe(kept.get(ref))} -> {describe(new)}")
        changed[ref] = new
    if not changed:
        forget_previous(kept)
        report.ok("Nothing new to install.")
        ready = launch(project, report, [*project.compose, "up", "-d", "--no-build"])
        show_result(project, ready)
        return ready

    backup = None
    own = next((image for image in images if image.own and image.ref in changed), None)
    if own is not None:
        archive = read_release_files(changed[own.ref])
        if archive is None:
            for ref in changed:
                if ref in kept:
                    docker_quietly("tag", kept[ref], ref)
            forget_previous(kept)
            report.problem(
                "The new release's files could not be copied out of its image.",
                "The previous version keeps running.",
                "Run the update action again. If it keeps failing, report it on GitHub.",
            )
            return False
        backup = replace_release_files(archive, project.root)
        written = len(backup.saved) + len(backup.added)
        report.ok(
            f"Took {written} changed file(s) from the new release, such as run and compose files."
            if written
            else "The new release brings no changes to the run, setup, or compose files."
        )
        project = detect_project(project.root)
        add_new_secrets(project, report)

    section("Restarting")
    if not launch(project, report, [*project.compose, "up", "-d", "--no-build"]):
        roll_back(project.root, changed, kept, backup, report)
        return False
    forget_previous(kept)
    if backup is not None:
        shutil.rmtree(backup.folder, ignore_errors=True)
    state.broken.difference_update(changed.values())
    state.save(project.root)
    report.ok("Updated to the newest release.")
    show_result(project, True)
    return True


def update(project: Project, report: Report) -> bool:
    """Installs the newest release, or pulls the latest code and rebuilds in a Git clone."""
    if project.published:
        return update_release(project, report)
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


def schedule(project: Project, report: Report) -> bool:
    """Makes the computer install the newest release every night."""
    if not project.published:
        report.problem(
            "Automatic updates are for downloaded releases, and this folder is a Git clone.",
            "A Git clone may hold changes of its own, which an unattended update could break.",
            "Run the update action whenever you want the latest code.",
        )
        return False
    return schedule_updates(project.root, project.name, report)


def unschedule(project: Project, report: Report) -> bool:
    """Stops installing new releases every night."""
    return unschedule_updates(project.name, report)


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
    width = max(len(name) for name in ACTIONS) + 2
    epilog = "\n".join(f"  {name:<{width}}{text}" for name, text in ACTIONS.items())
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
        "schedule": schedule,
        "unschedule": unschedule,
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
