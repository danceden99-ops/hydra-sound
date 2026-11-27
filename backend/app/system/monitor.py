"""System telemetry (CPU load, Pi temperature) and upstream module update checker."""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import psutil

from app.renderers.base import RendererAdapter

logger = logging.getLogger(__name__)

THERMAL_ZONE_PATH = Path("/sys/class/thermal/thermal_zone0/temp")


def get_cpu_temperature_c() -> Optional[float]:
    """Read Raspberry Pi SoC temperature in Celsius."""
    if THERMAL_ZONE_PATH.exists():
        try:
            milli = int(THERMAL_ZONE_PATH.read_text(encoding="utf-8").strip())
            return round(milli / 1000.0, 1)
        except Exception:
            pass
    try:
        temps = psutil.sensors_temperatures()
        for entries in temps.values():
            if entries:
                return round(entries[0].current, 1)
    except Exception:
        pass
    return None


def get_system_metrics() -> Dict[str, object]:
    cpu_percent = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    return {
        "cpu_percent": round(cpu_percent, 1),
        "memory_percent": round(mem.percent, 1),
        "temperature_c": get_cpu_temperature_c(),
    }


async def check_github_latest_release(repo: str) -> Optional[str]:
    """Fetch latest release tag from GitHub API."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                url,
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code == 200:
                return resp.json().get("tag_name")
    except Exception as exc:
        logger.debug("GitHub release check failed for %s: %s", repo, exc)
    return None


def _check_apt_package_versions(pkg: str) -> tuple[Optional[str], Optional[str]]:
    """Return (installed, candidate) versions from `apt-cache policy <pkg>`."""
    try:
        res = subprocess.run(
            ["apt-cache", "policy", pkg],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return None, None
        installed: Optional[str] = None
        candidate: Optional[str] = None
        for line in res.stdout.splitlines():
            s = line.strip()
            if s.startswith("Installed:"):
                val = s.split(":", 1)[1].strip()
                installed = None if val == "(none)" else val
            elif s.startswith("Candidate:"):
                val = s.split(":", 1)[1].strip()
                candidate = None if val == "(none)" else val
        return installed, candidate
    except Exception:
        return None, None


_APT_MANAGED_PACKAGES = {
    "airplay": "shairport-sync",
    "dlna": "gmediarender",
}


def _get_binary_version(cmd: list[str]) -> Optional[str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        out = (res.stdout or res.stderr).strip()
        return out.splitlines()[0] if out else None
    except Exception:
        return None


def get_installed_module_versions(
    renderers: Dict[str, RendererAdapter],
) -> Dict[str, str]:
    """Return local installed versions for all modular renderers + CamillaDSP (offline)."""
    versions: Dict[str, str] = {}
    for rid, adapter in renderers.items():
        ver = adapter.get_installed_version()
        if not ver and rid in _APT_MANAGED_PACKAGES:
            ver, _ = _check_apt_package_versions(_APT_MANAGED_PACKAGES[rid])
        versions[rid] = ver or "not installed"
    versions["camilladsp"] = _get_binary_version(["camilladsp", "--version"]) or "not installed"
    return versions


async def check_all_module_updates(
    renderers: Dict[str, RendererAdapter],
) -> List[Dict[str, object]]:
    """Return installed vs latest upstream versions for all modular renderers + CamillaDSP."""
    results: List[Dict[str, object]] = []
    for rid, adapter in renderers.items():
        installed = adapter.get_installed_version()
        apt_pkg = _APT_MANAGED_PACKAGES.get(rid)
        if apt_pkg:
            apt_inst, apt_cand = _check_apt_package_versions(apt_pkg)
            if apt_inst and not installed:
                installed = apt_inst
            latest = f"APT {apt_cand}" if apt_cand else "APT latest"
            update_available = bool(apt_cand and (not apt_inst or apt_cand != apt_inst))
        else:
            latest = await check_github_latest_release(adapter.meta.github_repo) or "Unknown"
            update_available = bool(
                latest != "Unknown"
                and (not installed or latest.lstrip("v") not in installed)
            )
        results.append(
            {
                "id": rid,
                "display_name": adapter.meta.display_name,
                "github_repo": adapter.meta.github_repo,
                "installed_version": installed or "Not installed",
                "latest_version": latest,
                "update_available": update_available,
                "update_script": adapter.meta.update_script,
                "quality_note": adapter.meta.quality_note,
            }
        )

    camilla_installed = _get_binary_version(["camilladsp", "--version"])
    camilla_latest = await check_github_latest_release("HEnquist/camilladsp") or "Unknown"
    camilla_update = bool(
        camilla_latest != "Unknown"
        and (not camilla_installed or camilla_latest.lstrip("v") not in camilla_installed)
    )
    results.append(
        {
            "id": "camilladsp",
            "display_name": "CamillaDSP (64-bit DSP Engine)",
            "github_repo": "HEnquist/camilladsp",
            "installed_version": camilla_installed or "Not installed",
            "latest_version": camilla_latest,
            "update_available": camilla_update,
            "update_script": "scripts/modules/update_camilladsp.sh",
            "quality_note": "64-bit floating-point resampling & software volume engine.",
        }
    )
    return results


def run_script_update(script_rel_path: str) -> Dict[str, object]:
    """Execute a module update script relative to repository root."""
    script_path = Path(__file__).resolve().parents[3] / script_rel_path
    if not script_path.exists():
        return {"ok": False, "error": f"Update script not found: {script_path}"}
    try:
        res = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=1200,
        )
        return {
            "ok": res.returncode == 0,
            "stdout": res.stdout[-2000:],
            "stderr": res.stderr[-2000:],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_module_update(adapter: RendererAdapter) -> Dict[str, object]:
    """Execute the module's isolated update script."""
    return run_script_update(adapter.meta.update_script)


