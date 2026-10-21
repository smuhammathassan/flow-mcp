"""Google Flow MCP server.

Drives flow.google.com in headless Chromium using the user's own Google session cookies, so an agent can
create projects, submit video prompts, wait for them, download the results and stitch them with ffmpeg.

Cookies live in a single jar (default ~/.config/flow-mcp/cookies.json, mode 600). After every successful run
the jar is rewritten with the cookies Google rotated during that run; replaying stale rotated cookies is a
likely reason sessions get signed out.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from playwright.async_api import async_playwright

HOME = Path(os.environ.get("FLOW_MCP_HOME", Path.home() / ".config" / "flow-mcp"))
JAR = HOME / "cookies.json"
BASE = "https://flow.google.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
CLIP_MARKER = re.compile(r"Clip (\d+) of (\d+)")

mcp = MCPServer(
    name="google-flow",
    instructions=(
        "Automates Google Flow (flow.google.com) video generation with the user's own account. "
        "flow_generate spends Flow credits: run it with dry_run=true first to read the cost, and only submit "
        "what the user asked for. Typical loop: flow_status -> flow_create_project -> flow_generate -> "
        "flow_wait -> flow_download -> flow_stitch. If a tool reports the session expired, ask the user for "
        "fresh cookies and pass them to flow_set_cookies."
    ),
)


class NotLoggedIn(RuntimeError):
    pass


# ---------- cookie jar ----------

def _to_playwright(c: dict) -> dict:
    """Accept both browser-extension exports (expirationDate, lowercase sameSite) and Playwright cookies."""
    ss = c.get("sameSite")
    ss = SAMESITE.get(str(ss).lower(), ss if ss in ("Lax", "Strict", "None") else "Lax")
    out = {
        "name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/"),
        "expires": c.get("expires", c.get("expirationDate", -1)) or -1,
        "httpOnly": bool(c.get("httpOnly", False)), "secure": bool(c.get("secure", False)), "sameSite": ss,
    }
    if ss == "None":
        out["secure"] = True
    return out


def _write_jar(cookies: list[dict]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)
    tmp = JAR.with_suffix(".tmp")
    tmp.write_text(json.dumps(cookies))
    os.chmod(tmp, 0o600)
    tmp.replace(JAR)


def _read_jar() -> list[dict]:
    if not JAR.exists():
        raise NotLoggedIn("No cookies saved yet. Call flow_set_cookies with a cookie export for flow.google.com.")
    return [_to_playwright(c) for c in json.loads(JAR.read_text())]


# ---------- browser session ----------

class Flow:
    """One headless browser run. Saves rotated cookies back to the jar if the run was logged in."""

    def __init__(self, cookies: list[dict] | None = None):
        self.cookies = cookies
        self.logged_in = False

    async def __aenter__(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        self.ctx = await self.browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900}, accept_downloads=True)
        await self.ctx.add_cookies(self.cookies if self.cookies is not None else _read_jar())
        self.page = await self.ctx.new_page()
        return self

    async def __aexit__(self, *exc):
        try:
            if self.logged_in:
                _write_jar(await self.ctx.cookies())
        finally:
            await self.browser.close()
            await self.pw.stop()

    async def open(self, path: str = "/", settle_ms: int = 8000):
        p = self.page
        await p.goto(BASE + path, wait_until="domcontentloaded", timeout=60000)
        await p.wait_for_timeout(settle_ms)
        if "/about" in p.url or await p.locator('a[aria-label^="Google Account"]').count() == 0:
            raise NotLoggedIn("Flow session expired or cookies are invalid. Ask the user for fresh cookies and call flow_set_cookies.")
        self.logged_in = True
        for name in ("No thanks", "Got it, dismiss onboarding message", "Dismiss announcement"):
            loc = p.get_by_role("button", name=name)
            if await loc.count() and await loc.first.is_visible():
                try:
                    await loc.first.click(timeout=2000)
                except Exception:
                    pass

    async def account(self) -> str:
        label = await self.page.locator('a[aria-label^="Google Account"]').first.get_attribute("aria-label")
        return re.sub(r"\s+", " ", (label or "").replace("Google Account:", "")).replace(", Google membership", "").strip()

    async def direct_mode(self):
        """Close Flow's own agent chat panel and switch the prompt bar out of Agent mode."""
        p = self.page
        if await p.get_by_text("Untitled session").count() and await p.get_by_text("Untitled session").first.is_visible():
            await p.mouse.click(1402, 99)
            await p.wait_for_timeout(1500)
        chip = p.get_by_role("button", name="Agent", exact=True)
        if await chip.count() and await chip.get_attribute("aria-pressed") == "true":
            await chip.click()
            await p.wait_for_timeout(1500)

    async def configure_video(self, aspect_ratio: str, duration_seconds: int, resolution: str, outputs: int, model: str) -> str:
        p = self.page
        await self.direct_mode()
        await p.get_by_role("button", name="Settings trigger").click()
        await p.wait_for_timeout(1500)

        async def pick(text: str):
            loc = p.get_by_text(text, exact=True)
            if not await loc.count():
                raise ValueError(f"Option '{text}' is not offered in Flow's settings menu.")
            await loc.last.click()
            await p.wait_for_timeout(700)

        await pick("Video")
        await p.wait_for_timeout(800)
        current = (await p.get_by_role("button", name="Select model family").inner_text()).replace("arrow_drop_down", "").strip()
        if model.lower() not in current.lower():
            await p.get_by_role("button", name="Select model family").click()
            await p.wait_for_timeout(1200)
            await pick(model)
        await pick(aspect_ratio)
        await pick(resolution)
        await pick(f"{duration_seconds}s")
        await pick(f"x{outputs}")
        cost = (await p.get_by_text("Generating will use").last.inner_text()).strip()
        await p.keyboard.press("Escape")
        await p.wait_for_timeout(800)
        return cost

    async def tiles(self) -> list[dict]:
        """Media tiles in the project grid, newest first."""
        return await self.page.evaluate("""() => [...document.querySelectorAll('flow-grid-tile-container')].map(t => {
            const txt = (t.innerText || '').replace(/\\s+/g, ' ').trim();
            const pct = txt.match(/(\\d+)%/);
            const failed = /fail|couldn.t|error|policy|unable/i.test(txt);
            return {title: t.getAttribute('aria-label') || '', progress: pct ? Number(pct[1]) : null,
                    state: failed ? 'failed' : (pct ? 'generating' : 'done'), text: txt.slice(0, 160)};
        })""")


