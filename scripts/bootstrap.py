"""Prepares this repository on a new machine in one run.

setup.bat and setup.sh install uv when it is missing and then run this script with it.
It is safe to run again at any time, because every step checks what is already done.
A step that fails says what went wrong, why it matters, and what to do next.
Only the project dependencies are required.
Every other tool is offered before it is installed, and declining one only limits what runs.

The script uses the standard library only, because it runs before the dependencies exist.
It is identical in every bot and api-* repository and finds out which kind it runs in.
scripts/run.py reuses its helpers for the everyday actions after setup.
"""

import contextlib
import ctypes.util
import getpass
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Values that .env templates have used to mean "fill this in".
PLACEHOLDERS = frozenset(
    {"", "your-token-here", "your-secret-here", "changeme", "secret"}
)

# A KEY=value line in .env, active or commented out.
_ENV_LINE = re.compile(r"^(?P<comment>#\s*)?(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")

# A secret that compose.stack.yml refuses to start without, such as ${API_MEDIA_SECRET:?...}.
_REQUIRED_STACK_SECRET = re.compile(r"\$\{API_([A-Z0-9_]+)_SECRET:\?")

# A service that compose.stack.yml builds from a repository next to this one.
_SIBLING_BUILD = re.compile(r"^\s*build:\s*\.\./([\w.-]+)\s*$", re.MULTILINE)

# A service entry in compose.stack.yml, such as "  media:" under services.
_SERVICE_ENTRY = re.compile(r"^  [\w.-]+:\s*$")

# A profiles key, which makes a service start only when one of its profiles is asked for.
_PROFILES = re.compile(r"^\s+profiles:", re.MULTILINE)

# The version that GitHub adds to the folder name of a release ZIP, such as "-2.1.0".
_VERSION_SUFFIX = re.compile(r"-v?(\d+(?:\.\d+)*)")

# A Discord bot token has three dot-separated parts.
_DISCORD_TOKEN = re.compile(r"[\w-]+\.[\w-]+\.[\w-]+")

# The project name at the top of a compose file, such as "name: api-media".
_PROJECT_NAME = re.compile(r"^name:\s*([\w.-]+)\s*$", re.MULTILINE)

# What the registry answers for an image that is private or not published yet.
_PULL_DENIED = re.compile(r"denied|unauthorized|authentication required", re.IGNORECASE)

DOCKER_WAIT_SECONDS = 180

# The registry that holds the image of every release.
REGISTRY = "ghcr.io"

# The page that creates a GitHub token that may only read packages, with the scope already ticked.
TOKEN_PAGE = "https://github.com/settings/tokens/new?scopes=read:packages&description=Docker+updates"

# When scheduled updates run, in the computer's local time.
UPDATE_HOUR = 4

# Marks the crontab line of a scheduled update, followed by the project name.
_CRON_MARKER = "# scheduled update of "


@dataclass(frozen=True)
class Tool:
    """A program or library that this repository needs besides its Python dependencies.

    Attributes:
        name: The name shown to the user.
        purpose: Why the repository needs it, as one or more sentences.
        manual: The page that explains how to install it by hand.
        command: The executable looked up on PATH. None means the tool is a library.
        library: The shared library looked up when command is None.
        winget: The winget package ID used on Windows.
        brew: The Homebrew arguments used on macOS.
        linux: The package name for each supported Linux package manager.
        linux_script: A shell command that installs the tool on Linux instead of a package.
            {sudo} stands for sudo, or for nothing when the script already runs as root.
    """

    name: str
    purpose: str
    manual: str
    command: str | None = None
    library: str | None = None
    winget: str | None = None
    brew: tuple[str, ...] = ()
    linux: dict[str, str] = field(default_factory=dict)
    linux_script: str | None = None


DOCKER = Tool(
    name="Docker",
    purpose="Docker runs this project and the services it uses in containers, as a server would.",
    manual="https://docs.docker.com/get-started/get-docker/",
    command="docker",
    winget="Docker.DockerDesktop",
    brew=("--cask", "docker"),
    linux_script="curl -fsSL https://get.docker.com | {sudo}sh",
)

FFMPEG = Tool(
    name="FFmpeg",
    purpose="FFmpeg processes audio and video when this project runs outside Docker.",
    manual="https://ffmpeg.org/download.html",
    command="ffmpeg",
    winget="Gyan.FFmpeg",
    brew=("ffmpeg",),
    linux={
        "apt-get": "ffmpeg",
        "dnf": "ffmpeg-free",
        "pacman": "ffmpeg",
        "zypper": "ffmpeg",
    },
)

OPUS = Tool(
    name="Opus",
    purpose=(
        "discord.py encodes voice audio with the Opus library "
        "when the bot runs outside Docker."
    ),
    manual="https://opus-codec.org/downloads/",
    library="opus",
    brew=("opus",),
    linux={
        "apt-get": "libopus0",
        "dnf": "opus",
        "pacman": "opus",
        "zypper": "libopus0",
    },
)

DENO = Tool(
    name="Deno",
    purpose=(
        "yt-dlp runs YouTube's player JavaScript with Deno "
        "when the service runs outside Docker."
    ),
    manual="https://docs.deno.com/runtime/getting_started/installation/",
    command="deno",
    winget="DenoLand.Deno",
    brew=("deno",),
    linux_script="curl -fsSL https://deno.land/install.sh | sh",
)

GIT = Tool(
    name="Git",
    purpose="Git downloads the repositories that this bot's Docker stack builds.",
    manual="https://git-scm.com/downloads",
    command="git",
    winget="Git.Git",
    brew=("git",),
    linux={"apt-get": "git", "dnf": "git", "pacman": "git", "zypper": "git"},
)

LINUX_INSTALL_COMMANDS = {
    "apt-get": ["apt-get", "install", "-y"],
    "dnf": ["dnf", "install", "-y"],
    "pacman": ["pacman", "-S", "--needed", "--noconfirm"],
    "zypper": ["zypper", "install", "-y"],
}


@dataclass(frozen=True)
class Problem:
    """A step that needs the user's attention.

    Attributes:
        what: What went wrong.
        why: What does not work because of it.
        fix: What to do next.
    """

    what: str
    why: str
    fix: str


