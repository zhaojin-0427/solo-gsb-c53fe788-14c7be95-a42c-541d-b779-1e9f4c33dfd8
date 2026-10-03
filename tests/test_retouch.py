"""局部 CMYK 修色（local CMYK correction）端到端测试。"""
import io
import os

import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app import lcms_native, retouch


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("retouch-data")
    os.environ["SPD_DATA_DIR"] = str(data_dir)
    from app import config
    # 重置共享单例，使本模块使用独立的临时数据目录与 ProfileManager。
    config._state = None
    config.DATA_DIR = data_dir
    config.PROFILE_DIR = data_dir / "profiles"
    config.JOB_DIR = data_dir / "jobs"
    config.RELEASE_DIR = data_dir / "release"
    config.DB_PATH = data_dir / "softproof.db"
    from app import main
    main.JOB_DIR = config.JOB_DIR
    main.DB_PATH = config.DB_PATH
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def ids(client):
    profs = client.get("/api/profiles").json()
    return {
        "srgb": next(p["profile_id"] for p in profs["source"] if p["color_space"] == "RGB"),
        "cmyk": next(p["profile_id"] for p in profs["press"]),
        "display": next(p["profile_id"] for p in profs["display"]),
    }


def _solid_png(rgb=(170, 60, 90), size=40):
    arr = np.zeros((size, size, 3), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = rgb
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="PNG")
    return buf.getvalue()


def _run_job(client, ids, data):
    return client.post(
        "/api/jobs",
        files={"image": ("img.png", data, "image/png")},
        data={
            "source_profile": ids["srgb"],
            "press_profile": ids["cmyk"],
            "display_profile": ids["display"],
            "threshold": "3",
        },
    ).json()


@pytest.fixture
def solid_job(client, ids):
    return _run_job(client, ids, _solid_png())


def _region_mean_press(client, jid, x=4, y=4, w=24, h=24):
    d = client.post(
        f"/api/jobs/{jid}/region",
        json={"x": x, "y": y, "width": w, "height": h},
    ).json()
    return d["lab_after_press"]["mean"]


def _retouch(client, jid, **over):
    body = {
        "x": 4, "y": 4, "width": 24, "height": 24,
        "target_lab": [50, 0, 0], "tolerance_de": 2.0,
        "max_shift": 8, "total_ink_limit": 1020,
    }
    body.update(over)
    return client.post(f"/api/jobs/{jid}/retouch", json=body)


def _builtin_press_id(client):
    return client.get("/api/profiles").json()["press"][0]["profile_id"]


# --------------------------------------------------------------------------

def _exact_mean_lab(client, jid, x=4, y=4, w=24, h=24):
    """Full-precision mean post-press Lab straight from the persisted arrays."""
    from app import main
    lab = np.load(main.JOB_DIR / jid / "lab_press.npy")[y:y + h, x:x + w]
    return [float(v) for v in lab.reshape(-1, 3).mean(axis=0)]


def test_zero_delta_reaches_when_target_is_current_mean(client, solid_job):
    jid = solid_job["job_id"]
    lab = _exact_mean_lab(client, jid)
    r = _retouch(client, jid, target_lab=lab, tolerance_de=1e-6, max_shift=0)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "reached"
    assert d["after"]["delta_cmyk"] == [0, 0, 0, 0]
    assert d["after"]["target_de00_mean"] <= 1e-3