async def _viewer_strip(p, open_first: bool = False, click: int | None = None) -> list[str]:
    """Titles of the media viewer's thumbnail strip (buttons between "Previous media" and "Next media").
    Opens the viewer from the grid when it isn't showing; optionally clicks the strip item at index `click`."""
    if "/edit/" not in p.url:  # the viewer's URL is /project/<id>/edit/<media id>
        if not open_first and click is None:
            return []
        # The tile's own element is covered by its thumbnail, so click the tile's centre point.
        box = await p.locator("flow-grid-tile-container").first.bounding_box()
        await p.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await p.wait_for_timeout(5000)
    return await p.evaluate("""k => {
        const all = [...document.querySelectorAll('button')];
        const a = all.findIndex(b => b.getAttribute('aria-label') === 'Previous media');
        const z = all.findIndex(b => b.getAttribute('aria-label') === 'Next media');
        const strip = a >= 0 && z > a ? all.slice(a + 1, z) : [];
        if (k !== null && strip[k]) strip[k].click();
        return strip.map(b => b.getAttribute('aria-label') || '');
    }""", click)


async def _close_menu(p):
    """Close the download menu if it is still open; a stray Escape would close the media viewer instead."""
    if await p.locator(".cdk-overlay-container [role=menu]").count():
        await p.keyboard.press("Escape")
    await p.wait_for_timeout(600)


def _project_path(project_id: str) -> str:
    m = re.search(r"project/([0-9a-f-]{36})", project_id)
    return f"/project/{m.group(1) if m else project_id}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "media"


def _err(e: Exception) -> dict:
    return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------- tools ----------

@mcp.tool()
async def flow_status() -> dict:
    """Check whether the saved Google Flow session is logged in, and which account and plan it uses."""
    try:
        async with Flow() as f:
            await f.open("/")
            plan = "PRO" if await f.page.get_by_text("PRO", exact=True).count() else ("ULTRA" if await f.page.get_by_text("ULTRA", exact=True).count() else "unknown")
            return {"ok": True, "logged_in": True, "account": await f.account(), "plan": plan}
    except NotLoggedIn as e:
        return {"ok": False, "logged_in": False, "error": str(e)}
    except Exception as e:
        return _err(e)


