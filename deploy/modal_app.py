"""ORCA on Modal: the FastAPI app, its data cache, and a refresh schedule.

Deploy:

    pip install modal
    modal token set --token-id "$modal_token_id" --token-secret "$modal_token_secret"
    modal secret create orca-llm \
        OPENCODE_API_KEY=... OPENCODE_GO_API_KEY=... GROQ_API_KEY=... ORCA_TIER=paid
    modal deploy deploy/modal_app.py

Then seed the cache once, so the first question has data to answer from:

    modal run deploy/modal_app.py::refresh

---------------------------------------------------------------------------
THE TWO THINGS THIS FILE HAS TO GET RIGHT
---------------------------------------------------------------------------

**One container.** ``/replay`` swaps the cache directory process-globally and
``/chat`` answers 503 while a replay holds the lock. That lock is a Python
object, so it is per-process: two containers would each hold their own, and
a replay in one would silently not fence questions arriving at the other --
which is a fisherman being handed cyclone-replay conditions as though they
were today's. ``max_containers=1`` keeps the deployment inside the constraint
the code was written under. It is stated in ``docs/HANDOFF.md`` §4 as
"single-worker deployments only"; this is what honouring it looks like in a
serverless runtime that would otherwise scale out on its own.

Concurrency *within* the container is fine and wanted -- ``run_turn`` already
runs its tools in a thread pool and ``tests/test_stream.py`` covers two
overlapping streaming turns.

**A live cache, not a frozen one.** The tools never fetch during a question;
everything is read from ``data/``, refreshed on a schedule. Baking that
directory into the image would freeze it at build time, so a deployment left
alone for a week would answer from week-old forecasts -- honestly labelled by
``/readiness``, but useless. The cache lives in a Volume and a scheduled
function refreshes it, which is the same contract as the laptop this was
developed on, minus the human remembering.

The image still carries a copy as a seed, seen only when the Volume is empty
on a first deploy. A system whose first question fails is a system nobody
tries twice.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import modal

APP_NAME = "orca"

#: The shared-secret header. Lowercase because HTTP header names are
#: case-insensitive and Starlette normalises them on the way in.
AUTH_HEADER = "x-orca-key"
REPO = Path(__file__).resolve().parent.parent

#: Where the app expects its cache. The ingest modules resolve it relative to
#: the repo root (``parents[2] / "data"``), so this path is not configurable
#: from outside -- the Volume has to land exactly here.
DATA = "/root/orca/data"
SEED = "/root/orca-seed-data"

cache = modal.Volume.from_name("orca-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .uv_pip_install(
        # The base five, from pyproject.
        "pydantic>=2.9",
        "pyyaml>=6.0",
        "fastapi>=0.115",
        "uvicorn[standard]>=0.30",
        "httpx>=0.27",
        # The geo extra. geofence_check and the route grid need these, and a
        # geofence answer is the one a fisherman gets arrested over.
        "shapely>=2.0",
        "pyproj>=3.6",
    )
    .env({"PYTHONPATH": "/root/orca", "PYTHONUNBUFFERED": "1"})
    # Source only. `data/` is gitignored and ships separately as a seed, and
    # the frontend is deployed elsewhere.
    .add_local_dir(REPO / "agents", "/root/orca/agents")
    .add_local_dir(REPO / "alerts", "/root/orca/alerts")
    .add_local_dir(REPO / "config", "/root/orca/config")
    .add_local_dir(REPO / "core", "/root/orca/core")
    .add_local_dir(REPO / "ingest", "/root/orca/ingest")
    .add_local_dir(REPO / "language", "/root/orca/language")
    .add_local_dir(REPO / "orchestrator", "/root/orca/orchestrator")
    .add_local_dir(REPO / "rag", "/root/orca/rag")
    .add_local_dir(REPO / "scripts", "/root/orca/scripts")
    .add_local_dir(REPO / "tools", "/root/orca/tools")
    .add_local_dir(REPO / "fixtures", "/root/orca/fixtures")
    .add_local_dir(REPO / "data", SEED)
)

app = modal.App(APP_NAME, image=image)

#: Provider keys and the tier. Created out of band -- never read from .env at
#: deploy time, because that would bake a key into an image layer.
secrets = [modal.Secret.from_name("orca-llm")]


def _seed_cache_if_empty() -> None:
    """Copy the shipped cache into the Volume the first time only.

    Never overwrites. Once the scheduled refresh has run, the Volume holds
    fresher data than the image does, and a seed that clobbered it would
    quietly walk the deployment backwards on every cold start.
    """
    data = Path(DATA)
    data.mkdir(parents=True, exist_ok=True)
    if any(data.iterdir()):
        return
    seed = Path(SEED)
    if not seed.exists():
        return
    for child in seed.iterdir():
        target = data / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)
    cache.commit()


@app.function(
    secrets=secrets,
    volumes={DATA: cache},
    # See the module docstring: the replay lock is per-process, so more than
    # one container breaks the fence that stops replay conditions being
    # served as live ones.
    max_containers=1,
    timeout=600,
    # A cold start pays for the model chain's first handshake; keeping a
    # container warm is the difference between a demo that answers and a demo
    # that waits.
    min_containers=1,
)
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def api():
    """The same FastAPI app the CLI and the laptop run. No fork, no variant."""
    _seed_cache_if_empty()

    import asyncio
    import hmac
    import time

    from fastapi import Request
    from fastapi.responses import JSONResponse

    from orchestrator.main import app as orca

    # A Volume mount is a snapshot. `min_containers=1` keeps this container
    # alive for days, so without an explicit reload it serves the files it saw
    # at start-up forever -- and the six-hourly refresh, running in a
    # *different* container, would write fresh forecasts that nobody ever
    # read. Caught by deploying it: after a successful refresh, /readiness
    # still reported the seed cache's age.
    #
    # Reload is cheap but not free, so it is rate-limited rather than run per
    # request. Fifteen minutes is well inside the 12 h staleness gate
    # /readiness enforces, so the served cache can never be a refresh cycle
    # behind in a way a user could notice.
    reload_every_s = 900.0
    last_reload = [0.0]
    reload_lock = asyncio.Lock()

    @orca.middleware("http")
    async def refresh_volume_view(request: Request, call_next):
        now = time.monotonic()
        if now - last_reload[0] > reload_every_s:
            async with reload_lock:
                # Re-check inside the lock: a burst of requests arrives
                # together at page load, and without this every one of them
                # would queue its own reload behind the first.
                if time.monotonic() - last_reload[0] > reload_every_s:
                    last_reload[0] = time.monotonic()
                    try:
                        # OFF the event loop. Volume.reload() is synchronous
                        # I/O, and calling it directly from async middleware
                        # stalled every other request in flight -- the
                        # handlers still completed (Modal logged 200) but the
                        # connections died before their bodies reached the
                        # browser, which saw net::ERR_FAILED. Found by a page
                        # whose three start-up calls failed while the server
                        # insisted it had served all three.
                        await asyncio.to_thread(cache.reload)
                    except Exception as exc:  # noqa: BLE001 -- a stale view beats a 500
                        print(f"volume reload failed, serving previous view: {exc}")
        return await call_next(request)

    # ---------------------------------------------------------------
    # Shared-secret gate
    # ---------------------------------------------------------------
    # This endpoint spends real money. Every /chat call walks the paid Spark
    # chain, and the URL is in a public repo, so without this anyone reading
    # the repository can run up the bill.
    #
    # CORS is not a substitute and never was: it restrains browsers, and the
    # traffic worth worrying about is curl.
    #
    # Honest about what this is and is not. The frontend has to send the key,
    # so it ships in the client bundle and anyone who opens devtools can read
    # it. That is understood and accepted -- the threat being defended
    # against is drive-by traffic and scraped URLs, not a determined reader.
    # Modal's own proxy auth is the answer if that changes.
    api_key = os.environ.get("ORCA_API_KEY", "").strip()

    # Liveness only. These cost nothing to serve and are how anyone checks
    # the deployment is up, including Modal itself.
    OPEN_PATHS = {"/health", "/readiness"}

    @orca.middleware("http")
    async def require_key(request: Request, call_next):
        if request.method == "OPTIONS" or request.url.path in OPEN_PATHS:
            return await call_next(request)
        if not api_key:
            # Fail CLOSED. An auth control that quietly does nothing when
            # misconfigured is worse than none, because it is believed in.
            # The message is deliberately actionable: this is an operator
            # error, and it should take one reading to fix rather than a
            # debugging session.
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "ORCA_API_KEY is not set on this deployment, so the "
                        "shared-secret gate cannot be enforced and requests "
                        "are refused rather than served unprotected. Set it "
                        "on the orca-llm secret and redeploy."
                    )
                },
            )
        # compare_digest rather than ==. The timing signal here is not a
        # realistic attack, but constant-time comparison of a secret costs
        # one import and is the habit worth having.
        offered = request.headers.get(AUTH_HEADER, "")
        if not hmac.compare_digest(offered, api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": f"missing or invalid {AUTH_HEADER}"},
            )
        return await call_next(request)

    # CORS is NOT configured here. orchestrator/main.py already installs
    # CORSMiddleware, and adding a second layer meant two middlewares racing
    # to set the same header on every response. main.py reads
    # ORCA_ALLOWED_ORIGINS itself, so this deployment gets its restriction
    # from the same secret without a second stack entry.
    return orca


@app.function(
    secrets=secrets,
    volumes={DATA: cache},
    timeout=1800,
    # Every six hours. The weather cache is gated at 12 h by `/readiness`, so
    # this refreshes twice inside its own staleness window -- one failed run
    # does not put the deployment into a degraded state.
    schedule=modal.Period(hours=6),
)
def refresh():
    """Re-fetch the forecast and advisory caches into the Volume.

    Deliberately not the satellite layers: those are large, change daily
    rather than hourly, and a failure there must not take the weather refresh
    down with it. Run `--ocean` by hand when the chlorophyll goes stale.
    """
    import subprocess

    _seed_cache_if_empty()
    done = subprocess.run(
        ["python", "scripts/refresh_cache.py", "--weather", "--alerts"],
        cwd="/root/orca",
        capture_output=True,
        text=True,
        check=False,
    )
    print(done.stdout[-4000:])
    if done.returncode != 0:
        print("STDERR:", done.stderr[-4000:])
    # Commit whatever landed. A partial refresh is better than none, and
    # `/readiness` reports per-layer ages so a half-filled cache is visible
    # rather than silently averaged into "ready".
    cache.commit()
    return done.returncode


@app.local_entrypoint()
def main():
    """`modal run deploy/modal_app.py` -- seed the cache and report."""
    code = refresh.remote()
    print("refresh exit code:", code)
