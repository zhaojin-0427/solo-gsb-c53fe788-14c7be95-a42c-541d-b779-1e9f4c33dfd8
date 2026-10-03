"""FastAPI application: 印前色彩软打样台."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from . import pipeline as pl
from . import regions as regions_mod
from . import releases
from . import retouch
from .release_api import router as release_router
from .config import (
    DB_PATH,
    JOB_DIR,
    MAX_DIMENSION,
    MAX_UPLOAD_MB,
    init_state,
)
from .profiles import ProfileError


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = init_state()
    releases.ensure_dirs()
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    for rec in state.pm.list_profiles():
        db.upsert_profile(conn, rec)
    app.state.db = conn
    app.state.state = state
    yield
    conn.close()


app = FastAPI(title="印前色彩软打样台", version="1.1.0", lifespan=lifespan)
app.include_router(release_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def job_dir(job_id: str) -> Path:
    d = JOB_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_job_arrays(job_id: str) -> dict:
    base = JOB_DIR / job_id
    out = {}
    for name in ("lab_src", "lab_press", "cmyk", "de", "heatmap", "mask3"):
        out[name] = np.load(base / f"{name}.npy", mmap_mode="r")
    meta = json.loads((base / "meta.json").read_text("utf-8"))
    out["meta"] = meta
    return out


# ---------------------------------------------------------------------------
# API: health / profiles
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/profiles")
def list_profiles():
    state = app.state.state
    records = state.pm.list_profiles()
    for rec in records:
        db.upsert_profile(app.state.db, rec)
    groups = {"source": [], "press": [], "display": []}
    for rec in records:
        if rec.role in groups:
            d = rec.to_dict()
            groups[rec.role].append(d)
    for items in groups.values():
        items.sort(key=lambda d: (not d["builtin"], d["name"]))
    return groups


@app.post("/api/profiles/{role}")
async def upload_profile(role: str, file: UploadFile = File(...)):
    if role not in ("source", "press", "display"):
        raise HTTPException(400, "配置角色必须是 source / press / display")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "ICC 配置文件过大（上限 10MB）")
    state = app.state.state
    try:
        rec = state.pm.save_upload(role, file.filename or "uploaded.icc", data)
    except ProfileError as exc:
        raise HTTPException(422, str(exc))
    db.upsert_profile(app.state.db, rec)
    return rec.to_dict()


# ---------------------------------------------------------------------------
# API: run pipeline
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(
    image: UploadFile = File(...),
    source_profile: str = Form(...),
    press_profile: str = Form(...),
    display_profile: str = Form(...),
    source_intent: str = Form("relative_colorimetric"),
    proof_intent: str = Form("relative_colorimetric"),
    threshold: float = Form(3.0),
    use_embedded: bool = Form(False),
):
    data = await image.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"图像超过上限 {MAX_UPLOAD_MB}MB")

    state = app.state.state
    inputs = pl.PipelineInputs(
        image_data=data,
        source_profile_id=source_profile,
        press_profile_id=press_profile,
        display_profile_id=display_profile,
        source_intent=source_intent,
        proof_intent=proof_intent,
        threshold=threshold,
        use_embedded=use_embedded,
        max_dimension=MAX_DIMENSION,
    )
    try:
        result = pl.run_pipeline(inputs, state.pm)
    except (pl.PipelineError, ProfileError) as exc:
        raise HTTPException(422, str(exc))

    job_id = pl.compute_job_id(result)
    base = job_dir(job_id)

    # Persist pixel arrays for region inspection at full double precision so
    # inspection and report statistics come from exactly the same Lab/ΔE
    # values that decided mask membership.
    np.save(base / "lab_src.npy", result.lab_src.astype(np.float64))
    np.save(base / "lab_press.npy", result.lab_press.astype(np.float64))
    np.save(base / "cmyk.npy", result.cmyk.astype(np.uint8))
    np.save(base / "de.npy", result.de.astype(np.float64))
    np.save(base / "heatmap.npy", result.heatmap.astype(np.uint8))
    np.save(base / "mask3.npy", result.mask.astype(np.uint8))
    (base / "original.bin").write_bytes(data)

    display_icc_bytes = state.pm.load_bytes(display_profile)
    pngs = pl.export_pngs(result, state.srgb_bytes, display_icc_bytes)
    for name, blob in pngs.items():
        (base / name).write_bytes(blob)

    report = pl.build_report(result, job_id)
    (base / "report.json").write_text(
        json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2), "utf-8"
    )

    meta = {
        "job_id": job_id,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "mode": result.mode,
        "image_sha256": result.image_sha,
        "embedded_sha256": result.embedded_sha,
        "source_profile": source_profile,
        "press_profile": press_profile,
        "display_profile": display_profile,
        "source_intent": source_intent,
        "proof_intent": proof_intent,
        "threshold": threshold,
        "use_embedded": use_embedded,
    }
    (base / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")

    db.insert_job(app.state.db, {
        "job_id": job_id,
        "image_sha256": result.image_sha,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "image_mode": result.mode,
        "source_profile": source_profile if not use_embedded else "<embedded>",
        "press_profile": press_profile,
        "display_profile": display_profile,
        "source_intent": source_intent,
        "proof_intent": proof_intent,
        "threshold": threshold,
        "use_embedded": use_embedded,
        "embedded_sha256": result.embedded_sha,
        "parameters": report["parameters"],
        "profile_digests": {k: v["sha256"] for k, v in result.profile_summaries.items()},
        "stats": result.stats,
    })

    return {
        "job_id": job_id,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "stats": result.stats,
        "report": report,
        "images": {
            "preview": f"/api/jobs/{job_id}/images/preview.png",
            "softproof": f"/api/jobs/{job_id}/images/softproof.png",
            "heatmap": f"/api/jobs/{job_id}/images/heatmap.png",
            "overlay": f"/api/jobs/{job_id}/images/overlay.png",
            "mask": f"/api/jobs/{job_id}/images/mask.png",
        },
    }


# ---------------------------------------------------------------------------
# API: job artifacts
# ---------------------------------------------------------------------------

def _require_job(job_id: str) -> Path:
    base = JOB_DIR / job_id
    if not (base / "meta.json").exists():
        raise HTTPException(404, "任务不存在或已被删除")
    return base


@app.get("/api/jobs")
def list_jobs():
    rows = db.recent_jobs(app.state.db)
    return [dict(r) for r in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    _require_job(job_id)
    return JSONResponse(json.loads((JOB_DIR / job_id / "report.json").read_text("utf-8")))


@app.get("/api/jobs/{job_id}/images/{name}")
def get_job_image(job_id: str, name: str):
    base = _require_job(job_id)
    allowed = {"preview.png", "softproof.png", "heatmap.png", "overlay.png", "mask.png"}
    if name not in allowed:
        raise HTTPException(404, "图像不存在")
    return FileResponse(base / name, media_type="image/png")


@app.get("/api/jobs/{job_id}/report")
def get_job_report(job_id: str):
    base = _require_job(job_id)
    return FileResponse(
        base / "report.json", media_type="application/json", filename="report.json"
    )


@app.get("/api/jobs/{job_id}/export.zip")
def get_job_zip(job_id: str):
    base = _require_job(job_id)
    cache = base / "export.zip"
    if not cache.exists():
        pngs = {}
        for name in ("preview.png", "softproof.png", "heatmap.png", "overlay.png", "mask.png"):
            pngs[name] = (base / name).read_bytes()
        report = json.loads((base / "report.json").read_text("utf-8"))
        cache.write_bytes(pl.export_zip(pngs, report))
    return FileResponse(cache, media_type="application/zip", filename=f"{job_id[:12]}-softproof.zip")


@app.post("/api/jobs/{job_id}/region")
async def inspect_region(job_id: str, payload: dict):
    _require_job(job_id)
    try:
        arrays = load_job_arrays(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "任务像素数据不存在")

    class R:  # light adapter
        pass

    r = R()
    r.img = type("I", (), {"size": (arrays["meta"]["width"], arrays["meta"]["height"])})()
    r.lab_src = np.asarray(arrays["lab_src"])
    r.lab_press = np.asarray(arrays["lab_press"])
    r.cmyk = np.asarray(arrays["cmyk"])
    r.de = np.asarray(arrays["de"])
    r.inputs = type("P", (), {"threshold": arrays["meta"]["threshold"]})()

    try:
        return regions_mod.inspect_region(
            r,
            int(payload.get("x", 0)),
            int(payload.get("y", 0)),
            int(payload.get("width", 10)),
            int(payload.get("height", 10)),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# ---------------------------------------------------------------------------
# API: 局部 CMYK 修色
# ---------------------------------------------------------------------------

_RETTOUCH_IMAGES = {
    "before_crop.png": "image/png",
    "after_crop.png": "image/png",
    "after.png": "image/png",
}


@app.post("/api/jobs/{job_id}/retouch")
async def create_retouch(job_id: str, payload: dict):
    base = _require_job(job_id)
    state = app.state.state
    try:
        return retouch.run_retouch(job_id, payload, state.pm)
    except retouch.RetouchUnreachable as exc:
        # 原任务 ICC 缺失或摘要不符——必须拒绝（409，与发布模块的冲突语义一致）。
        raise HTTPException(409, str(exc))
    except retouch.RetouchError as exc:
        raise HTTPException(422, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, f"任务产物缺失，无法修色: {exc}")


def _retouch_dir(job_id: str, retouch_id: str) -> Path:
    base = _require_job(job_id)
    d = base / "retouch" / retouch_id
    if not (d / "retouch.json").exists():
        raise HTTPException(404, "修色结果不存在")
    return d


@app.get("/api/jobs/{job_id}/retouch/{retouch_id}")
def get_retouch(job_id: str, retouch_id: str):
    d = _retouch_dir(job_id, retouch_id)
    return JSONResponse(json.loads((d / "retouch.json").read_text("utf-8")))


@app.get("/api/jobs/{job_id}/retouch/{retouch_id}/images/{name}")
def get_retouch_image(job_id: str, retouch_id: str, name: str):
    d = _retouch_dir(job_id, retouch_id)
    if name not in _RETTOUCH_IMAGES or not (d / name).exists():
        raise HTTPException(404, "图像不存在")
    return FileResponse(d / name, media_type=_RETTOUCH_IMAGES[name])


@app.get("/api/jobs/{job_id}/retouch/{retouch_id}/corrected_cmyk.tif")
def get_retouch_tiff(job_id: str, retouch_id: str):
    d = _retouch_dir(job_id, retouch_id)
    tif = d / "corrected_cmyk.tif"
    if not tif.exists():
        raise HTTPException(404, "成品不存在（未达容差或无可行解时不生成 TIFF）")
    return FileResponse(
        tif, media_type="image/tiff", filename=f"{job_id[:12]}-corrected-cmyk.tif"
    )


@app.get("/api/jobs/{job_id}/retouch/{retouch_id}/correction_report.json")
def get_retouch_report(job_id: str, retouch_id: str):
    d = _retouch_dir(job_id, retouch_id)
    rep = d / "correction_report.json"
    if not rep.exists():
        raise HTTPException(404, "报告不存在（未达容差或无可行解时不生成成品报告）")
    return FileResponse(rep, media_type="application/json",
                        filename="correction_report.json")


# Static frontend (mounted last so /api routes win).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