@mcp.tool()
async def flow_set_cookies(cookies_json: str) -> dict:
    """Save a fresh cookie export for flow.google.com / .google.com (JSON array, e.g. from a Cookie-Editor
    browser extension) and verify that it logs in. The previous jar is kept if the new cookies don't work."""
    try:
        cookies = [_to_playwright(c) for c in json.loads(cookies_json)]
    except Exception as e:
        return {"ok": False, "error": f"Could not parse cookies JSON: {e}"}
    try:
        async with Flow(cookies) as f:
            await f.open("/")
            account = await f.account()
        return {"ok": True, "logged_in": True, "account": account, "saved_to": str(JAR)}
    except NotLoggedIn as e:
        return {"ok": False, "logged_in": False, "error": str(e)}
    except Exception as e:
        return _err(e)


@mcp.tool()
async def flow_list_projects() -> dict:
    """List the projects on the Flow home page (id and URL)."""
    try:
        async with Flow() as f:
            await f.open("/")
            hrefs = await f.page.evaluate("""() => [...new Set([...document.querySelectorAll('a[href^="/project/"]')]
                .map(a => a.getAttribute('href')))]""")
            ids = sorted({h.split("/")[2] for h in hrefs})
            return {"ok": True, "projects": [{"id": i, "url": f"{BASE}/project/{i}"} for i in ids]}
    except Exception as e:
        return _err(e)


@mcp.tool()
async def flow_create_project(title: str = "") -> dict:
    """Create a new empty Flow project and return its id and URL. Optionally rename it to `title`."""
    try:
        async with Flow() as f:
            await f.open("/")
            await f.page.get_by_text("New project").first.click()
            await f.page.wait_for_url(re.compile(r"/project/[0-9a-f-]{36}"), timeout=30000)
            await f.page.wait_for_timeout(4000)
            if title:
                box = f.page.get_by_role("textbox", name="Editable text").first
                await box.click()
                await box.fill(title)
                await box.press("Enter")
                await f.page.wait_for_timeout(1500)
            pid = re.search(r"project/([0-9a-f-]{36})", f.page.url).group(1)
            return {"ok": True, "project_id": pid, "url": f"{BASE}/project/{pid}"}
    except Exception as e:
        return _err(e)


@mcp.tool()
async def flow_generate(prompts: list[str], project_id: str, aspect_ratio: str = "9:16", duration_seconds: int = 10,
                        resolution: str = "720p", outputs_per_prompt: int = 1, model: str = "Omni 1.1 Flash",
                        dry_run: bool = False) -> dict:
    """Submit text-to-video prompts to a Flow project, one generation per prompt, in the order given.

    SPENDS CREDITS unless dry_run is true. dry_run applies the settings and returns the per-prompt credit cost
    without submitting anything. aspect_ratio: "16:9" or "9:16". duration_seconds: 4, 6, 8 or 10.
    resolution: "360p" or "720p". model: as named in Flow, e.g. "Omni 1.1 Flash", "Veo 3.1 - Fast".
    Tip: start each prompt with "Clip N of M." so flow_download can name the files in order."""
    if not prompts:
        return {"ok": False, "error": "No prompts given."}
    try:
        async with Flow() as f:
            await f.open(_project_path(project_id))
            cost = await f.configure_video(aspect_ratio, duration_seconds, resolution, outputs_per_prompt, model)
            chip = re.sub(r"crop_\w+", "", (await f.page.get_by_role("button", name="Settings trigger").inner_text())).split()
            settings = " ".join(chip)
            if dry_run:
                return {"ok": True, "dry_run": True, "settings": settings, "cost_per_prompt": cost, "prompts": len(prompts)}
            submitted = []
            box = f.page.get_by_role("textbox", name="What do you want to create?")
            for i, prompt in enumerate(prompts, 1):
                await box.click()
                await f.page.keyboard.press("Control+A")
                await f.page.keyboard.press("Delete")
                await f.page.keyboard.insert_text(prompt)
                await f.page.wait_for_timeout(1200)
                go = f.page.get_by_role("button", name="Start generation")
                if await go.is_disabled():
                    return {"ok": False, "error": f"Flow disabled the generate button at prompt {i} (out of credits or prompt rejected).",
                            "submitted": submitted, "settings": settings, "cost_per_prompt": cost}
                await go.click()
                await f.page.wait_for_timeout(5000)
                submitted.append({"index": i, "chars": len(prompt), "starts_with": prompt[:60]})
            await f.page.wait_for_timeout(3000)
            return {"ok": True, "settings": settings, "cost_per_prompt": cost, "submitted": submitted,
                    "project_url": f"{BASE}{_project_path(project_id)}"}
    except Exception as e:
        return _err(e)