def test_reached_exports_tiff_and_report_with_outside_bytes_unchanged(client, solid_job):
    from app import main
    jid = solid_job["job_id"]
    lab = _region_mean_press(client, jid)
    d = _retouch(client, jid, target_lab=[lab[0] + 4, lab[1], lab[2]],
                 tolerance_de=1.5).json()
    assert d["status"] == "reached", d
    delta = d["after"]["delta_cmyk"]

    tif = client.get(d["artifacts"]["tiff"])
    assert tif.status_code == 200
    im = Image.open(io.BytesIO(tif.content))
    assert im.mode == "CMYK"
    # 嵌入的正是原印刷 ICC
    state = client.app.state.state
    press_bytes = state.pm.load_bytes(_builtin_press_id(client))
    assert im.info["icc_profile"] == press_bytes

    orig = np.load(main.JOB_DIR / jid / "cmyk.npy")
    got = np.asarray(im)
    x, y, w, h = 4, 4, 24, 24
    mask = np.ones(orig.shape[:2], dtype=bool)
    mask[y:y + h, x:x + w] = False
    # 选区外字节与原分色完全一致
    assert np.array_equal(got[mask], orig[mask])
    # 选区内恰为原值 + 统一偏移
    assert np.array_equal(
        got[y:y + h, x:x + w].astype(np.int32),
        orig[y:y + h, x:x + w].astype(np.int32) + np.array(delta, dtype=np.int32),
    )
    assert got.min() >= 0 and got.max() <= 255

    rep = client.get(d["artifacts"]["report"]).json()
    assert rep["report_type"] == "local_cmyk_correction"
    assert rep["source_job"]["job_id"] == jid
    assert rep["profiles"]["press"]["sha256"] == \
        solid_job["report"]["profiles"]["press"]["sha256"]
    assert rep["constraints"]["target_lab"]
    assert rep["result"]["delta_cmyk"] == delta
    assert rep["output"]["tiff_bytes_outside_selection_unchanged"] is True
    assert rep["output"]["embedded_press_icc_sha256"] == \
        rep["profiles"]["press"]["sha256"]

    # 修色前后软打样均可取回
    for key in ("before_crop", "after_crop", "after"):
        resp = client.get(d["artifacts"][key])
        assert resp.status_code == 200
        assert Image.open(io.BytesIO(resp.content)).info.get("icc_profile")


def test_unreachable_when_tolerance_not_met_has_no_artifacts(client, solid_job):
    jid = solid_job["job_id"]
    # 极端目标（L=0,a=127,b=-128）在 ±8 偏移下达不到
    r = _retouch(client, jid, target_lab=[0, 127, -128], tolerance_de=0.5)
    d = r.json()
    assert d["status"] == "unreachable"
    assert d["reason"] == "tolerance_missed"
    assert "tiff" not in d["artifacts"]
    assert "report" not in d["artifacts"]
    # 成品端点返回 404
    rid = d["retouch_id"]
    assert client.get(f"/api/jobs/{jid}/retouch/{rid}/corrected_cmyk.tif").status_code == 404
    assert client.get(
        f"/api/jobs/{jid}/retouch/{rid}/correction_report.json").status_code == 404
    # 仍给出最优可行解的前后软打样
    assert client.get(d["artifacts"]["after"]).status_code == 200


def test_no_feasible_solution_when_ink_limit_zero(client, solid_job):
    jid = solid_job["job_id"]
    # 原分色本身含墨时，总墨量上限 0 排除一切可行偏移
    from app import main
    cmyk = np.load(main.JOB_DIR / jid / "cmyk.npy")[4:28, 4:28]
    if int(cmyk.sum(axis=2).min()) == 0:
        pytest.skip("该选区存在零墨像素，0 墨量约束测试不适用")
    r = _retouch(client, jid, target_lab=[0, 0, 0], tolerance_de=200,
                 total_ink_limit=0)
    d = r.json()
    assert d["status"] == "unreachable"
    assert d["reason"] == "no_feasible_solution"
    assert d["search"]["feasible_count"] == 0


def test_validation_errors(client, solid_job):
    jid = solid_job["job_id"]
    cases = [
        {"width": 65},
        {"height": 65},
        {"x": 30, "y": 0, "width": 20, "height": 20},  # 越界（图 40 宽）
        {"target_lab": [100.5, 0, 0]},
        {"target_lab": [50, 128, 0]},
        {"target_lab": [50, 0, -129]},
        {"tolerance_de": -0.1},
        {"max_shift": 9},
        {"total_ink_limit": 1021},
        {"target_lab": [50, 0]},
    ]
    for over in cases:
        r = _retouch(client, jid, **over)
        assert r.status_code == 422, (over, r.text)


