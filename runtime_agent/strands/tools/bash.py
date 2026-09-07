"""Agent bash tool — runs commands under workspace.ARTIFACTS_DIR.

Threat model / shell=True rationale:
  This tool is intentionally a general shell for the agent (pipes, redirects,
  &&/||, globs). shell=False + shlex.split cannot express that surface.
  Mitigations: cwd fixed to workspace.ARTIFACTS_DIR, timeout, captured stdout/stderr,
  structured OSError handling. Do not expose this tool to untrusted end-users
  without an allowlist or sandbox.
"""

import logging
import os
import subprocess

from strands import tool

import tools.workspace as workspace
from tools.workspace import WORKING_DIR, REPO_ROOT, ARTIFACTS_REL

logger = logging.getLogger("strands-agent")

BASH_TIMEOUT_SECONDS = 300


def _ensure_cli_scripts_on_path() -> None:
    """Prepend pip user script dir so CLIs (e.g. browser-use) resolve in subprocess."""
    import site
    import sysconfig

    extra: list[str] = []
    user_base = getattr(site, "USER_BASE", None)
    if user_base:
        user_bin = os.path.join(user_base, "bin")
        if os.path.isdir(user_bin):
            extra.append(user_bin)
    try:
        scripts = sysconfig.get_path("scripts")
        if scripts and os.path.isdir(scripts):
            extra.append(scripts)
    except Exception:
        pass
    path = os.environ.get("PATH", "")
    parts = [p for p in path.split(os.pathsep) if p]
    for d in reversed(extra):
        if d and d not in parts:
            parts.insert(0, d)
    os.environ["PATH"] = os.pathsep.join(parts)


def _ensure_user_site_on_sys_path() -> None:
    """Expose pip --user site-packages to this process (and PYTHONPATH for children).

    Runtime runs as appuser, so `pip install` lands under ~/.local. site.py only
    adds USER_SITE at interpreter startup if that directory already exists; packages
    installed later (bash/execute_code) stay invisible until we addsitedir here.
    """
    import site

    try:
        user_site = site.getusersitepackages()
    except Exception:
        return
    if not user_site or not os.path.isdir(user_site):
        return

    # addsitedir also honors .pth files; skip if already on path
    if user_site not in sys.path:
        site.addsitedir(user_site)

    py_path = os.environ.get("PYTHONPATH", "")
    parts = [p for p in py_path.split(os.pathsep) if p]
    if user_site not in parts:
        parts.insert(0, user_site)
        os.environ["PYTHONPATH"] = os.pathsep.join(parts)


class _UserSiteRefreshFinder:
    """Refresh USER_SITE on import so mid-exec `pip install` + `import` works."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        import site

        try:
            user_site = site.getusersitepackages()
        except Exception:
            return None
        if user_site and os.path.isdir(user_site) and user_site not in sys.path:
            _ensure_user_site_on_sys_path()
        return None


def _install_user_site_import_hook() -> None:
    if any(isinstance(f, _UserSiteRefreshFinder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _UserSiteRefreshFinder())


_install_user_site_import_hook()


@tool
def bash(command: str) -> str:
    """Execute a bash command from artifacts/ and return the result.

    Working directory is workspace.ARTIFACTS_DIR. Save outputs by filename only
    (e.g. node create_skills_doc.js, output.docx). Skill scripts must use
    $WORKING_DIR/skills/... (not a relative skills/ path).
    """
    logger.info(f"###### bash: {command} ######")
    if not isinstance(command, str) or not command.strip():
        return "Error: empty command"

    _ensure_cli_scripts_on_path()
    _ensure_user_site_on_sys_path()
    try:
        os.makedirs(workspace.ARTIFACTS_DIR, exist_ok=True)
    except OSError as e:
        logger.warning("bash: failed to create artifacts dir: %s", e)
        return f"Error: could not create artifacts directory ({type(e).__name__})"
    env = {
        **os.environ,
        "REPO_ROOT": REPO_ROOT,
        "WORKING_DIR": WORKING_DIR,
        "ARTIFACTS_DIR": workspace.ARTIFACTS_DIR,
        "ARTIFACTS_REL": ARTIFACTS_REL,
        "USER_SKILLS_DIR": workspace.USER_SKILLS_DIR,
        "SKILLS_DIR": workspace.SKILLS_DIR,
    }
    try:
        # Intentional agent shell tool; shell=True required for pipes/redirects/&&/globs.
        # cwd pinned to workspace.ARTIFACTS_DIR, 300s timeout, captured I/O. See module docstring
        # threat model. Inline suppressions are kept on the exact flagged lines so the
        # scanner honors them (adjacent-line placement is unreliable across tools).
        result = subprocess.run(  # nosec B602  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            command,
            shell=True,  # nosec B602  # nosemgrep: python.lang.security.audit.subprocess-shell-true
            capture_output=True,
            text=True,
            cwd=workspace.ARTIFACTS_DIR,
            timeout=BASH_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.warning("bash timed out after %ss: %s", BASH_TIMEOUT_SECONDS, command[:200])
        return f"Error: command timed out after {BASH_TIMEOUT_SECONDS}s"
    except FileNotFoundError:
        logger.warning("bash: shell or command not found")
        return "Error: shell or command not found"
    except PermissionError:
        logger.warning("bash: permission denied for command")
        return "Error: permission denied"
    except OSError as e:
        logger.warning("bash OSError: %s", e)
        return f"Error: OS error ({type(e).__name__})"

    # pip install may have just created ~/.local/.../site-packages
    _ensure_user_site_on_sys_path()
    parts = []
    if result.stdout:
        parts.append(f"STDOUT:\n{result.stdout}")
    if result.stderr:
        parts.append(f"STDERR:\n{result.stderr}")
    if result.returncode != 0:
        parts.append(f"Return code: {result.returncode}")
    return "\n".join(parts) if parts else "(no output)"