async def get_app_version_info(
    app_version: str = "1.0.0",
    configured_repo: str = "",
) -> Dict[str, object]:
    """Return local app version, git tag/commit, and latest GitHub Release tag."""
    repo_root = Path(__file__).resolve().parents[3]
    git_tag: Optional[str] = None
    git_commit: Optional[str] = None
    try:
        tag_res = subprocess.run(
            ["git", "-C", str(repo_root), "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if tag_res.returncode == 0:
            git_tag = tag_res.stdout.strip()
        rev_res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if rev_res.returncode == 0:
            git_commit = rev_res.stdout.strip()
    except Exception:
        pass

    github_repo = configured_repo.strip() or os.environ.get("HYDRA_GITHUB_REPO", "")
    latest_github_tag = (
        await check_github_latest_release(github_repo) if github_repo else None
    )
    resolved_version = app_version
    if git_tag and git_tag.startswith("v") and "." in git_tag:
        resolved_version = git_tag.lstrip("v").split("-", 1)[0]
    return {
        "app_version": resolved_version,
        "git_tag": git_tag or f"v{resolved_version}",
        "git_commit": git_commit or "unknown",
        "github_repo": github_repo,
        "latest_github_tag": latest_github_tag,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }


async def list_github_releases(repo: str, limit: int = 15) -> List[Dict[str, object]]:
    """Fetch available releases (or git tags fallback) from GitHub for `owner/repo`."""
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo.strip()):
        return []
    clean_repo = repo.strip()
    headers = {"Accept": "application/vnd.github+json"}
    items: List[Dict[str, object]] = []
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{clean_repo}/releases?per_page={limit}",
                headers=headers,
            )
            if resp.status_code == 200:
                items = [
                    {
                        "tag": r.get("tag_name", ""),
                        "name": r.get("name") or r.get("tag_name", ""),
                        "published_at": (r.get("published_at") or "")[:10],
                        "prerelease": bool(r.get("prerelease", False)),
                    }
                    for r in resp.json()
                    if r.get("tag_name")
                ]
            if not items:
                tag_resp = await client.get(
                    f"https://api.github.com/repos/{clean_repo}/tags?per_page={limit}",
                    headers=headers,
                )
                if tag_resp.status_code == 200:
                    items = [
                        {
                            "tag": t.get("name", ""),
                            "name": t.get("name", ""),
                            "published_at": "git tag",
                            "prerelease": False,
                        }
                        for t in tag_resp.json()
                        if t.get("name")
                    ]
            if resp.status_code == 200 or items:
                items.append(
                    {
                        "tag": "main",
                        "name": "main (Latest Commit)",
                        "published_at": "branch",
                        "prerelease": False,
                    }
                )
            return items
    except Exception as exc:
        logger.debug("Failed to list GitHub releases for %s: %s", repo, exc)
        return []