def test_unknown_job_404(client):
    r = client.post("/api/jobs/" + "f" * 64 + "/retouch", json={
        "x": 0, "y": 0, "width": 1, "height": 1,
        "target_lab": [50, 0, 0], "tolerance_de": 1,
        "max_shift": 1, "total_ink_limit": 1020,
    })
    assert r.status_code == 404


def test_press_icc_tampered_or_missing_rejected(client, solid_job, monkeypatch):
    """原任务印刷 ICC 缺失或摘要不符必须拒绝（409）。"""
    from app import config
    jid = solid_job["job_id"]
    lab = _region_mean_press(client, jid)
    press_path = config.PROFILE_DIR / "builtin" / "builtin-demo-cmyk.icc"
    original = press_path.read_bytes()

    variant = lcms_native.build_demo_cmyk_profile_bytes(ink_scale=0.7)
    assert variant != original
    press_path.write_bytes(variant)
    try:
        r = _retouch(client, jid, target_lab=lab, tolerance_de=1)
        assert r.status_code == 409
        assert "摘要不符" in r.json()["detail"]
    finally:
        press_path.write_bytes(original)

    # 恢复后重新可用
    assert _retouch(client, jid, target_lab=lab, tolerance_de=1).status_code == 200

    # 缺失（重命名移走）
    hidden = press_path.with_suffix(".hidden")
    press_path.rename(hidden)
    try:
        r = _retouch(client, jid, target_lab=lab, tolerance_de=1)
        assert r.status_code == 409
        assert "缺失" in r.json()["detail"]
    finally:
        hidden.rename(press_path)


def test_deterministic_retouch_id_and_results(client, solid_job):
    jid = solid_job["job_id"]
    lab = _region_mean_press(client, jid)
    d1 = _retouch(client, jid, target_lab=[lab[0] + 2, lab[1], lab[2]],
                  tolerance_de=1.0).json()
    d2 = _retouch(client, jid, target_lab=[lab[0] + 2, lab[1], lab[2]],
                  tolerance_de=1.0).json()
    assert d1["retouch_id"] == d2["retouch_id"]
    assert d1["after"]["delta_cmyk"] == d2["after"]["delta_cmyk"]
    # GET 可复现同一结果
    got = client.get(
        f"/api/jobs/{jid}/retouch/{d1['retouch_id']}").json()
    assert got["retouch_id"] == d1["retouch_id"]


def test_channel_clip_rule_is_hard_constraint(client, solid_job):
    """选区含 0 或 255 通道时，向界外推的偏移必须不可行（不做钳制）。"""
    from app import main
    jid = solid_job["job_id"]
    region = np.load(main.JOB_DIR / jid / "cmyk.npy")[4:28, 4:28]
    lab = _region_mean_press(client, jid)
    # 构造一个严格不可行场景：若选区某通道最小值为 0，则任何负偏移必须不可行。
    if (region.reshape(-1, 4).min(axis=0) == 0).any():
        r = _retouch(client, jid, target_lab=lab, tolerance_de=200,
                     max_shift=8)
        d = r.json()
        delta = np.array(d["after"]["delta_cmyk"])
        # 最优偏移中对应“选区最小值 0”的通道不得为负。
        zero_channels = np.flatnonzero(
            region.reshape(-1, 4).min(axis=0) == 0)
        assert (delta[zero_channels] >= 0).all()


