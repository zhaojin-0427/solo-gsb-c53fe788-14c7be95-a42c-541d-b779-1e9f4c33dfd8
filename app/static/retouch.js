// 局部 CMYK 修色前端：框选 ≤64×64 选区 → 输入目标 Lab/约束 → 搜索统一整数
// CMYK 偏移 → 显示修色前后软打样与残余色差 →（达标时）下载成品 TIFF 与报告。
"use strict";

const rt = {
  rect: null,
  lastRegionInfo: null,
  result: null,
};

const rt$ = (id) => document.getElementById(id);

function rtShowError(msg) {
  const box = rt$("rtError");
  if (!msg) {
    box.hidden = true;
    box.textContent = "";
  } else {
    box.hidden = false;
    box.textContent = msg;
  }
}

function rtResetResultUI() {
  rt.result = null;
  rt$("rtResult").hidden = true;
  rt$("rtResult").innerHTML = "";
}

// 切换/打开新任务时复位修色面板（由 app.js 在渲染任务后触发）。
window.addEventListener("spd:job-changed", () => {
  rt.rect = null;
  rt.result = null;
  rt$("retouchPanel").hidden = true;
  rt$("rtResult").hidden = true;
  rt$("rtResult").innerHTML = "";
  rtShowError("");
});

// 监听 app.js 的框选事件，自动回填坐标与“修色前平均 Lab”。
window.addEventListener("spd:region", (ev) => {
  const d = ev.detail;
  if (!window.state || !state.job) return;
  rt.rect = d;
  rt$("retouchPanel").hidden = false;
  rt$("rtX").value = d.x;
  rt$("rtY").value = d.y;
  rt$("rtW").value = d.width;
  rt$("rtH").value = d.height;
  rtResetResultUI();
  rtShowError("");
  if (d.width > 64 || d.height > 64) {
    rtShowError(`选区为 ${d.width}×${d.height}，超过 64×64 上限，请重新框选。`);
  }
});

// region 面板 JSON 中包含修色前平均 Lab；从 regionData 抓取，供“一键填充”。
rt$("rtFillBefore").addEventListener("click", () => {
  const pre = rt$("regionData");
  if (!pre || pre.hidden) {
    rtShowError("请先在图上框选区域。");
    return;
  }
  try {
    const data = JSON.parse(pre.textContent);
    const lab = data.lab_after_press.mean;
    rt$("rtL").value = lab[0];
    rt$("rtA").value = lab[1];
    rt$("rtB").value = lab[2];
  } catch (err) {
    rtShowError("无法读取修色前 Lab，请重新框选。");
  }
});

function rtReadBody() {
  const num = (id) => Number(rt$(id).value);
  const x = Math.trunc(num("rtX"));
  const y = Math.trunc(num("rtY"));
  const width = Math.trunc(num("rtW"));
  const height = Math.trunc(num("rtH"));
  const target_lab = [num("rtL"), num("rtA"), num("rtB")];
  const tolerance_de = num("rtTol");
  const max_shift = Math.trunc(num("rtShift"));
  const total_ink_limit = Math.trunc(num("rtInk"));

  if (![x, y, width, height].every(Number.isInteger)) {
    throw new Error("选区坐标/宽高必须是整数（先在图上框选或手工输入）。");
  }
  if (width < 1 || height < 1 || width > 64 || height > 64) {
    throw new Error("选区宽高必须在 1–64 像素之间。");
  }
  if (target_lab.some((v) => !Number.isFinite(v))) {
    throw new Error("目标 Lab 必须全部填写为数字。");
  }
  const [L, a, b] = target_lab;
  if (!(L >= 0 && L <= 100)) throw new Error("目标 L 必须在 0–100。");
  if (!(a >= -128 && a <= 127) || !(b >= -128 && b <= 127)) {
    throw new Error("目标 a/b 必须在 -128–127。");
  }
  if (!(tolerance_de >= 0)) throw new Error("ΔE00 容差必须是非负数。");
  if (!Number.isInteger(max_shift) || max_shift < 0 || max_shift > 8) {
    throw new Error("通道偏移绝对值上限必须是 0–8 的整数。");
  }
  if (!Number.isInteger(total_ink_limit) || total_ink_limit < 0 || total_ink_limit > 1020) {
    throw new Error("总墨量上限必须是 0–1020 的整数。");
  }
  return { x, y, width, height, target_lab, tolerance_de, max_shift, total_ink_limit };
}

