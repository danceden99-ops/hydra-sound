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


async def check_all_module_updates(
    renderers: Dict[str, RendererAdapter],
) -> List[Dict[str, object]]:
    """Return installed vs latest upstream versions for all modular renderers."""
    results: List[Dict[str, object]] = []
    for rid, adapter in renderers.items():
        installed = adapter.get_installed_version()
        latest = await check_github_latest_release(adapter.meta.github_repo)
        update_available = bool(
            latest and (not installed or latest.lstrip("v") not in installed)
        )
        results.append(
            {
                "id": rid,
                "display_name": adapter.meta.display_name,
                "github_repo": adapter.meta.github_repo,
                "installed_version": installed or "Not installed",
                "latest_version": latest or "Unknown",
                "update_available": update_available,
                "update_script": adapter.meta.update_script,
                "quality_note": adapter.meta.quality_note,
            }
        )
    return results


def run_module_update(adapter: RendererAdapter) -> Dict[str, object]:
    """Execute the module's isolated update script."""
    script_path = Path(__file__).resolve().parents[3] / adapter.meta.update_script
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
    return {
        "app_version": app_version,
        "git_tag": git_tag or f"v{app_version}",
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
    try:
        fetch = subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "--tags", remote_url, clean_tag if clean_tag == "main" else f"refs/tags/{clean_tag}:refs/tags/{clean_tag}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if fetch.returncode != 0:
            return {"ok": False, "error": fetch.stderr.strip() or "git fetch failed"}

        target_ref = "FETCH_HEAD" if clean_tag == "main" else f"tags/{clean_tag}"
        checkout_cmd = (
            ["git", "-C", str(repo_root), "checkout", "-B", "main", "FETCH_HEAD"]
            if clean_tag == "main"
            else ["git", "-C", str(repo_root), "checkout", target_ref]
        )
        checkout = subprocess.run(
            checkout_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if checkout.returncode != 0:
            return {"ok": False, "error": checkout.stderr.strip() or "git checkout failed"}

        pip_bin = Path("/opt/hydrasound/venv/bin/pip")
        req_file = repo_root / "backend" / "requirements.txt"
        if pip_bin.exists() and req_file.exists():
            subprocess.run(
                [str(pip_bin), "install", "-r", str(req_file)],
                capture_output=True,
                text=True,
                timeout=120,
            )
        return {"ok": True, "tag": clean_tag, "restart_required": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