@dataclass
class Report:
    """Collects the outcome of every step for the summary at the end.

    Attributes:
        problems: Steps that failed and need the user's attention.
        skipped: Optional steps the user declined, each with what it limits.
        required_failed: Whether a step failed that the project cannot work without.
    """

    problems: list[Problem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    required_failed: bool = False

    def ok(self, message: str) -> None:
        """Reports a finished step."""
        print(f"  [ok] {message}")

    def skip(self, message: str) -> None:
        """Reports an optional step that the user declined."""
        print(f"  [--] {message}")
        self.skipped.append(message)

    def problem(self, what: str, why: str, fix: str, *, required: bool = False) -> None:
        """Reports a failed step together with its consequence and its fix."""
        print(f"  [!!] {what}")
        print(f"       Why it matters: {why}")
        print(f"       What to do: {fix}")
        self.problems.append(Problem(what, why, fix))
        if required:
            self.required_failed = True


def section(title: str) -> None:
    """Prints the heading of a group of steps."""
    print()
    print(f"--- {title} ---")


def ask(question: str) -> bool:
    """Asks a yes or no question where pressing Enter means yes.

    Without an interactive input, such as when input is piped, the answer is no.
    Nothing is installed or started without someone agreeing to it.

    Args:
        question: The question, without the answer hint.

    Returns:
        Whether the answer was yes.
    """
    try:
        answer = input(f"  {question} [Y/n] ").strip().lower()
    except EOFError:
        print()
        return False
    return answer in ("", "y", "yes")


def run(command: list[str]) -> bool:
    """Runs a command with its output shown to the user.

    Args:
        command: The program and its arguments.

    Returns:
        Whether the command exited successfully.
    """
    print(f"  > {' '.join(command)}")
    executable = shutil.which(command[0]) or command[0]
    try:
        return subprocess.run([executable, *command[1:]], check=False).returncode == 0
    except OSError as error:
        print(f"  Could not start {command[0]}: {error}")
        return False


def sudo_prefix() -> list[str]:
    """Returns sudo when installing system packages needs it, or nothing when running as root."""
    if sys.platform != "win32" and os.geteuid() == 0:
        return []
    return ["sudo"] if shutil.which("sudo") else []


def refresh_path() -> None:
    """Adds the directories that installers write to.

    A tool installed by this script is then found without opening a new terminal.
    """
    directories: list[str] = []
    if sys.platform == "win32":
        import winreg

        # Installers change the stored PATH, which only new processes would otherwise see.
        keys = [
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        ]
        for root, subkey in keys:
            try:
                with winreg.OpenKey(root, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, "Path")
            except OSError:
                continue
            directories += os.path.expandvars(str(value)).split(os.pathsep)
    else:
        home = Path.home()
        directories += [
            str(home / ".local" / "bin"),
            str(home / ".deno" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
    current = os.environ.get("PATH", "").split(os.pathsep)
    added = [d for d in directories if d and d not in current]
    os.environ["PATH"] = os.pathsep.join(added + current)


def is_installed(tool: Tool) -> bool:
    """Returns whether a tool can be found right now."""
    if tool.command is not None:
        return shutil.which(tool.command) is not None
    return (
        tool.library is not None and ctypes.util.find_library(tool.library) is not None
    )


def linux_package_manager() -> str | None:
    """Returns the first supported package manager on this system, or None."""
    for manager in LINUX_INSTALL_COMMANDS:
        if shutil.which(manager):
            return manager
    return None


def install(tool: Tool) -> str | None:
    """Installs a tool with this system's package manager.

    Args:
        tool: The tool to install.

    Returns:
        None when the tool is installed afterward.
        Otherwise a sentence that says why it is not.
    """
    if sys.platform == "win32":
        if tool.winget is None:
            return "There is no winget package for it."
        if shutil.which("winget") is None:
            return (
                "winget, which installs it, is missing. "
                "It comes with App Installer from the Microsoft Store."
            )
        succeeded = run(["winget", "install", "--exact", "--id", tool.winget])
    elif sys.platform == "darwin":
        if not tool.brew:
            return "There is no Homebrew package for it."
        if shutil.which("brew") is None:
            return "Homebrew, which installs it, is missing. Install it from https://brew.sh first."
        succeeded = run(["brew", "install", *tool.brew])
    elif tool.linux_script is not None:
        if shutil.which("curl") is None:
            return "curl, which downloads its installer, is missing."
        sudo_words = "".join(f"{word} " for word in sudo_prefix())
        script = tool.linux_script.format(sudo=sudo_words)
        succeeded = run(["sh", "-c", script])
    else:
        manager = linux_package_manager()
        if manager is None or manager not in tool.linux:
            return (
                "No supported package manager was found. "
                "The script knows apt-get, dnf, pacman, and zypper."
            )
        sudo = sudo_prefix()
        if manager == "apt-get":
            run([*sudo, "apt-get", "update"])
        succeeded = run([*sudo, *LINUX_INSTALL_COMMANDS[manager], tool.linux[manager]])

    refresh_path()
    if is_installed(tool):
        return None
    if succeeded:
        return "It was installed, but this window cannot find it yet."
    return "The installer reported an error, shown in its output above."


def ensure_tool(tool: Tool, report: Report, *, without_it: str) -> bool:
    """Makes sure a tool is installed, offering to install it when it is missing.

    Args:
        tool: The tool to look for.
        report: The report that records the outcome.
        without_it: What does not work while the tool is missing.

    Returns:
        Whether the tool is installed.
    """
    if is_installed(tool):
        report.ok(f"{tool.name} is installed.")
        return True
    print(f"  {tool.name} was not found. {tool.purpose}")
    if not ask(f"Install {tool.name} now?"):
        report.skip(f"{tool.name} was not installed. {without_it}")
        return False
    reason = install(tool)
    if reason is None:
        report.ok(f"Installed {tool.name}.")
        return True
    if reason.startswith("It was installed"):
        fix = "Close this window, open a new terminal, and run the setup script again."
    else:
        fix = f"Install it by hand from {tool.manual} and run the setup script again."
    report.problem(f"{tool.name} could not be installed. {reason}", without_it, fix)
    return False


def tools_from_dockerfile(dockerfile: str, *, windows: bool) -> list[Tool]:
    """Lists the system tools that the Docker image installs.

    The Dockerfile is the one place that names them.
    Running outside Docker needs the same tools on the machine itself.

    Args:
        dockerfile: The text of the Dockerfile.
        windows: Whether the script runs on Windows.

    Returns:
        The tools in a fixed order.
    """
    text = dockerfile.lower()
    tools: list[Tool] = []
    if "ffmpeg" in text:
        tools.append(FFMPEG)
    # discord.py ships its own Opus library for Windows.
    if "libopus" in text and not windows:
        tools.append(OPUS)
    if "deno" in text:
        tools.append(DENO)
    return tools


def env_value(text: str, key: str) -> str | None:
    """Returns the value of an active KEY=value line in .env text.

    Args:
        text: The contents of a .env file.
        key: The variable name.

    Returns:
        The value without surrounding spaces, or None when the key is unset or commented out.
    """
    for line in text.splitlines():
        match = _ENV_LINE.match(line.strip())
        if match and not match["comment"] and match["key"] == key:
            return match["value"].strip()
    return None


def commented_value(text: str, key: str) -> str | None:
    """Returns the example value of the first commented-out KEY=value line in .env text.

    Args:
        text: The contents of a .env file.
        key: The variable name.

    Returns:
        The example value, or None when no commented-out line has the key.
    """
    for line in text.splitlines():
        match = _ENV_LINE.match(line.strip())
        if match and match["comment"] and match["key"] == key:
            return match["value"].strip()
    return None


def set_env_value(text: str, key: str, value: str) -> str:
    """Sets KEY=value in .env text, where the template put the key.

    An active line for the key is replaced.
    Otherwise the first commented-out line for the key is uncommented.
    Otherwise the line is added at the end.

    Args:
        text: The contents of a .env file.
        key: The variable name.
        value: The new value.

    Returns:
        The changed contents, ending with a newline.
    """
    lines = text.splitlines()
    for want_comment in (False, True):
        for index, line in enumerate(lines):
            match = _ENV_LINE.match(line.strip())
            if match and match["key"] == key and bool(match["comment"]) == want_comment:
                lines[index] = f"{key}={value}"
                return "\n".join(lines) + "\n"
    lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def is_placeholder(value: str | None) -> bool:
    """Returns whether a .env value is missing or still the template's placeholder."""
    return value is None or value.strip().lower() in PLACEHOLDERS


def required_stack_secrets(compose: str) -> list[tuple[str, str]]:
    """Lists the API secrets that compose.stack.yml requires, with the repository of each.

    A bot reaches the service api-<name> with the secret API_<NAME>_SECRET.

    Args:
        compose: The text of compose.stack.yml.

    Returns:
        Pairs of the variable name and the repository name, in file order without repeats.
    """
    pairs: list[tuple[str, str]] = []
    for name in _REQUIRED_STACK_SECRET.findall(compose):
        pair = (f"API_{name}_SECRET", "api-" + name.lower().replace("_", "-"))
        if pair not in pairs:
            pairs.append(pair)
    return pairs


def service_blocks(compose: str) -> list[str]:
    """Splits the services section of a compose file into one text block per service.

    Lines before the first top-level key count as services.
    So a fragment without the services key works too.
    """
    blocks: list[list[str]] = []
    in_services = True
    for line in compose.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            in_services = line.startswith("services:")
        elif in_services and _SERVICE_ENTRY.match(line):
            blocks.append([line])
        elif in_services and blocks:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


def stack_siblings(compose: str) -> list[str]:
    """Lists the repositories that compose.stack.yml always builds from the folder next to this one.

    A service with profiles only starts when one of them is asked for, such as with --profile media.
    Its repository is therefore optional and not listed.
    """
    siblings: set[str] = set()
    for block in service_blocks(compose):
        if not _PROFILES.search(block):
            siblings.update(_SIBLING_BUILD.findall(block))
    return sorted(siblings)


def sibling_url(origin: str, name: str) -> str:
    """Builds the clone URL of a repository that has the same owner as this one.

    Args:
        origin: This repository's origin URL, over HTTPS or SSH.
        name: The other repository's name.

    Returns:
        The other repository's URL in the same form as the origin.
    """
    owner, _, _ = origin.strip().rstrip("/").removesuffix(".git").rpartition("/")
    return f"{owner}/{name}.git"


def uses_published_images(root: Path) -> bool:
    """Returns whether a folder runs the images that releases publish, instead of building them.

    A downloaded release has no Git history, and its code is the code of a published image.
    A Git clone may hold changes of its own, so it builds the images from its files.
    """
    return not (root / ".git").exists()


def project_name(root: Path, compose: str) -> str:
    """Returns the project name that a compose file sets, or the folder name without one."""
    match = _PROJECT_NAME.search(compose)
    return match.group(1) if match else root.name


def sync_dependencies(report: Report) -> bool:
    """Installs the locked dependencies into .venv with uv."""
    if run(["uv", "sync"]):
        report.ok("Installed the locked dependencies into .venv.")
        return True
    report.problem(
        "uv could not install the dependencies.",
        "Nothing in this project can run or be tested without them.",
        "Read uv's error above. A lost internet connection is the most common cause. "
        "Fix it and run the setup script again.",
        required=True,
    )
    return False


def load_env_file(report: Report) -> str | None:
    """Creates .env from .env.template when it is missing and returns its contents."""
    env_file = REPO / ".env"
    if env_file.exists():
        report.ok(".env already exists, so it was kept.")
    else:
        template = REPO / ".env.template"
        if not template.exists():
            report.problem(
                ".env.template is missing, so .env could not be created.",
                "The project reads its settings from .env.",
                "Restore it with git checkout -- .env.template "
                "and run the setup script again.",
                required=True,
            )
            return None
        shutil.copyfile(template, env_file)
        report.ok("Created .env from .env.template.")
    return env_file.read_text(encoding="utf-8")


def save_env_file(text: str) -> None:
    """Writes .env with LF line endings, which is how Git checks out .env.template."""
    (REPO / ".env").write_text(text, encoding="utf-8", newline="")


def fill_api_secret(text: str, report: Report) -> str:
    """Generates API_SECRET when .env still has the placeholder."""
    if not is_placeholder(env_value(text, "API_SECRET")):
        report.ok("API_SECRET is already set in .env.")
        return text
    report.ok("Generated a random API_SECRET in .env.")
    return set_env_value(text, "API_SECRET", secrets.token_urlsafe(32))


def fill_discord_token(text: str, report: Report) -> str:
    """Asks for the bot token when .env still has the placeholder.

    The token is read without echoing it and is never printed.
    """
    if not is_placeholder(env_value(text, "DISCORD_TOKEN")):
        report.ok("DISCORD_TOKEN is already set in .env.")
        return text
    print("  The bot signs in to Discord with its token.")
    print(
        "  Find it in the Discord Developer Portal at https://discord.com/developers/applications."
    )
    print("  Open your application, go to Bot, and click Reset Token.")
    token = ""
    # getpass reads the Windows console directly, so it would wait forever on piped input.
    if sys.stdin.isatty():
        try:
            token = getpass.getpass(
                "  Paste the token here. It stays hidden. Press Enter to skip: "
            )
        except EOFError:
            token = ""
    token = token.strip()
    fix = (
        "Open .env, set DISCORD_TOKEN to the token from the Developer Portal, "
        "and run the setup script again."
    )
    if not token:
        report.problem(
            "DISCORD_TOKEN is not set.", "The bot cannot sign in to Discord.", fix
        )
        return text
    if not _DISCORD_TOKEN.fullmatch(token):
        report.problem(
            "The pasted text does not look like a bot token, so it was not saved.",
            "The bot cannot sign in to Discord.",
            fix,
        )
        return text
    report.ok("Saved DISCORD_TOKEN in .env.")
    return set_env_value(text, "DISCORD_TOKEN", token)


def fill_stack_secrets(text: str, compose: str, report: Report) -> str:
    """Sets every API secret that compose.stack.yml requires, and the URL that goes with it.

    The bot only configures a service that has both a URL and a secret.
    So a URL that the template shows as a commented-out example is turned on as well.
    Inside the stack, compose.stack.yml replaces the URL with the service's own address.
    """
    for variable, repository in required_stack_secrets(compose):
        text = _fill_stack_secret(text, variable, repository, report)
        url_variable = variable.removesuffix("_SECRET") + "_URL"
        example = commented_value(text, url_variable)
        if not env_value(text, url_variable) and example:
            text = set_env_value(text, url_variable, example)
            report.ok(
                f"Set {url_variable} in .env to {example} for running outside Docker."
            )
    return text


def _fill_stack_secret(
    text: str, variable: str, repository: str, report: Report
) -> str:
    # A service set up first already holds a secret in its own .env.
    # Reusing it keeps the bot and the service in agreement outside Docker too.
    if not is_placeholder(env_value(text, variable)):
        report.ok(f"{variable} is already set in .env.")
        return text
    service_env = REPO.parent / repository / ".env"
    existing = None
    if service_env.exists():
        existing = env_value(service_env.read_text(encoding="utf-8"), "API_SECRET")
    if is_placeholder(existing):
        report.ok(f"Generated a random {variable} in .env.")
        return set_env_value(text, variable, secrets.token_urlsafe(32))
    report.ok(f"Set {variable} in .env to API_SECRET from ../{repository}/.env.")
    return set_env_value(text, variable, existing or "")


def release_version(folder_name: str, name: str) -> tuple[int, ...] | None:
    """Reads the version from the name of a folder that may hold a copy of a repository.

    GitHub names the folder in a release ZIP after the repository and the version.
    So a download of api-media extracts to a folder such as api-media-2.1.0.

    Args:
        folder_name: The name of the folder.
        name: The repository name that compose.stack.yml expects.

    Returns:
        The version numbers, an empty tuple for the plain name, or None for another folder.
    """
    if folder_name == name:
        return ()
    if not folder_name.startswith(name):
        return None
    match = _VERSION_SUFFIX.fullmatch(folder_name[len(name) :])
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def search_places() -> list[Path]:
    """Lists the folders where an extracted release of another repository may sit.

    Windows' Extract All puts the top folder of a ZIP inside another folder of the same name.
    When this repository sits in such a wrapper, the other downloads are one level higher.
    """
    places = [REPO.parent]
    if REPO.parent.name == REPO.name:
        places.append(REPO.parent.parent)
    return places


def find_copies(name: str) -> list[Path]:
    """Finds the folders that hold a copy of a repository, the preferred one first.

    A copy may carry a version in its name and may sit inside an Extract All wrapper.
    A folder only counts when it holds a Dockerfile, so an unrelated folder is never moved.
    The plain name comes first, and otherwise the newest version.

    Args:
        name: The repository name that compose.stack.yml expects.

    Returns:
        The folders that hold the repository's files.
    """
    ranked: list[tuple[tuple[bool, tuple[int, ...]], Path]] = []
    for place in search_places():
        for folder in place.iterdir():
            version = release_version(folder.name, name)
            if version is None or not folder.is_dir():
                continue
            inner = folder / folder.name
            content = inner if inner.is_dir() else folder
            if (content / "Dockerfile").is_file():
                ranked.append(((version == (), version), content))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [content for _, content in ranked]


def move_downloaded_copies(missing: list[str], report: Report) -> list[str]:
    """Offers to move downloaded release folders to where compose.stack.yml expects them.

    Args:
        missing: The repository names that were not found.
        report: The report that records the outcome.

    Returns:
        The names that are still missing afterward.
    """
    home = search_places()[-1]
    still_missing = []
    for name in missing:
        copies = find_copies(name)
        if not copies:
            still_missing.append(name)
            continue
        copy, target = copies[0], REPO.parent / name
        shown_copy = os.path.relpath(copy, home)
        shown_target = os.path.relpath(target, home)
        newest = f", the newest of {len(copies)} copies" if len(copies) > 1 else ""
        print(f"  Found {name} in {shown_copy}{newest}.")
        if not ask(f"Move it to {shown_target}, where the Docker stack looks for it?"):
            still_missing.append(name)
            continue
        wrapper = copy.parent
        try:
            copy.rename(target)
        except OSError as error:
            report.problem(
                f"Moving {shown_copy} to {shown_target} failed: {error}",
                "The Docker stack builds it from that place.",
                "Close any window or program that has the folder open, "
                "or move it by hand, and run the setup script again.",
            )
            still_missing.append(name)
            continue
        # The empty Extract All wrapper would only confuse a later look at the folder.
        if wrapper.name == copy.name:
            with contextlib.suppress(OSError):
                wrapper.rmdir()
        report.ok(f"Moved {shown_copy} to {shown_target}.")
    return still_missing


def ensure_siblings(compose: str, report: Report) -> bool:
    """Makes sure the repositories that compose.stack.yml builds are next to this one.

    A downloaded release folder is moved into place after asking.
    A repository that is still missing is cloned when this repository is a Git clone.
    A downloaded release runs the published images, so there a missing repository is fine.

    Returns:
        Whether every repository is present afterward.
    """
    missing = [n for n in stack_siblings(compose) if not (REPO.parent / n).is_dir()]
    if not missing:
        report.ok(
            "The repositories that compose.stack.yml builds are next to this one."
        )
        return True
    missing = move_downloaded_copies(missing, report)
    if not missing:
        return True
    names = ", ".join(missing)
    # A downloaded release runs the published images, so nothing is built from these folders.
    if uses_published_images(REPO):
        report.ok(
            f"{names} runs from its published image, so its folder is optional. "
            "The folder only holds the service's own optional settings."
        )
        return True
    without_it = "The Docker stack cannot be built without them."
    were = "were" if len(missing) > 1 else "was"
    print(
        f"  compose.stack.yml builds {names} from the folder next to this one, "
        f"but it {were} not found."
    )
    manual_fix = f"Clone {names} into {REPO.parent} and run the setup script again."
    if not ensure_tool(GIT, report, without_it=without_it):
        return False
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if origin.returncode != 0:
        report.problem(
            "This repository has no origin remote, so the address of the others is unknown.",
            without_it,
            manual_fix,
        )
        return False
    if not ask(f"Clone {names} next to this repository now?"):
        report.skip(f"{names} was not cloned. {without_it}")
        return False
    for name in missing:
        if not run(
            ["git", "clone", sibling_url(origin.stdout, name), str(REPO.parent / name)]
        ):
            report.problem(f"Cloning {name} failed.", without_it, manual_fix)
            return False
        report.ok(f"Cloned {name}.")
    return True


@dataclass(frozen=True)
class Virtualization:
    """What Windows reports about running virtual machines, which Docker Desktop needs.

    Docker Desktop runs its containers in a WSL 2 virtual machine.
    That needs WSL, the Windows virtual machine feature, and virtualization in the firmware.

    Attributes:
        wsl_installed: Whether wsl --status succeeds.
        hypervisor_running: Whether the Windows hypervisor runs, which proves the rest works.
        firmware_enabled: Whether the processor reports virtualization as enabled in the firmware.
            While the hypervisor runs, Windows reports False here, so it only counts without one.
        restart_pending: Whether Windows waits for a restart to finish installing a component.
    """

    wsl_installed: bool
    hypervisor_running: bool
    firmware_enabled: bool
    restart_pending: bool


class VirtualizationStep(Enum):
    """What has to happen before Docker Desktop can start on Windows."""

    READY = "ready"
    INSTALL_WSL = "install WSL"
    RESTART = "restart"
    FIRMWARE = "firmware"


def next_virtualization_step(state: Virtualization) -> VirtualizationStep:
    """Decides what Windows still needs before Docker Desktop can start.

    Args:
        state: What Windows reports.

    Returns:
        The step to take next.
    """
    if not state.wsl_installed:
        return VirtualizationStep.INSTALL_WSL
    if state.hypervisor_running:
        return VirtualizationStep.READY
    if state.restart_pending:
        return VirtualizationStep.RESTART
    if not state.firmware_enabled:
        return VirtualizationStep.FIRMWARE
    # WSL is installed and the firmware allows virtualization, so the Windows feature is off.
    return VirtualizationStep.INSTALL_WSL


def read_virtualization() -> Virtualization:
    """Reads what Windows reports about virtualization, without needing administrator rights."""
    try:
        wsl = subprocess.run(
            ["wsl.exe", "--status"],
            capture_output=True,
            env={**os.environ, "WSL_UTF8": "1"},
            timeout=60,
            check=False,
        )
        wsl_installed = wsl.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        wsl_installed = False

    query = (
        "$c = Get-CimInstance Win32_Processor; $s = Get-CimInstance Win32_ComputerSystem; "
        '"$($c[0].VirtualizationFirmwareEnabled) $($s.HypervisorPresent)"'
    )
    try:
        cim = subprocess.run(
            ["powershell", "-NoProfile", "-Command", query],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        firmware, _, hypervisor = cim.stdout.strip().partition(" ")
    except (OSError, subprocess.TimeoutExpired):
        firmware, hypervisor = "", ""
    if not hypervisor:
        # Without an answer, Docker Desktop itself is the judge, so nothing is blocked here.
        firmware, hypervisor = "True", "True"

    restart_pending = False
    if sys.platform == "win32":
        import winreg

        key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"
        try:
            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key).Close()
            restart_pending = True
        except OSError:
            restart_pending = False

    return Virtualization(
        wsl_installed=wsl_installed,
        hypervisor_running=hypervisor == "True",
        firmware_enabled=firmware == "True",
        restart_pending=restart_pending,
    )


RESTART_FIX = (
    "Restart Windows, then run this script again. It continues where it stopped."
)


def prepare_windows_virtualization(report: Report) -> bool:
    """Makes sure Windows can run the virtual machine that Docker Desktop needs.

    Docker Desktop otherwise fails with "Virtualization support not detected".

    Returns:
        Whether Docker Desktop can start now.
    """
    without_it = "Docker Desktop cannot start, so nothing can run in Docker."
    step = next_virtualization_step(read_virtualization())
    if step is VirtualizationStep.READY:
        report.ok("Windows can run the virtual machine that Docker Desktop needs.")
        return True
    if step is VirtualizationStep.RESTART:
        report.problem(
            "Windows needs a restart to finish installing a component.",
            without_it,
            RESTART_FIX,
        )
        return False
    if step is VirtualizationStep.FIRMWARE:
        report.problem(
            "Virtualization is turned off in this computer's firmware, the BIOS or UEFI.",
            without_it,
            "In Windows, open Settings > System > Recovery and click Restart now "
            "under Advanced startup. Then choose Troubleshoot > Advanced options > "
            "UEFI Firmware Settings > Restart. In the firmware, turn on the setting called "
            "Intel Virtualization Technology, VT-x, AMD-V, or SVM Mode. "
            "Save, let Windows start, and run this script again.",
        )
        return False
    print(
        "  Docker Desktop needs WSL, the Windows Subsystem for Linux, to run containers."
    )
    print("  Turning it on also turns on the Windows feature for virtual machines.")
    if not ask(
        "Turn on WSL now? Windows asks for permission and needs a restart afterward."
    ):
        report.skip(f"WSL was not turned on. {without_it}")
        return False
    if not run(["wsl.exe", "--install", "--no-distribution"]):
        report.problem(
            "Turning on WSL failed.",
            without_it,
            "Read the error above. In PowerShell, run wsl --install --no-distribution, "
            "then restart Windows and run this script again.",
        )
        return False
    report.problem(
        "Windows needs a restart to finish turning on WSL.", without_it, RESTART_FIX
    )
    return False


WSL_CONFIG = Path.home() / ".wslconfig"

# A memory setting in .wslconfig, such as "memory=4GB".
_WSL_MEMORY = re.compile(r"^\s*memory\s*=\s*(\S+)", re.IGNORECASE)

# A section header in .wslconfig, such as "[wsl2]".
_INI_SECTION = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")

# Docker Desktop needs this much memory to build and run the images.
MIN_WSL_MEMORY_GB = 2

# The services of one project need well under 1 GB, so more only holds file caches.
MAX_SUGGESTED_WSL_MEMORY_GB = 4


def total_memory_gb() -> float | None:
    """Returns the computer's total RAM in gigabytes, or None when Windows does not say."""
    if sys.platform != "win32":
        return None
    import ctypes

    class MemoryStatus(ctypes.Structure):
        # The layout of MEMORYSTATUSEX, which GlobalMemoryStatusEx fills in.
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    total: int = status.total_physical
    return total / 1024**3


def suggested_wsl_memory_gb(total_gb: float | None) -> int:
    """Suggests a memory cap for WSL: half of the RAM, between the minimum and the maximum."""
    if total_gb is None:
        return MAX_SUGGESTED_WSL_MEMORY_GB
    half = int(total_gb // 2)
    return max(MIN_WSL_MEMORY_GB, min(MAX_SUGGESTED_WSL_MEMORY_GB, half))


def wsl_memory_setting(text: str) -> str | None:
    """Returns the memory cap in the [wsl2] section of .wslconfig text, or None without one."""
    section = ""
    for line in text.splitlines():
        header = _INI_SECTION.match(line)
        if header:
            section = header["name"].strip().lower()
            continue
        match = _WSL_MEMORY.match(line)
        if section == "wsl2" and match:
            return match.group(1)
    return None


def with_wsl_memory(text: str, size_gb: int) -> str:
    """Adds a memory cap to .wslconfig text, keeping every other line.

    Args:
        text: The current contents, which hold no memory cap yet.
        size_gb: The cap in whole gigabytes.

    Returns:
        The new contents, ending with a newline.
    """
    setting = f"memory={size_gb}GB"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        header = _INI_SECTION.match(line)
        if header and header["name"].strip().lower() == "wsl2":
            lines.insert(index + 1, setting)
            return "\n".join(lines) + "\n"
    before = "\n".join(lines).rstrip("\n")
    return (f"{before}\n\n" if before else "") + f"[wsl2]\n{setting}\n"


def read_size_answer(answer: str, suggested: int, total_gb: float | None) -> int | None:
    """Reads the answer to the memory cap question.

    Args:
        answer: What the user typed.
        suggested: The cap that pressing Enter accepts.
        total_gb: The computer's total RAM, or None when it is unknown.

    Returns:
        The cap in whole gigabytes, or None when the user declined.

    Raises:
        ValueError: The answer is not a usable size. The message says why.
    """
    text = answer.strip().lower().removesuffix("gb").strip()
    if not text:
        return suggested
    if text in ("n", "no"):
        return None
    if not text.isdigit():
        raise ValueError("Type a whole number of gigabytes, such as 6, or n to skip.")
    size = int(text)
    if size < MIN_WSL_MEMORY_GB:
        raise ValueError(f"Docker Desktop needs at least {MIN_WSL_MEMORY_GB} GB.")
    if total_gb is not None and size >= total_gb:
        raise ValueError(
            f"That is not below the {total_gb:.1f} GB that this computer has."
        )
    return size


def cap_wsl_memory(report: Report) -> None:
    """Offers to cap the memory of the WSL virtual machine that Docker Desktop runs in.

    WSL lets the virtual machine take up to half of the RAM and keeps file caches in it.
    Windows therefore shows far more memory in use than the containers need.
    A cap that .wslconfig already holds is the user's choice and stays unchanged.
    """
    try:
        text = WSL_CONFIG.read_text(encoding="utf-8") if WSL_CONFIG.exists() else ""
    except (OSError, UnicodeDecodeError) as error:
        report.skip(
            f"WSL memory was not capped, because {WSL_CONFIG} could not be read: {error}"
        )
        return
    current = wsl_memory_setting(text)
    if current is not None:
        report.ok(f"WSL memory is already capped at {current} in {WSL_CONFIG}.")
        return

    total = total_memory_gb()
    suggested = suggested_wsl_memory_gb(total)
    if total is not None:
        print(f"  This computer has {total:.1f} GB of RAM.")
        print(
            "  Docker Desktop's WSL virtual machine may take up to half of it, "
            f"about {total / 2:.1f} GB."
        )
    print(
        "  It keeps file caches there, so Windows shows more memory in use than needed."
    )
    print(
        f"  This project needs well under 1 GB, so {suggested} GB leaves plenty of room."
    )
    print(f"  The cap goes into {WSL_CONFIG} and applies to every WSL virtual machine.")
    size: int | None = None
    for _ in range(3):
        try:
            answer = input(
                f"  Cap it at {suggested} GB? Press Enter for {suggested} GB, "
                "type another number of GB, or n to skip: "
            )
        except EOFError:
            # Without an interactive input, the machine-wide setting stays unchanged.
            print()
            answer = "n"
        try:
            size = read_size_answer(answer, suggested, total)
        except ValueError as error:
            print(f"  {error}")
            continue
        break
    else:
        report.skip("WSL memory was not capped, because no usable size was given.")
        return
    if size is None:
        report.skip("WSL memory was not capped. Windows may show WSL using several GB.")
        return
    try:
        WSL_CONFIG.write_text(with_wsl_memory(text, size), encoding="utf-8")
    except OSError as error:
        report.problem(
            f"Writing {WSL_CONFIG} failed: {error}",
            "The WSL virtual machine stays without a memory cap.",
            f"Add the line memory={size}GB under [wsl2] in that file by hand.",
        )
        return
    report.ok(f"Capped WSL memory at {size} GB in {WSL_CONFIG}.")
    print("  The cap takes effect the next time Windows starts.")


def docker_info() -> subprocess.CompletedProcess[str] | None:
    """Asks the Docker engine for its status.

    Returns:
        The result of docker info, or None when the docker command could not run.
    """
    try:
        return subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def start_docker_desktop() -> bool:
    """Starts Docker Desktop in the background.

    Returns:
        Whether it could be started. Linux has no Docker Desktop to start.
    """
    if sys.platform == "win32":
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        desktop = Path(program_files) / "Docker" / "Docker" / "Docker Desktop.exe"
        if not desktop.exists():
            return False
        # A detached process keeps running after this window closes.
        subprocess.Popen(
            [str(desktop)],
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return True
    if sys.platform == "darwin":
        return run(["open", "-a", "Docker"])
    return False


def ensure_docker_running(report: Report, *, ask_first: bool = True) -> bool:
    """Makes sure the Docker engine answers, starting Docker Desktop when it does not.

    Args:
        report: The report that records the outcome.
        ask_first: Whether to ask before starting Docker Desktop.

    Returns:
        Whether Docker answers.
    """
    result = docker_info()
    if result is not None and result.returncode == 0:
        report.ok("Docker is running.")
        return True
    if result is not None and "permission denied" in result.stderr.lower():
        report.problem(
            "Docker is installed, but this user may not use it.",
            "Nothing can run in Docker from this account.",
            "Run sudo usermod -aG docker $USER, log out and back in, "
            "and run this script again.",
        )
        return False
    if sys.platform not in ("win32", "darwin"):
        report.problem(
            "Docker is installed but not running.",
            "Nothing can run in Docker until it starts.",
            "Start it with sudo systemctl enable --now docker and run this script again.",
        )
        return False
    not_running = "Docker Desktop is not running."
    fix = (
        "Start Docker Desktop from the Start menu or Applications, "
        "wait until it is ready, and run this script again."
    )
    if ask_first and not ask(f"{not_running} Start it now?"):
        report.skip(f"{not_running} Start it before using Docker.")
        return False
    if not ask_first:
        print(f"  {not_running} Starting it.")
    if not start_docker_desktop():
        report.problem(
            "Docker Desktop could not be started.", "Nothing can run in Docker.", fix
        )
        return False
    print("  Waiting for Docker to start. The first start can take a few minutes.")
    print(
        "  If Docker Desktop asks you to accept its terms or to update WSL, do so in its window."
    )
    deadline = time.monotonic() + DOCKER_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(3)
        result = docker_info()
        if result is not None and result.returncode == 0:
            report.ok("Docker is running.")
            return True
    report.problem(
        f"Docker did not become ready within {DOCKER_WAIT_SECONDS // 60} minutes.",
        "Nothing can run in Docker until it is ready.",
        "Check the Docker Desktop window for a message, such as a restart that Windows needs. "
        "If it says that virtualization support was not detected, run the setup script, "
        "which finds out why. " + fix,
    )
    return False


@dataclass(frozen=True)
class ServiceImage:
    """The image of one service, as the compose file names it.

    Attributes:
        service: The service name from the compose file.
        ref: The image name and tag, such as ghcr.io/lempki/api-media:latest.
        own: Whether it is the project's own image, which compose builds from the project folder.
    """

    service: str
    ref: str
    own: bool


def parse_config_images(config: str, root: Path) -> list[ServiceImage]:
    """Reads the image of every service from the output of docker compose config --format json.

    Args:
        config: The command's standard output.
        root: The project folder.

    Returns:
        One entry per service that names an image, in the order of the output.
    """
    services = json.loads(config).get("services", {})
    images: list[ServiceImage] = []
    for service, settings in services.items():
        ref = settings.get("image")
        if not ref:
            continue
        build = settings.get("build")
        context = build.get("context") if isinstance(build, dict) else build
        own = context is not None and Path(context).resolve() == root.resolve()
        images.append(ServiceImage(service=service, ref=ref, own=own))
    return images


def service_images(compose: list[str], root: Path) -> list[ServiceImage]:
    """Lists the images that a project's services run, or nothing when compose fails.

    Args:
        compose: The start of the docker compose command for the project.
        root: The project folder.
    """
    result = subprocess.run(
        [*compose, "config", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        print(f"  {result.stderr.strip()}")
        return []
    return parse_config_images(result.stdout, root)


def image_id(ref: str) -> str | None:
    """Returns the ID of the image that a name and tag point to on this machine, or None."""
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    image = result.stdout.strip()
    return image if result.returncode == 0 and image else None


def missing_images(compose: list[str], root: Path) -> list[str]:
    """Lists the services whose image is not on this machine yet.

    docker compose pull --policy missing downloads present images again when a newer one exists.
    So start downloads only these services, and only the update action installs new releases.
    """
    return [
        image.service
        for image in service_images(compose, root)
        if image_id(image.ref) is None
    ]


def sign_in_to_registry(report: Report) -> bool:
    """Signs Docker in to the registry with a GitHub token, which a private image needs.

    The token is read without echoing it and is never printed.
    Docker keeps the sign-in, so later downloads, scheduled updates included, need no token.

    Returns:
        Whether Docker is signed in afterward.
    """
    # Without a person at the keyboard, such as in a scheduled update, nobody can paste a token.
    if not sys.stdin.isatty():
        return False
    print(
        f"  {REGISTRY} refused to send an image, which happens when the image is private."
    )
    print("  Docker then needs a GitHub token that may read packages.")
    print(f"  Create one at {TOKEN_PAGE}")
    print("  The page has read:packages ticked, which is all the token needs.")
    print(
        "  Updates stop when the token expires, and this script then asks for a new one."
    )
    try:
        user = input(
            "  Type the GitHub user name that owns the token. Press Enter to skip: "
        ).strip()
        token = getpass.getpass("  Paste the token here. It stays hidden: ").strip()
    except EOFError:
        user, token = "", ""
    if not (user and token):
        report.skip(
            "Docker was not signed in to GitHub, so private images stay out of reach."
        )
        return False
    print(f"  > docker login {REGISTRY} --username {user} --password-stdin")
    result = subprocess.run(
        ["docker", "login", REGISTRY, "--username", user, "--password-stdin"],
        input=token,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode == 0:
        report.ok(f"Signed Docker in to {REGISTRY} as {user}.")
        return True
    print(f"  {result.stderr.strip()}")
    report.problem(
        f"Signing in to {REGISTRY} as {user} failed.",
        "Private images cannot be downloaded.",
        "Check that the token belongs to that user, has the read:packages scope, "
        "and has not expired. Then run this script again.",
    )
    return False


def pull_images(
    compose: list[str], report: Report, services: list[str] | None = None
) -> bool:
    """Downloads the newest published images, signing in when the registry asks.

    Args:
        compose: The start of the docker compose command for the project.
        report: The report that records the outcome.
        services: The services whose images to download. None downloads every service's.

    Returns:
        Whether the images are on this machine afterward.
    """
    command = [*compose, "pull", "--quiet", *(services or [])]
    print("  Downloading the images. The first download can take a few minutes.")
    signed_in = False
    while True:
        print(f"  > {' '.join(command)}")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode == 0:
            return True
        denied = bool(_PULL_DENIED.search(result.stderr))
        if not denied or signed_in:
            break
        signed_in = sign_in_to_registry(report)
        if not signed_in:
            break
    print(f"  {result.stderr.strip()}")
    if denied:
        report.problem(
            f"{REGISTRY} refused to send an image.",
            "Nothing new can start without it.",
            "Either the image is private or GitHub has not published it yet. "
            "A new release takes GitHub up to half an hour to publish, so try again later. "
            "For a private image, run this script in a window, which asks for a GitHub token.",
        )
    else:
        report.problem(
            "Downloading the images failed.",
            "Nothing new can start without them.",
            "Read the error above. A lost internet connection is the most common cause. "
            "Then run this script again.",
        )
    return False


def cron_line(root: Path, name: str) -> str:
    """Returns the crontab line that updates a project every night."""
    folder = shlex.quote(str(root))
    return (
        f"0 {UPDATE_HOUR} * * * cd {folder} && ./run.sh update < /dev/null "
        f">> update.log 2>&1 {_CRON_MARKER}{name}"
    )


def without_cron_line(crontab: str, name: str) -> str:
    """Removes a project's scheduled update from crontab text, keeping every other line."""
    lines = [
        line
        for line in crontab.splitlines()
        if not line.endswith(f"{_CRON_MARKER}{name}")
    ]
    return "".join(f"{line}\n" for line in lines)


def update_task_name(name: str) -> str:
    """Returns the name of a project's scheduled update in the Windows Task Scheduler."""
    return f"{name} update"


# Task Scheduler runs this with the window hidden, and run.bat writes into update.log.
# The values come from environment variables, so no quoting can break the command.
_REGISTER_TASK = (
    "$action = New-ScheduledTaskAction -Execute 'powershell.exe' "
    "-Argument $env:UPDATE_ARGUMENT -WorkingDirectory $env:UPDATE_FOLDER; "
    "$trigger = New-ScheduledTaskTrigger -Daily -At $env:UPDATE_TIME; "
    # A laptop that was asleep or on battery at that time still updates when it can.
    "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
    "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1); "
    "Register-ScheduledTask -TaskName $env:UPDATE_TASK -Action $action -Trigger $trigger "
    "-Settings $settings -Description $env:UPDATE_DESCRIPTION -Force | Out-Null"
)

_UNREGISTER_TASK = "Unregister-ScheduledTask -TaskName $env:UPDATE_TASK -Confirm:$false"

_FIND_TASK = (
    "if (Get-ScheduledTask -TaskName $env:UPDATE_TASK -ErrorAction SilentlyContinue) "
    "{ exit 0 } else { exit 1 }"
)

# The command that the scheduled task runs. Reading from nul answers every question with no.
UPDATE_ARGUMENT = (
    "-NoProfile -NonInteractive -WindowStyle Hidden "
    "-Command \"cmd.exe /c 'run.bat update < nul >> update.log 2>&1'\""
)


def run_task_script(script: str, root: Path, name: str) -> bool:
    """Runs a PowerShell script about a project's scheduled update and returns whether it worked."""
    env = {
        **os.environ,
        "UPDATE_ARGUMENT": UPDATE_ARGUMENT,
        "UPDATE_FOLDER": str(root),
        "UPDATE_TIME": f"{UPDATE_HOUR:02d}:00",
        "UPDATE_TASK": update_task_name(name),
        "UPDATE_DESCRIPTION": f"Installs the newest release of {name}. Created by its run script.",
    }
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 and result.stderr.strip():
        print(f"  {result.stderr.strip()}")
    return result.returncode == 0


def read_crontab() -> str | None:
    """Returns this user's crontab, an empty string without one, or None without cron."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else ""


def write_crontab(text: str) -> bool:
    """Replaces this user's crontab and returns whether it worked."""
    try:
        return (
            subprocess.run(
                ["crontab", "-"], input=text, text=True, check=False
            ).returncode
            == 0
        )
    except OSError:
        return False


def updates_scheduled(name: str) -> bool:
    """Returns whether a project already updates itself every night."""
    if sys.platform == "win32":
        return run_task_script(_FIND_TASK, REPO, name)
    crontab = read_crontab()
    return crontab is not None and crontab != without_cron_line(crontab, name)


def schedule_updates(root: Path, name: str, report: Report) -> bool:
    """Makes the computer install the newest release of a project every night.

    Returns:
        Whether the scheduled update is in place.
    """
    when = f"every night at {UPDATE_HOUR:02d}:00"
    if sys.platform == "win32":
        scheduled = run_task_script(_REGISTER_TASK, root, name)
        place = f"the Task Scheduler as {update_task_name(name)}"
    else:
        crontab = read_crontab()
        scheduled = crontab is not None and write_crontab(
            without_cron_line(crontab, name) + cron_line(root, name) + "\n"
        )
        place = "your crontab"
    if scheduled:
        report.ok(f"New releases install {when}. The update is in {place}.")
        print("  Each update writes what it did into update.log in this folder.")
        return True
    report.problem(
        "The nightly update could not be scheduled.",
        "New releases only install when you run the update action yourself.",
        "Read the error above, if any. Run run.bat update on Windows, "
        "or ./run.sh update elsewhere, whenever you want to update.",
    )
    return False


def unschedule_updates(name: str, report: Report) -> bool:
    """Stops the nightly update of a project.

    Returns:
        Whether no scheduled update is left.
    """
    if not updates_scheduled(name):
        report.ok("New releases were not installed automatically, so nothing changed.")
        return True
    if sys.platform == "win32":
        removed = run_task_script(_UNREGISTER_TASK, REPO, name)
    else:
        crontab = read_crontab()
        removed = crontab is not None and write_crontab(
            without_cron_line(crontab, name)
        )
    if removed:
        report.ok("New releases no longer install automatically.")
        return True
    report.problem(
        "The nightly update could not be removed.",
        "New releases keep installing every night.",
        f"Delete {update_task_name(name)} in the Task Scheduler, "
        "or its line in crontab -e on Linux and macOS.",
    )
    return False


def offer_scheduled_updates(name: str, report: Report) -> None:
    """Offers to install new releases automatically every night."""
    if updates_scheduled(name):
        report.ok(f"New releases already install every night at {UPDATE_HOUR:02d}:00.")
        return
    print("  An update downloads the newest release and restarts with it.")
    print("  When the new version fails to start, the previous version runs again.")
    if sys.platform == "win32":
        print(
            "  If the computer is off or asleep at that time, the update runs once it is on."
        )
    if not ask(
        f"Install new releases automatically every night at {UPDATE_HOUR:02d}:00?"
    ):
        report.skip(
            "New releases do not install automatically. "
            "Use the update action to update, or the schedule action to turn this on."
        )
        return
    schedule_updates(REPO, name, report)


STACK_COMPOSE = ["docker", "compose", "-f", "compose.stack.yml"]


def start_stack(report: Report, *, published: bool) -> bool:
    """Starts the bot and its services in Docker, after asking.

    Args:
        report: The report that records the outcome.
        published: Whether to download the published images instead of building them.

    Returns:
        Whether the stack started.
    """
    verb = "Download" if published else "Build"
    if not ask(f"{verb} and start the bot and its services in Docker now?"):
        report.skip("The Docker stack was not started.")
        return False
    if published:
        missing = missing_images(STACK_COMPOSE, REPO)
        if missing and not pull_images(STACK_COMPOSE, report, missing):
            return False
        command = [*STACK_COMPOSE, "up", "-d", "--no-build"]
    else:
        command = [*STACK_COMPOSE, "up", "-d", "--build"]
    if run(command):
        report.ok(
            "The bot and its services run in Docker. They start again whenever Docker starts."
        )
        return True
    report.problem(
        "Docker could not start the stack.",
        "The bot is not running.",
        "Read the error above, fix it, and run this script again.",
    )
    return False


def install_local_tools(report: Report) -> None:
    """Offers the tools that the Docker image installs, for running outside Docker."""
    dockerfile = REPO / "Dockerfile"
    if not dockerfile.exists():
        return
    text = dockerfile.read_text(encoding="utf-8")
    for tool in tools_from_dockerfile(text, windows=sys.platform == "win32"):
        ensure_tool(
            tool,
            report,
            without_it=(
                f"Running outside Docker fails where {tool.name} is needed. "
                "Docker still works."
            ),
        )


def print_problems(report: Report) -> None:
    """Prints every problem and skipped step again, so none scrolls out of sight."""
    if report.problems:
        print(f"  {len(report.problems)} thing(s) need your attention:")
        for number, problem in enumerate(report.problems, start=1):
            print(f"  {number}. {problem.what}")
            print(f"     Why it matters: {problem.why}")
            print(f"     What to do: {problem.fix}")
    else:
        print("  Nothing needs your attention.")
    if report.skipped:
        print("  Skipped on request:")
        for message in report.skipped:
            print(f"  - {message}")


def print_summary(report: Report, *, published: bool) -> None:
    """Prints what still needs attention and how to start the project."""
    section("Summary")
    print_problems(report)
    print()
    print("  Start it in Docker    : run.bat on Windows, ./run.sh elsewhere")
    if published:
        print("  Update it             : run.bat update, or ./run.sh update")
        print("  List every action     : run.bat help, or ./run.sh help")
        return
    print("  Run it without Docker : run.bat local, or ./run.sh local")
    print("  Run the tests         : uv run pytest")


def main() -> int:
    """Runs every step and returns the exit code for the setup script."""
    # Installers write straight to the terminal, so this script's lines must not wait in a buffer.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    os.chdir(REPO)
    report = Report()
    is_bot = (REPO / "bot.py").exists()
    stack_file = REPO / "compose.stack.yml"
    has_stack = is_bot and stack_file.exists()
    compose = stack_file.read_text(encoding="utf-8") if has_stack else ""
    published = uses_published_images(REPO)

    section("Tools")
    has_docker = ensure_tool(
        DOCKER, report, without_it="Only running outside Docker is possible."
    )
    docker_can_start = has_docker
    if has_docker and sys.platform == "win32":
        docker_can_start = prepare_windows_virtualization(report)
        cap_wsl_memory(report)
    if published:
        # Everything runs in the published images, which bring their own tools and dependencies.
        report.ok(
            "This downloaded release runs in Docker from published images, "
            "so it needs no other tools."
        )
    else:
        install_local_tools(report)
        section("Python dependencies")
        sync_dependencies(report)

    section("Settings in .env")
    original = load_env_file(report)
    has_token = False
    if original is not None:
        if is_bot:
            env_text = fill_discord_token(original, report)
            has_token = not is_placeholder(env_value(env_text, "DISCORD_TOKEN"))
            env_text = fill_stack_secrets(env_text, compose, report)
        else:
            env_text = fill_api_secret(original, report)
        # A .env that needs nothing filled in stays exactly as the user left it.
        if env_text != original:
            save_env_file(env_text)

    if has_stack:
        section("Docker stack")
        has_siblings = ensure_siblings(compose, report)
        # Docker is checked last, so Docker Desktop is not started for a stack that cannot run.
        ready = docker_can_start and has_siblings and has_token
        if ready and ensure_docker_running(report):
            started = start_stack(report, published=published)
            if started and published:
                offer_scheduled_updates(project_name(REPO, compose), report)

    print_summary(report, published=published)
    return 1 if report.required_failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print(
            "Setup was cancelled. Run the setup script again to continue where it stopped."
        )
        sys.exit(130)
