"""مهام خلفية قصيرة مع تتبع التقدم (بدون Celery/Redis).

مصمم لنشر أحادي: قاموس داخل الذاكرة + خيوط daemon.
كل مهمة: total/done لحساب النسبة، stage لعرض المرحلة،
result_bytes + filename للملفات الجاهزة للتنزيل.
"""
from __future__ import annotations

import threading
import time
import uuid

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_TTL_SECONDS = 30 * 60


def create_job(kind: str, total: int = 0, label: str = "") -> str:
    job_id = uuid.uuid4().hex
    now = time.time()
    with _LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "label": label,
            "status": "running",  # running | done | error
            "total": int(total or 0),
            "done": 0,
            "stage": "بدء...",
            "created": 0,
            "errors_count": 0,
            "error_list": [],
            "message": "",
            "result_bytes": None,
            "filename": "",
            "updated_at": now,
        }
        _cleanup_locked(now)
    return job_id


def update_job(job_id: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()


def progress_job(job_id: str, done: int, stage: str = "", errors_count: int | None = None) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["done"] = int(done)
        if stage:
            job["stage"] = stage
        if errors_count is not None:
            job["errors_count"] = int(errors_count)
        job["updated_at"] = time.time()


def finish_job(job_id: str, message: str = "", result_bytes: bytes | None = None, filename: str = "") -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "done"
        job["message"] = message
        job["result_bytes"] = result_bytes
        job["filename"] = filename
        job["updated_at"] = time.time()


def fail_job(job_id: str, message: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "error"
        job["message"] = message
        job["updated_at"] = time.time()


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        return dict(_JOBS.get(job_id)) if job_id in _JOBS else None


def take_result(job_id: str) -> tuple[bytes | None, str]:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None, ""
        data = job.get("result_bytes")
        return data, job.get("filename", "")


def public_job(job_id: str) -> dict | None:
    job = get_job(job_id)
    if not job:
        return None
    total = job["total"] or 0
    done = job["done"] or 0
    percent = round(done / total * 100) if total > 0 else (100 if job["status"] == "done" else 0)
    return {
        "id": job["id"],
        "kind": job["kind"],
        "label": job["label"],
        "status": job["status"],
        "total": total,
        "done": done,
        "percent": percent,
        "stage": job["stage"],
        "created": job["created"],
        "errors_count": job["errors_count"],
        "error_list": job["error_list"][-10:],
        "message": job["message"],
        "has_file": bool(job["result_bytes"]),
    }


def _cleanup_locked(now: float) -> None:
    expired = [jid for jid, j in _JOBS.items() if now - j.get("updated_at", now) > _TTL_SECONDS]
    for jid in expired:
        _JOBS.pop(jid, None)