def install_github_release(repo: str, tag: str) -> Dict[str, object]:
    """Checkout a selected GitHub release tag (or `main` branch) and reinstall requirements."""
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo.strip()):
        return {"ok": False, "error": "Invalid GitHub repository format (expected owner/repo)."}
    clean_tag = tag.strip()
    if not re.match(r"^[A-Za-z0-9_.-]+$", clean_tag):
        return {"ok": False, "error": "Invalid release tag format."}

    repo_root = Path(__file__).resolve().parents[3]
    remote_url = f"https://github.com/{repo.strip()}.git"
    logger.info("Installing release %s from %s", clean_tag, repo.strip())
    try:
        fetch = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "fetch",
                "--force",
                remote_url,
                clean_tag if clean_tag == "main" else f"refs/tags/{clean_tag}:refs/tags/{clean_tag}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if fetch.returncode != 0:
            err = fetch.stderr.strip() or "git fetch failed"
            logger.error("Release install fetch failed: %s", err)
            return {"ok": False, "error": err}

        target_ref = "FETCH_HEAD" if clean_tag == "main" else f"tags/{clean_tag}"
        checkout_cmd = (
            ["git", "-C", str(repo_root), "checkout", "-f", "-B", "main", "FETCH_HEAD"]
            if clean_tag == "main"
            else ["git", "-C", str(repo_root), "checkout", "-f", target_ref]
        )
        checkout = subprocess.run(
            checkout_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if checkout.returncode != 0:
            err = checkout.stderr.strip() or "git checkout failed"
            logger.error("Release install checkout failed: %s", err)
            return {"ok": False, "error": err}

        # Ensure shell scripts remain executable after git checkout
        for pattern in ("scripts/*.sh", "scripts/modules/*.sh", "scripts/hooks/*.sh"):
            for sh_file in repo_root.glob(pattern):
                sh_file.chmod(0o755)

        pip_bin = Path("/opt/hydrasound/venv/bin/pip")
        req_file = repo_root / "backend" / "requirements.txt"
        if pip_bin.exists() and req_file.exists():
            pip_res = subprocess.run(
                [str(pip_bin), "install", "-r", str(req_file)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if pip_res.returncode != 0:
                logger.warning(
                    "pip install requirements exited %s: %s",
                    pip_res.returncode,
                    (pip_res.stderr or pip_res.stdout).strip()[-400:],
                )

        # Also update prebuilt ARM64 streaming binaries if attached to this release tag
        if clean_tag != "main" and platform.machine() == "aarch64":
            asset_url = (
                f"https://github.com/{repo.strip()}/releases/download/"
                f"{clean_tag}/hydra-sound-arm64-binaries.tar.gz"
            )
            sha_url = f"{asset_url}.sha256"
            subprocess.run(
                [
                    "bash",
                    "-c",
                    f'TMP=$(mktemp -d) && curl -fsSL "{asset_url}" -o "$TMP/hydra-sound-arm64-binaries.tar.gz" '
                    f'&& curl -fsSL "{sha_url}" -o "$TMP/hydra-sound-arm64-binaries.tar.gz.sha256" '
                    '&& (cd "$TMP" && sha256sum -c hydra-sound-arm64-binaries.tar.gz.sha256) '
                    '&& tar -xzf "$TMP/hydra-sound-arm64-binaries.tar.gz" -C /usr/local/bin '
                    '&& chmod +x /usr/local/bin/librespot /usr/local/bin/pibuz /usr/local/bin/go-librespot 2>/dev/null || true '
                    '&& ([ ! -f /usr/local/bin/libasound_module_pcm_cdsp.so ] || '
                    '(mkdir -p /usr/lib/aarch64-linux-gnu/alsa-lib && '
                    'mv -f /usr/local/bin/libasound_module_pcm_cdsp.so /usr/lib/aarch64-linux-gnu/alsa-lib/libasound_module_pcm_cdsp.so)) '
                    '&& ([ -s /usr/local/bin/go-librespot.version ] || '
                    '(curl -fsSL https://api.github.com/repos/devgianlu/go-librespot/releases/latest '
                    '| sed -n \'s/.*"tag_name": *"\\([^"]*\\)".*/\\1/p\' > /usr/local/bin/go-librespot.version || true)); '
                    'rm -rf "$TMP"',
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        return {"ok": True, "tag": clean_tag, "restart_required": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
