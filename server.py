"""Live dashboard API for Gmail stats and parallel Jev classification."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import classify
import gmail_api

classify.load_dotenv_files()

ROOT = Path(__file__).resolve().parent
WEB_DIST = ROOT / "web" / "dist"

app = FastAPI(title="Email triage")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = asyncio.Lock()


class ClassifyRequest(BaseModel):
    unread: bool = True
    folder: str = "primary"
    limit: int = Field(default=30, ge=0, description="0 = no cap")
    concurrency: int = Field(default=classify.DEFAULT_CONCURRENCY, ge=1, le=64)
    fetch_workers: int = Field(default=classify.DEFAULT_FETCH_WORKERS, ge=1, le=8)
    dry_run: bool = True
    apply: bool = False
    demo: bool = False
    force: bool = False
    threshold: float = Field(default=classify.DEFAULT_THRESHOLD, ge=0, le=1)


def _emit(job: dict[str, Any], event: dict[str, Any], *, persist: bool = False) -> None:
    if persist:
        job["events"].append(event)
    for queue in list(job["subscribers"]):
        queue.put_nowait(event)


def _apply_progress(job: dict[str, Any], update: classify.PipelineProgress) -> None:
    job["phase"] = update.phase
    job["listed"] = update.listed
    job["listed_estimate"] = update.listed_estimate
    job["listing_done"] = update.listing_done
    job["fetched"] = update.fetched
    job["fetch_total"] = update.fetch_total
    job["done"] = update.done
    job["total"] = max(update.total, update.listed, update.done)
    job["current"] = update.current


def _metrics(job: dict[str, Any]) -> dict[str, Any]:
    results: list[classify.Classification] = job["results"]
    elapsed_ms = int((time.perf_counter() - job["started"]) * 1000)
    counts = {"attention": 0, "review": 0, "skip": 0}
    kinds: dict[str, int] = {}
    tokens = 0
    for item in results:
        counts[item.decision] = counts.get(item.decision, 0) + 1
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
        if not item.cached:
            tokens += item.input_tokens
    done = job["done"]
    total = max(int(job.get("total") or 0), done)
    rate = (done / (elapsed_ms / 1000)) if elapsed_ms and done else 0.0
    return {
        "status": job["status"],
        "phase": job.get("phase") or job["status"],
        "dry_run": job["dry_run"],
        "wrote_gmail": job["wrote_gmail"],
        "total": total,
        "done": done,
        "listed": int(job.get("listed") or 0),
        "listed_estimate": int(job.get("listed_estimate") or 0),
        "listing_done": bool(job.get("listing_done")),
        "fetched": int(job.get("fetched") or 0),
        "fetch_total": int(job.get("fetch_total") or 0),
        "current": job.get("current") or "",
        "elapsed_ms": elapsed_ms,
        "rate": round(rate, 2),
        "concurrency": job["concurrency"],
        "fetch_workers": job["fetch_workers"],
        "input_tokens": tokens,
        "counts": counts,
        "kinds": kinds,
        "would_star": sum(
            1
            for item in results
            if item.decision == "attention"
            and (item.email.gmail_id or item.email.imap_uid)
        ),
        "cached": sum(1 for item in results if item.cached),
        "fresh": sum(1 for item in results if not item.cached),
        "error": job.get("error"),
        "folder": job.get("folder"),
        "model": job.get("model") or "",
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"ok": "true"}


@app.get("/api/gmail/stats")
def gmail_stats() -> dict[str, Any]:
    try:
        return gmail_api.mailbox_snapshot()
    except SystemExit as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/results")
def saved_results(limit: int = 500) -> dict[str, Any]:
    import store

    items = store.list_recent(threshold=classify.DEFAULT_THRESHOLD, limit=limit)
    return {
        "count": store.count(),
        "db": str(store.db_path()),
        "results": [classify.classification_dict(item) for item in items],
    }


@app.post("/api/jobs")
async def start_job(body: ClassifyRequest) -> dict[str, str]:
    job_id = uuid.uuid4().hex[:12]
    dry_run = bool(body.dry_run or not body.apply)
    job = {
        "id": job_id,
        "status": "running",
        "dry_run": dry_run,
        "apply": bool(body.apply) and not dry_run,
        "folder": body.folder,
        "concurrency": body.concurrency,
        "fetch_workers": body.fetch_workers,
        "total": 0,
        "done": 0,
        "listed": 0,
        "listed_estimate": 0,
        "listing_done": False,
        "fetched": 0,
        "fetch_total": 0,
        "phase": "starting",
        "current": "",
        "results": [],
        "events": [],
        "subscribers": [],
        "started": time.perf_counter(),
        "wrote_gmail": False,
        "error": None,
        "model": "",
    }
    async with _jobs_lock:
        _jobs[job_id] = job
    asyncio.create_task(_run_job(job, body))
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    return {
        "id": job_id,
        "metrics": _metrics(job),
        "results": [classify.classification_dict(item) for item in job["results"]],
    }


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    job["subscribers"].append(queue)

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'progress', 'metrics': _metrics(job)})}\n\n"
            seen: set[str] = set()
            for item in list(job["results"]):
                seen.add(item.email.id)
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "result",
                            "item": classify.classification_dict(item),
                            "metrics": _metrics(job),
                        }
                    )
                    + "\n\n"
                )
            if job["status"] in {"done", "error"}:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": job["status"],
                            "error": job.get("error"),
                            "metrics": _metrics(job),
                        }
                    )
                    + "\n\n"
                )
                return
            while True:
                event = await queue.get()
                item = event.get("item") or {}
                email = item.get("email") or {}
                item_id = email.get("id")
                if event.get("type") == "result" and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in {"done", "error"}:
                    return
        finally:
            if queue in job["subscribers"]:
                job["subscribers"].remove(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


async def _run_job(job: dict[str, Any], body: ClassifyRequest) -> None:
    last_progress = 0.0

    def emit_progress(*, force: bool = False) -> None:
        nonlocal last_progress
        now = time.monotonic()
        if not force and now - last_progress < 0.2:
            return
        last_progress = now
        _emit(job, {"type": "progress", "metrics": _metrics(job)})

    def on_progress(update: classify.PipelineProgress) -> None:
        _apply_progress(job, update)
        emit_progress()

    def on_result(item: classify.Classification, done: int, total: int) -> None:
        job["done"] = done
        job["total"] = max(int(job.get("total") or 0), total, done)
        job["results"].append(item)
        job["current"] = item.email.subject
        if item.model:
            job["model"] = item.model
        _emit(
            job,
            {
                "type": "result",
                "item": classify.classification_dict(item),
                "metrics": _metrics(job),
            },
        )

    try:
        job["phase"] = "listing"
        emit_progress(force=True)
        model = os.environ.get("OPENROUTER_MODEL", classify.DEFAULT_MODEL)
        if body.demo:
            emails = classify.take_limit(classify.sample_emails(), body.limit)
            job["listed"] = len(emails)
            job["total"] = len(emails)
            job["listing_done"] = True
            job["phase"] = "classifying"
            emit_progress(force=True)
            if not emails:
                job["status"] = "done"
                job["phase"] = "done"
                _emit(job, {"type": "done", "metrics": _metrics(job)}, persist=True)
                return
            stats = await classify.classify_all(
                emails,
                model=model,
                threshold=body.threshold,
                concurrency=body.concurrency,
                on_result=on_result,
                on_progress=on_progress,
                force=body.force,
            )
        else:
            stats = await classify.run_gmail_pipeline(
                unread=body.unread,
                limit=body.limit,
                since_days=None,
                folder=body.folder,
                body_limit=classify.BODY_CHAR_LIMIT,
                workers=body.fetch_workers,
                model=model,
                threshold=body.threshold,
                concurrency=body.concurrency,
                force=body.force,
                on_result=on_result,
                on_progress=on_progress,
            )
        job["results"] = stats.results
        job["done"] = len(stats.results)
        job["total"] = max(int(job.get("total") or 0), len(stats.results))
        job["listing_done"] = True
        job["model"] = stats.model
        if job["apply"] and not job["dry_run"]:
            gmail_ids = [
                item.email.gmail_id
                for item in stats.results
                if item.email.gmail_id and item.decision == "attention"
            ]
            if gmail_ids:
                job["phase"] = "writing"
                emit_progress(force=True)
                await asyncio.to_thread(
                    gmail_api.apply_attention_labels,
                    gmail_ids,
                    label=os.environ.get("GMAIL_LABEL", "Jev/Attention").strip(),
                )
                job["wrote_gmail"] = True
        job["status"] = "done"
        job["phase"] = "done"
        _emit(job, {"type": "done", "metrics": _metrics(job)}, persist=True)
    except (Exception, SystemExit) as exc:
        job["status"] = "error"
        job["phase"] = "error"
        job["error"] = str(exc)
        _emit(
            job,
            {"type": "error", "error": str(exc), "metrics": _metrics(job)},
            persist=True,
        )


if WEB_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        target = WEB_DIST / path
        if path and target.is_file():
            return FileResponse(target)
        return FileResponse(WEB_DIST / "index.html")


def serve_dashboard(*, host: str, port: int) -> None:
    import uvicorn

    classify.load_dotenv_files()
    if not WEB_DIST.is_dir():
        print("UI not built yet. Run: cd web && pnpm install && pnpm build")
    print(f"Dashboard http://{host}:{port}")
    print("Dry-run is the default; Gmail is not modified unless you enable writes.")
    uvicorn.run(app, host=host, port=port, log_level="info")
