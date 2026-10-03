"""局部 CMYK 修色（local CMYK correction）。

从一个**已保存任务**的分色结果出发，在用户框选的（≤64×64 像素）矩形内，
按**原印刷 ICC** 搜索一个**统一的整数 CMYK 偏移** ``(dC, dM, dY, dK)``，使
选区平均 Lab 落入给定的目标 Lab 与 ΔE00 容差内。

约束（对选区中的**每个**像素逐一成立）：

* 通道结果 ``orig + delta`` 保持在 0..255（无钳制，越界即不可行）；
* ``|delta_ch| <= max_shift``（max_shift 为 0..8 的整数）；
* 总墨量 ``(orig+delta).sum(axis=通道) <= total_ink_limit``（0..1020）。

目标函数（严格字典序，取最小者）：

1. 选区平均 Lab 与目标 Lab 的 **CIEDE2000** 色差；
2. 偏移**绝对值之和** ``|dC|+|dM|+|dY|+|dK|``；
3. 偏移元组 **(C,M,Y,K) 字典序**。

回算 Lab 全部由 LittleCMS 经原印刷 ICC 完成（``TYPE_Lab_DBL``），与软打样
主链路同一精度。无可行解或最优解未达容差时标记 *unreachable*，不导出成品；
达标时导出**选区外 CMYK 字节与原分色完全一致**、嵌入原印刷 ICC 的 TIFF，
以及记录原任务、ICC 摘要、约束与偏移的 JSON 报告。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import colorscience, lcms_native
from .profiles import ProfileManager

MAX_SIDE = 64
MAX_SHIFT = 8
MAX_TOTAL_INK = 255 * 4

# 单次送入 lcms 的“候选 × 唯一颜色”行数上限（控制每个工作线程的驻留内存）。
# 500,000 行 × (4 字节输入 + 3×8 字节 Lab 等中间量) ≈ 60 MB/线程。
_CHUNK_ROWS = 500_000


class RetouchError(ValueError):
    """请求非法（坐标/参数越界等），HTTP 422。"""


class RetouchUnreachable(RuntimeError):
    """原任务印刷 ICC 缺失或摘要不符，拒绝修色，HTTP 409。"""


@dataclass(frozen=True)
class RetouchRequest:
    x: int
    y: int
    width: int
    height: int
    target_lab: tuple[float, float, float]
    tolerance_de: float
    max_shift: int
    total_ink_limit: int


def validate_request(raw: dict, image_width: int, image_height: int) -> RetouchRequest:
    """Validate and normalize the JSON body of ``POST .../retouch``."""
    try:
        x = int(raw["x"])
        y = int(raw["y"])
        w = int(raw["width"])
        h = int(raw["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RetouchError("选区坐标 x/y/width/height 必须为整数") from exc
    if w < 1 or h < 1:
        raise RetouchError("选区宽高必须为正整数")
    if w > MAX_SIDE or h > MAX_SIDE:
        raise RetouchError(f"选区不得超过 {MAX_SIDE}×{MAX_SIDE} 像素")
    if x < 0 or y < 0 or x + w > image_width or y + h > image_height:
        raise RetouchError("选区超出图像范围")

    lab_raw = raw.get("target_lab")
    if not isinstance(lab_raw, (list, tuple)) or len(lab_raw) != 3:
        raise RetouchError("target_lab 必须是 [L,a,b] 三个数")
    try:
        L, a, b = (float(v) for v in lab_raw)
    except (TypeError, ValueError) as exc:
        raise RetouchError("target_lab 数值无效") from exc
    if not (0.0 <= L <= 100.0):
        raise RetouchError("目标 L 必须在 0–100 之间")
    if not (-128.0 <= a <= 127.0 and -128.0 <= b <= 127.0):
        raise RetouchError("目标 a/b 必须在 -128–127 之间")

    try:
        tol = float(raw.get("tolerance_de", 0.0))
    except (TypeError, ValueError) as exc:
        raise RetouchError("ΔE00 容差无效") from exc
    if tol < 0.0 or not np.isfinite(tol):
        raise RetouchError("ΔE00 容差必须是非负数")

    try:
        max_shift = int(raw.get("max_shift", 8))
    except (TypeError, ValueError) as exc:
        raise RetouchError("通道偏移上限必须是 0–8 的整数") from exc
    if not 0 <= max_shift <= MAX_SHIFT:
        raise RetouchError("通道偏移绝对值上限必须是 0–8 的整数")

    try:
        ink_limit = int(raw.get("total_ink_limit", MAX_TOTAL_INK))
    except (TypeError, ValueError) as exc:
        raise RetouchError("总墨量上限必须是 0–1020 的整数") from exc
    if not 0 <= ink_limit <= MAX_TOTAL_INK:
        raise RetouchError("总墨量上限必须是 0–1020 的整数")

    return RetouchRequest(
        x=x, y=y, width=w, height=h,
        target_lab=(L, a, b), tolerance_de=tol,
        max_shift=max_shift, total_ink_limit=ink_limit,
    )


def load_job(job_id: str, pm: ProfileManager) -> dict:
    """Load job arrays/meta and re-resolve the *original* press ICC.

    Raises RetouchUnreachable when the press ICC file is gone or its bytes no
    longer match the digest recorded by the original task (the soft proof and
    the correction must stay bound to exactly the press separation that
    produced the saved CMYK).
    """
    from .config import JOB_DIR

    base = JOB_DIR / job_id
    meta_path = base / "meta.json"
    if not meta_path.exists():
        raise RetouchError("任务不存在或已被删除")
    meta = json.loads(meta_path.read_text("utf-8"))
    report = json.loads((base / "report.json").read_text("utf-8"))

    press_id = meta["press_profile"]
    try:
        press_bytes = pm.load_bytes(press_id)
    except Exception as exc:
        raise RetouchUnreachable(
            f"原任务印刷 ICC 缺失（{press_id}），拒绝修色: {exc}"
        ) from exc
    actual = hashlib.sha256(press_bytes).hexdigest()
    expected = report["profiles"]["press"]["sha256"]
    if actual != expected:
        raise RetouchUnreachable(
            "原任务印刷 ICC 摘要不符：当前文件 "
            f"{actual[:16]}… ≠ 任务记录 {expected[:16]}…，拒绝修色"
        )
    # The display profile is needed to render the corrected soft proof; it is
    # part of the original task's binding as well.
    try:
        display_bytes = pm.load_bytes(meta["display_profile"])
    except Exception as exc:
        raise RetouchUnreachable(
            f"原任务显示 ICC 缺失（{meta['display_profile']}），拒绝修色: {exc}"
        ) from exc
    if hashlib.sha256(display_bytes).hexdigest() != \
            report["profiles"]["display"]["sha256"]:
        raise RetouchUnreachable("原任务显示 ICC 摘要不符，拒绝修色")

    cmyk = np.load(base / "cmyk.npy")
    lab_before = np.load(base / "lab_press.npy")
    proof_rgb = np.asarray(
        Image.open(base / "softproof.png").convert("RGB").copy()
    )
    return {
        "base": base,
        "meta": meta,
        "report": report,
        "cmyk": cmyk,
        "lab_before": lab_before,
        "proof_rgb": proof_rgb,
        "press_bytes": press_bytes,
        "press_summary": report["profiles"]["press"],
        "display_bytes": display_bytes,
        "display_summary": report["profiles"]["display"],
        "source_summary": report["profiles"]["source"],
    }


def _feasible_mask(cands: np.ndarray, region_flat: np.ndarray,
                   s: int, ink_limit: int) -> np.ndarray:
    """Boolean mask of candidates feasible for *every* region pixel.

    The uniform-delta constraints collapse, per channel ``ch``, to an
    interval on the scalar delta:

        0 <= v + d <= 255  for all region pixels v
        =>  -min(v) <= d <= 255 - max(v)            （再与 [-s, s] 取交）

    and the total-ink rule collapses to a single scalar inequality::

        sum_p(v) + sum(delta) <= limit  for all p
        =>  sum(delta) <= limit - max_p sum_p(v)

    i.e. everything is O(M) instead of an O(M×U×4) broadcast.
    """
    vmin = region_flat.min(axis=0)  # (4,)
    vmax = region_flat.max(axis=0)
    lo = np.maximum(-s, -vmin.astype(np.int32))[None, :]      # (1,4)
    hi = np.minimum(s, 255 - vmax.astype(np.int32))[None, :]  # (1,4)
    channel_ok = ((cands >= lo) & (cands <= hi)).all(axis=1)
    max_ink = int(region_flat.sum(axis=1).max())
    ink_ok = cands.astype(np.int32).sum(axis=1) <= (ink_limit - max_ink)
    return channel_ok & ink_ok


def search_correction(cmyk_region: np.ndarray, press_bytes: bytes,
                      intent: str, req: RetouchRequest) -> dict:
    """Exhaustively search the best uniform integer CMYK delta.

    Returns a dict with feasibility/optimality information and the corrected
    per-pixel CMYK/Lab of the region. No clipping is ever applied.
    """
    orig = np.ascontiguousarray(cmyk_region.reshape(-1, 4), dtype=np.int32)

    # 候选：(2s+1)^4 个整数偏移，先按最终字典序 (C,M,Y,K) 排好，
    # 使后续“仅在 ΔE 严格更优时替换”天然实现完整三级择优。
    s = req.max_shift
    axis = np.arange(-s, s + 1, dtype=np.int16)
    cc, mm, yy, kk = np.meshgrid(axis, axis, axis, axis, indexing="ij")
    cands = np.stack([cc.ravel(), mm.ravel(), yy.ravel(), kk.ravel()], axis=1)
    cand_abs_sum = np.abs(cands.astype(np.int32)).sum(axis=1)

    # 唯一颜色（回算 Lab 只需对每个唯一设备值做一次）。
    uniq, inverse, counts = np.unique(
        cmyk_region.reshape(-1, 4), axis=0, return_inverse=True, return_counts=True
    )
    counts = counts.astype(np.float64)
    total = float(counts.sum())
    target = np.asarray(req.target_lab, dtype=np.float64)

    feasible = _feasible_mask(cands, orig, s, req.total_ink_limit)

    if not feasible.any():
        return {"status": "infeasible", "candidates_total": int(len(cands)),
                "feasible_count": 0}

    fid = np.flatnonzero(feasible)
    pairs_chunk = max(1, _CHUNK_ROWS // max(1, len(uniq)))
    chunks = [fid[i:i + pairs_chunk] for i in range(0, len(fid), pairs_chunk)]

    def evaluate(sub: np.ndarray):
        """Evaluate one candidate chunk on a thread-private lcms transform."""
        shifted = (
            cands[sub][:, None, :].astype(np.int32)
            + uniq[None, :, :].astype(np.int32)
        )  # (m, U, 4)
        m, u = shifted.shape[:2]
        with lcms_native.DeviceToLabTransform(press_bytes, "CMYK", intent) as xf:
            labs = xf.apply_many(
                shifted.reshape(m * u, 4).astype(np.uint8)
            ).reshape(m, u, 3)
        # 选区平均 Lab：按各唯一颜色出现次数加权。
        mean_lab = (labs * counts[None, :, None]).sum(axis=1) / total
        de = colorscience.ciede2000(
            np.broadcast_to(target, mean_lab.shape), mean_lab
        )
        abs_sum = cand_abs_sum[sub]
        # 块内按 (ΔE, 偏移绝对值和, (C,M,Y,K) 字典序) 取最优；cands 按
        # (C,M,Y,K) 网格升序展平，故 sub 的顺序就是字典序，可用位置索引
        # 作为 lexsort 的最后（最高优先级）键。
        order = np.lexsort((np.arange(len(sub)), abs_sum, de))
        j = int(order[0])
        return (
            float(de[j]), int(abs_sum[j]),
            tuple(int(v) for v in cands[sub[j]]),
            int(sub[j]),
            np.ascontiguousarray(labs[j]),
        )

    # 每个工作线程持有独立的 LittleCMS transform（lcms 转换本身线程安全，
    # 但 transform 句柄不可共享）；最坏情形（4096 个唯一色 × 数千可行偏移）
    # 下按核并行把回算控制在可接受时长。
    n_workers = min(len(chunks), max(1, (os.cpu_count() or 2)))
    best: tuple | None = None
    best_lab_unique: np.ndarray | None = None
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        # 完整择优键 (ΔE, 偏移绝对值和, (C,M,Y,K) 字典序) 在分块归并时也逐
        # 级比较，避免并列最优落在不同分块时选错字典序。
        for de_best, abs_best, delta_t, cand_idx, lab_unique in pool.map(
            evaluate, chunks
        ):
            key = (de_best, abs_best, delta_t)
            if best is None or key < best[0]:
                best = (key, cand_idx, lab_unique)

    (de_best, abs_best, delta), best_idx, best_lab_unique = best
    # 用最优偏移重建选区逐像素 Lab（不经钳制——可行性已保证 0..255）。
    lab_unique = best_lab_unique
    lab_after = lab_unique[inverse]  # (P, 3)

    reached = bool(de_best <= req.tolerance_de + 1e-9)
    return {
        "status": "reached" if reached else "tolerance_missed",
        "candidates_total": int(len(cands)),
        "feasible_count": int(len(fid)),
        "delta": delta,
        "delta_abs_sum": abs_best,
        "mean_lab_after": [float(v) for v in lab_after.mean(axis=0)],
        "mean_de00": de_best,
        "lab_after_region": lab_after,
    }


def _per_pixel_stats(de: np.ndarray) -> dict:
    return {
        "mean": round(float(de.mean()), 4),
        "p95": round(float(np.percentile(de, 95)), 4),
        "max": round(float(de.max()), 4),
    }


def retouch_id(job_id: str, req: RetouchRequest) -> str:
    h = hashlib.sha256()
    h.update(job_id.encode())
    for v in (req.x, req.y, req.width, req.height, req.max_shift,
              req.total_ink_limit):
        h.update(str(v).encode())
    h.update(",".join(f"{v:.6f}" for v in req.target_lab).encode())
    h.update(f"{req.tolerance_de:.6f}".encode())
    return h.hexdigest()


def run_retouch(job_id: str, raw_request: dict, pm: ProfileManager) -> dict:
    """Full local-correction run: validate, search, render, (if reached) export.

    Deterministic: identical request bytes reuse the same retouch directory.
    """
    job = load_job(job_id, pm)
    meta = job["meta"]
    req = validate_request(raw_request, meta["width"], meta["height"])

    x, y, w, h = req.x, req.y, req.width, req.height
    cmyk_full = job["cmyk"]
    cmyk_region = np.ascontiguousarray(
        cmyk_full[y:y + h, x:x + w].copy()
    )

    search = search_correction(
        cmyk_region, job["press_bytes"], meta["source_intent"], req
    )

    rid = retouch_id(job_id, req)
    out_dir = job["base"] / "retouch" / rid
    out_dir.mkdir(parents=True, exist_ok=True)

    target = np.asarray(req.target_lab, dtype=np.float64)
    lab_before_region = job["lab_before"][y:y + h, x:x + w]
    before_mean = [float(v) for v in lab_before_region.reshape(-1, 3).mean(axis=0)]
    before_target_de = float(
        colorscience.ciede2000(
            target[None, None, :],
            np.asarray(before_mean, dtype=np.float64)[None, None, :],
        )[0, 0]
    )

    response = {
        "retouch_id": rid,
        "job_id": job_id,
        "rect": {"x": x, "y": y, "width": w, "height": h},
        "constraints": {
            "target_lab": [float(v) for v in req.target_lab],
            "tolerance_de00": req.tolerance_de,
            "max_shift_per_channel": req.max_shift,
            "total_ink_limit": req.total_ink_limit,
        },
        "search": {
            "candidates_total": search["candidates_total"],
            "feasible_count": search["feasible_count"],
        },
        "before": {
            "mean_lab": [round(v, 4) for v in before_mean],
            "target_de00": round(before_target_de, 4),
        },
        "press_profile": {
            "profile_id": meta["press_profile"],
            "name": job["press_summary"].get("name"),
            "sha256": job["press_summary"]["sha256"],
        },
        "artifacts": {},
    }

    proof_intent = meta["proof_intent"]
    display_bytes = job["display_bytes"]

    def save_png(name: str, rgb: np.ndarray) -> None:
        buf = io.BytesIO()
        Image.fromarray(np.ascontiguousarray(rgb), "RGB").save(
            buf, format="PNG", icc_profile=display_bytes
        )
        (out_dir / name).write_bytes(buf.getvalue())

    # 修色前选区软打样（原任务结果，按原显示 ICC 呈现）。
    proof_before_crop = job["proof_rgb"][y:y + h, x:x + w]
    save_png("before_crop.png", proof_before_crop)

    if search["status"] == "infeasible":
        response["status"] = "unreachable"
        response["reason"] = "no_feasible_solution"
        response["message"] = (
            "不存在满足通道范围/偏移上限/总墨量上限的统一整数偏移"
            f"（已评估 {search['candidates_total']} 个候选，0 个可行）；"
            "标记为不可达，不生成成品。"
        )
        # 仅修色前裁剪图可用于说明；无成品。
        response["artifacts"]["before_crop"] = \
            f"/api/jobs/{job_id}/retouch/{rid}/images/before_crop.png"
        _write_result_json(out_dir, response)
        return response

    delta = search["delta"]
    delta_arr = np.asarray(delta, dtype=np.int32)

    # 全图修色后 CMYK（选区外字节不变；选区内无钳制）。
    corrected_full = cmyk_full.astype(np.int32).copy()
    corrected_full[y:y + h, x:x + w] += delta_arr[None, None, :]
    assert corrected_full.min() >= 0 and corrected_full.max() <= 255

    # 全图修色后软打样（Lab -> 显示，按原 proof_intent）。
    lab_after_full = lcms_native.pixels_to_lab(
        corrected_full.reshape(-1, 4).astype(np.uint8),
        "CMYK", job["press_bytes"], meta["source_intent"],
    ).reshape(meta["height"], meta["width"], 3)
    proof_after_full = lcms_native.lab_to_pixels(
        lab_after_full.reshape(-1, 3), display_bytes, "RGB", proof_intent
    ).reshape(meta["height"], meta["width"], 3)
    save_png("after.png", proof_after_full)
    save_png("after_crop.png", proof_after_full[y:y + h, x:x + w])

    # 残余色差统计（两个视角：对目标、对修色前）。
    de_to_target = colorscience.ciede2000(
        np.broadcast_to(target, lab_after_full[y:y + h, x:x + w].shape),
        lab_after_full[y:y + h, x:x + w],
    )
    de_residual = colorscience.ciede2000(
        lab_before_region, lab_after_full[y:y + h, x:x + w]
    )
    response["after"] = {
        "delta_cmyk": list(delta),
        "delta_abs_sum": search["delta_abs_sum"],
        "mean_lab": [round(v, 4) for v in search["mean_lab_after"]],
        "target_de00_mean": round(search["mean_de00"], 4),
        "region_pixels_de00_to_target": _per_pixel_stats(de_to_target),
        "residual_de00_vs_before": _per_pixel_stats(de_residual),
    }
    response["artifacts"].update({
        "before_crop": f"/api/jobs/{job_id}/retouch/{rid}/images/before_crop.png",
        "after_crop": f"/api/jobs/{job_id}/retouch/{rid}/images/after_crop.png",
        "after": f"/api/jobs/{job_id}/retouch/{rid}/images/after.png",
    })

    if search["status"] == "tolerance_missed":
        response["status"] = "unreachable"
        response["reason"] = "tolerance_missed"
        response["message"] = (
            f"最优统一偏移 {delta} 的选区平均 ΔE00="
            f"{search['mean_de00']:.4f} 超过容差 {req.tolerance_de:.4f}；"
            "标记为不可达，不生成成品。"
        )
        _write_result_json(out_dir, response)
        return response

    # ---- reached: 导出成品（选区外字节不变 + 嵌入印刷 ICC 的 TIFF） -------
    rect_mask = _rect_mask(cmyk_full.shape[:2], x, y, w, h)
    outside_unchanged = bool(np.array_equal(
        corrected_full[~rect_mask], cmyk_full[~rect_mask]
    ))
    tiff_name = _export_tiff(
        corrected_full.astype(np.uint8), job["press_bytes"], out_dir
    )

    report = _build_report(job, req, response, search, out_dir, tiff_name,
                           outside_unchanged)
    (out_dir / "correction_report.json").write_text(
        json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2),
        "utf-8",
    )
    response["status"] = "reached"
    response["message"] = (
        f"达标：统一偏移 C{d0(delta[0])},M{d0(delta[1])},Y{d0(delta[2])},"
        f"K{d0(delta[3])}，选区平均 ΔE00={search['mean_de00']:.4f} "
        f"≤ 容差 {req.tolerance_de:.4f}。"
    )
    response["artifacts"]["tiff"] = \
        f"/api/jobs/{job_id}/retouch/{rid}/{tiff_name}"
    response["artifacts"]["report"] = \
        f"/api/jobs/{job_id}/retouch/{rid}/correction_report.json"
    _write_result_json(out_dir, response)
    return response


def d0(v: int) -> str:
    return f"{v:+d}"


def _rect_mask(shape_hw: tuple, x: int, y: int, w: int, h: int) -> np.ndarray:
    mask = np.zeros(shape_hw, dtype=bool)
    mask[y:y + h, x:x + w] = True
    return mask


def _write_result_json(out_dir, response: dict) -> None:
    (out_dir / "retouch.json").write_text(
        json.dumps(response, sort_keys=True, ensure_ascii=False, indent=2),
        "utf-8",
    )


def _export_tiff(cmyk: np.ndarray, press_icc: bytes, out_dir) -> str:
    """Write the corrected CMYK separations as an ICC-tagged TIFF.

    Bytes are written exactly as separated (0 = no ink, 255 = full, the lcms
    convention also used by the rest of the pipeline); no channel reversal and
    no clipping happen here.
    """
    img = Image.fromarray(np.ascontiguousarray(cmyk), "CMYK")
    name = "corrected_cmyk.tif"
    img.save(out_dir / name, format="TIFF", compression="tiff_lzw",
             icc_profile=press_icc)
    return name


def _build_report(job: dict, req: RetouchRequest, response: dict,
                  search: dict, out_dir, tiff_name: str,
                  outside_unchanged: bool) -> dict:
    meta = job["meta"]
    tiff_bytes = (out_dir / tiff_name).read_bytes()
    lab_before_region = job["lab_before"][
        req.y:req.y + req.height, req.x:req.x + req.width
    ]
    return {
        "report_type": "local_cmyk_correction",
        "source_job": {
            "job_id": job["meta"]["job_id"],
            "image_sha256": job["report"]["image"]["sha256"],
            "image_size": [meta["width"], meta["height"]],
            "image_mode": job["report"]["image"]["mode"],
            "embedded_source_sha256": job["report"]["image"].get(
                "embedded_source_sha256"
            ),
            "source_intent": meta["source_intent"],
            "proof_intent": meta["proof_intent"],
            "threshold_delta_e": meta["threshold"],
        },
        "profiles": {
            "source": job["source_summary"],
            "press": job["press_summary"],
            "display": job["display_summary"],
        },
        "selection": response["rect"],
        "constraints": response["constraints"],
        "search": {
            **response["search"],
            "tie_break_order": "delta_e00_mean -> sum_abs_delta -> (C,M,Y,K) lexicographic",
            "delta_space": "uniform integer CMYK offset applied to every selected pixel",
            "channel_rule": "each pixel channel result must remain in 0..255 without clipping",
            "lab_back_calculation": (
                "LittleCMS TYPE_Lab_DBL via the original press ICC; "
                f"intent={meta['source_intent']}"
            ),
        },
        "result": {
            "status": "reached",
            "delta_cmyk": response["after"]["delta_cmyk"],
            "delta_abs_sum": response["after"]["delta_abs_sum"],
            "before_mean_lab": response["before"]["mean_lab"],
            "after_mean_lab": response["after"]["mean_lab"],
            "target_de00_before": response["before"]["target_de00"],
            "target_de00_after": response["after"]["target_de00_mean"],
            "region_pixels_de00_to_target":
                response["after"]["region_pixels_de00_to_target"],
            "residual_de00_vs_before":
                response["after"]["residual_de00_vs_before"],
        },
        "output": {
            "tiff": tiff_name,
            "tiff_sha256": hashlib.sha256(tiff_bytes).hexdigest(),
            "tiff_bytes_outside_selection_unchanged": outside_unchanged,
            "embedded_press_icc_sha256": job["press_summary"]["sha256"],
            "cmyk_convention": "C,M,Y,K; 0 = 无墨, 255 = 满版",
        },
    }