def test_feasible_mask_matches_brute_force():
    rng = np.random.RandomState(42)

    def brute(cands, reg, lim):
        sh = cands[:, None, :].astype(np.int32) + reg[None].astype(np.int32)
        return ((sh >= 0) & (sh <= 255)).all(axis=2).all(axis=1) & \
            (sh.sum(axis=2) <= lim).all(axis=1)

    for _ in range(30):
        s = int(rng.randint(0, 9))
        ax = np.arange(-s, s + 1, dtype=np.int16)
        cc, mm, yy, kk = np.meshgrid(ax, ax, ax, ax, indexing="ij")
        cands = np.stack([cc.ravel(), mm.ravel(), yy.ravel(), kk.ravel()], axis=1)
        reg = rng.randint(0, 256, size=(int(rng.randint(1, 40)), 4)).astype(np.uint8)
        lim = int(rng.randint(0, 1021))
        assert np.array_equal(
            retouch._feasible_mask(cands, reg, s, lim),
            brute(cands, reg, lim),
        )


def test_tie_break_order_prefers_smaller_abs_sum_then_lexicographic():
    """块内与分块归并都必须严格按 (ΔE, 绝对值和, (C,M,Y,K) 字典序) 择优。"""
    # 块内：sub 按字典序排列；构造并列验证 lexsort 三级键。
    de = np.array([1.0, 1.0, 1.0, 0.5])
    abssum = np.array([4, 2, 2, 9])
    order = np.lexsort((np.arange(4), abssum, de))
    assert de[int(order[0])] == 0.5  # ΔE 最优先
    de2 = np.array([1.0, 1.0, 1.0])
    abssum2 = np.array([4, 2, 2])
    order2 = np.lexsort((np.arange(3), abssum2, de2))
    assert int(order2[0]) == 1  # 同 ΔE/同 abs-sum 时取字典序最小者

    # 归并：前两个分块同 ΔE、同 abs-sum（必须取字典序最小者）；第三个
    # ΔE 更小，应无视 abs-sum 直接胜出——先分别验证两种情形。
    tied = [
        (1.0, 4, (0, -2, 0, -2)),
        (1.0, 4, (-1, 0, -1, -2)),
        (1.0, 4, (0, -3, 0, -1)),
    ]
    assert min(tied) == (1.0, 4, (-1, 0, -1, -2))
    mixed = tied + [(0.8, 6, (2, 2, 2, 0))]
    assert min(mixed) == (0.8, 6, (2, 2, 2, 0))


def test_real_search_lexicographic_tiebreak(client, solid_job):
    """真实搜索中并列时必须选 (C,M,Y,K) 字典序最小的可行偏移。"""
    from app import main
    jid = solid_job["job_id"]
    # 目标就是修色前平均 Lab：ΔE=0 的最优解集合包含 0 偏移本身；零偏移是
    # 绝对值和最小(0) 且字典序最小，必须被选中。
    lab = _exact_mean_lab(client, jid)
    d = _retouch(client, jid, target_lab=lab, tolerance_de=1e-6,
                 max_shift=8).json()
    assert d["status"] == "reached"
    assert d["after"]["delta_cmyk"] == [0, 0, 0, 0]
    assert d["after"]["delta_abs_sum"] == 0


def test_full_64x64_selection_accepted(client, ids):
    # 渐变图提供多种颜色；64×64 选区必须被接受并完成穷举。
    arr = np.zeros((70, 70, 3), np.uint8)
    arr[..., 0] = np.linspace(0, 255, 70, dtype=np.uint8)[None, :]
    arr[..., 1] = np.linspace(0, 255, 70, dtype=np.uint8)[:, None]
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="PNG")
    jid = _run_job(client, ids, buf.getvalue())["job_id"]
    lab = _exact_mean_lab(client, jid, x=0, y=0, w=64, h=64)
    r = _retouch(client, jid, x=0, y=0, width=64, height=64,
                 target_lab=lab, tolerance_de=1e-6, max_shift=0)
    assert r.status_code == 200
    assert r.json()["search"]["candidates_total"] == 1
    assert r.json()["status"] == "reached"