rt$("rtRun").addEventListener("click", async () => {
  rtShowError("");
  rtResetResultUI();
  if (!state.job) {
    rtShowError("请先打开或生成一个已保存任务。");
    return;
  }
  let body;
  try {
    body = rtReadBody();
  } catch (err) {
    rtShowError(err.message);
    return;
  }
  rt$("rtRun").disabled = true;
  rt$("rtBusy").hidden = false;
  try {
    const res = await api(`/api/jobs/${state.job.job_id}/retouch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    rt.result = res;
    rtRender(res);
  } catch (err) {
    rtShowError("修色请求被拒绝：" + err.message);
  } finally {
    rt$("rtRun").disabled = false;
    rt$("rtBusy").hidden = true;
  }
});

function fmtLab(v) {
  return (v || []).map((x) => Number(x).toFixed(3)).join(", ");
}
function fmtStats(s) {
  if (!s) return "—";
  return `均值 ${s.mean} ｜ P95 ${s.p95} ｜ 最大 ${s.max}`;
}

function rtRender(d) {
  const box = rt$("rtResult");
  box.hidden = false;
  const reached = d.status === "reached";
  const badge = reached
    ? '<span class="rt-badge ok">达标 · 已生成成品</span>'
    : '<span class="rt-badge bad">不可达 · 不生成成品</span>';

  let html = `
    <div class="rt-head">${badge}</div>
    <div class="rt-msg hint">${d.message || ""}</div>
    <table class="rt-table">
      <tr><th>搜索空间</th><td>${d.search.candidates_total} 个候选；可行 ${d.search.feasible_count} 个</td></tr>
      <tr><th>修色前平均 Lab</th><td>${fmtLab(d.before.mean_lab)}（对目标 ΔE00=${d.before.target_de00}）</td></tr>`;
  if (d.after) {
    html += `
      <tr><th>统一偏移 (C,M,Y,K)</th><td><b>${d.after.delta_cmyk.join(", ")}</b>
        （绝对值之和 ${d.after.delta_abs_sum}）</td></tr>
      <tr><th>修色后平均 Lab</th><td>${fmtLab(d.after.mean_lab)}</td></tr>
      <tr><th>对目标 ΔE00（平均）</th><td><b>${d.after.target_de00_mean}</b>
        （容差 ${d.constraints.tolerance_de00}）</td></tr>
      <tr><th>逐像素对目标 ΔE00</th><td>${fmtStats(d.after.region_pixels_de00_to_target)}</td></tr>
      <tr><th>残余色差（修色后 vs 修色前）</th><td>${fmtStats(d.after.residual_de00_vs_before)}</td></tr>`;
  }
  html += `
      <tr><th>原印刷 ICC</th><td>${d.press_profile.name || d.press_profile.profile_id}<br>
        <code>${d.press_profile.sha256.slice(0, 24)}…</code></td></tr>
    </table>`;

  const imgs = d.artifacts || {};
  const zoom = Math.max(1, Math.min(12, Math.floor(48 / Math.max(
    d.rect.width, d.rect.height))));
  html += `<div class="rt-proofs">`;
  if (imgs.before_crop) {
    html += `<figure><figcaption>修色前（选区软打样，${d.rect.width}×${d.rect.height}）</figcaption>
      <img src="${imgs.before_crop}" style="image-rendering:pixelated;width:${d.rect.width * zoom}px"></figure>`;
  }
  if (imgs.after_crop) {
    html += `<figure><figcaption>修色后（选区软打样）</figcaption>
      <img src="${imgs.after_crop}" style="image-rendering:pixelated;width:${d.rect.width * zoom}px"></figure>`;
  }
  html += `</div>`;
  if (imgs.after) {
    html += `<figure class="rt-full"><figcaption>修色后整图软打样</figcaption>
      <img src="${imgs.after}"></figure>`;
  }
  if (reached) {
    html += `<div class="row rt-exports">
      <a class="btn primary" href="${imgs.tiff}" download>下载成品 TIFF（选区外 CMYK 不变 · 嵌入印刷 ICC）</a>
      <a class="btn" href="${imgs.report}" download>下载修色报告 JSON</a>
      <a class="btn" href="${imgs.after}" target="_blank" rel="noopener">查看整图软打样</a>
    </div>`;
  }
  box.innerHTML = html;
}
