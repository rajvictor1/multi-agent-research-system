"""Vercel Flask API for the Multi-Agent Research System.

Exposes:
  GET  /            -> landing page
  POST /api/research  -> start research job
  GET  /api/research/<job_id> -> poll job status/log/report

The real research run is async and can take 2–5 minutes, so jobs run in a
background thread and are polled by the frontend.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, Response

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv
load_dotenv(HERE / ".env")

import agents
from coordinator import (
    DOCS_DIR,
    OUTPUT_DIR,
    local_documents,
    query_files,
    run_research,
)

app = Flask(__name__)


@dataclass
class Job:
    id: str
    source: str
    status: str = "pending"  # pending | running | done | error
    log: list[str] = field(default_factory=list)
    report: str = ""
    error: str = ""
    result: Any = None
    created_at: float = field(default_factory=time.time)


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _run_job(job_id: str, source: str) -> None:
    """Run the coordinator in a thread and update the job record."""
    job = _jobs.get(job_id)
    if job is None:
        return

    def on_event(line: str) -> None:
        job.log.append(line)
        print(line, flush=True)

    try:
        job.status = "running"
        on_event(f"[{datetime.now():%H:%M:%S}] starting research: {source}")
        result = asyncio.run(run_research(
            source,
            parallel=True,
            structured_errors=True,
            broad=True,
            on_event=on_event,
        ))
        job.result = result
        job.report = result.report or ""
        job.status = "done"
        on_event(f"[{datetime.now():%H:%M:%S}] done — {result.summary()}")
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        on_event(f"[{datetime.now():%H:%M:%S}] error: {job.error}")


def _start_job(source: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = Job(id=job_id, source=source)
    with _jobs_lock:
        _jobs[job_id] = job
    thread = threading.Thread(target=_run_job, args=(job_id, source), daemon=True)
    thread.start()
    return job_id


@app.route("/")
def landing():
    static = HERE / "static" / "index.html"
    if static.exists():
        return Response(static.read_text(encoding="utf-8"), mimetype="text/html")
    return "Multi-Agent Research System API is running."


@app.route("/api/research", methods=["GET", "POST"])
def research():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        source = payload.get("source", "")
        if not source:
            return jsonify({"error": "missing source"}), 400
        job_id = _start_job(source)
        return jsonify({"job_id": job_id, "status": "pending"}), 202

    return jsonify({
        "status": "ok",
        "sample_queries": query_files(),
        "documents": local_documents(),
    })


@app.route("/api/research/<job_id>")
def research_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify({
        "job_id": job.id,
        "status": job.status,
        "log": job.log,
        "report": job.report,
        "error": job.error,
        "elapsed": round(time.time() - job.created_at, 1),
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})