@mcp.tool()
async def flow_list_media(project_id: str) -> dict:
    """List the media in a Flow project, newest first, with each item's state (generating with % / done / failed)."""
    try:
        async with Flow() as f:
            await f.open(_project_path(project_id))
            return {"ok": True, "media": await f.tiles()}
    except Exception as e:
        return _err(e)


@mcp.tool()
async def flow_wait(project_id: str, timeout_seconds: int = 900, poll_seconds: int = 30) -> dict:
    """Wait until nothing in the project is still generating (or the timeout passes), then list the media."""
    try:
        async with Flow() as f:
            await f.open(_project_path(project_id))
            waited = 0
            while True:
                tiles = await f.tiles()
                busy = [t for t in tiles if t["state"] == "generating"]
                if not busy or waited >= timeout_seconds:
                    return {"ok": True, "finished": not busy, "waited_seconds": waited, "media": tiles}
                await asyncio.sleep(poll_seconds)
                waited += poll_seconds
                await f.page.reload(wait_until="domcontentloaded")
                await f.page.wait_for_timeout(6000)
    except Exception as e:
        return _err(e)


@mcp.tool()
async def flow_download(project_id: str, out_dir: str, overwrite: bool = False) -> dict:
    """Download every finished video in a project to out_dir (free; no credits). Files are named clipN.mp4 when the
    prompt starts with "Clip N of M", otherwise NN-<title>.mp4 in grid order (newest first)."""
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        async with Flow() as f:
            p = f.page
            await f.open(_project_path(project_id))
            tiles = [t for t in await f.tiles() if t["state"] == "done"]
            if not tiles:
                return {"ok": True, "downloaded": [], "note": "No finished media in this project."}
            titles = await _viewer_strip(p, open_first=True)
            for n, title in enumerate(titles, 1):
                await _viewer_strip(p, click=n - 1)
                await p.wait_for_timeout(3500)
                m = CLIP_MARKER.search(await p.inner_text("body"))
                name = f"clip{m.group(1)}.mp4" if m else f"{n:02d}-{_slug(title)}.mp4"
                dest = out / name
                if dest.exists() and not overwrite:
                    results.append({"title": title, "file": str(dest), "skipped": "exists"})
                    continue
                await p.get_by_role("button", name="Download media").click()
                await p.wait_for_timeout(1500)
                opts = p.locator(".cdk-overlay-container [role=menuitem], .cdk-overlay-container button")
                texts = [re.sub(r"\s+", " ", await opts.nth(k).inner_text()) for k in range(await opts.count())]
                choice = next((k for k, t in enumerate(texts) if "720" in t or "Original" in t), None)
                if choice is None:
                    results.append({"title": title, "error": f"No original-size option; menu offered {texts}. Nothing clicked."})
                    await _close_menu(p)
                    continue
                async with p.expect_download(timeout=120000) as dl:
                    await opts.nth(choice).click()
                await (await dl.value).save_as(dest)
                results.append({"title": title, "file": str(dest), "bytes": dest.stat().st_size})
                await _close_menu(p)
            return {"ok": True, "downloaded": results}
    except Exception as e:
        return {**_err(e), "downloaded": results}


@mcp.tool()
async def flow_stitch(files: list[str], output_path: str) -> dict:
    """Join video files end to end (in the given order) into one MP4 with ffmpeg. All inputs must have audio."""
    if not shutil.which("ffmpeg"):
        return {"ok": False, "error": "ffmpeg is not installed."}
    paths = [str(Path(x).expanduser()) for x in files]
    missing = [x for x in paths if not Path(x).exists()]
    if missing:
        return {"ok": False, "error": f"Missing files: {missing}"}
    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    inputs = [a for x in paths for a in ("-i", x)]
    graph = "".join(f"[{i}:v][{i}:a]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=1[v][a]"
    cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    if proc.returncode:
        return {"ok": False, "error": proc.stderr[-1500:]}
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True).stdout.strip()
    return {"ok": True, "output": str(out), "duration_seconds": float(dur or 0), "bytes": out.stat().st_size}


if __name__ == "__main__":
    mcp.run("stdio")
