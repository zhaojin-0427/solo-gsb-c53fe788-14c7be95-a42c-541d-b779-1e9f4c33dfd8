# 印前色彩软打样台（Prepress Color Soft-Proofing Station）

本地运行的 Web 应用：上传 **RGB / CMYK 的 TIFF、PNG 或 JPEG** 图像，选择**源 ICC 配置、印刷 ICC 配置、显示 ICC 配置**与渲染意图，由后端通过 **LittleCMS 2（直接 ctypes 调用 + Pillow ImageCms）** 完成全链路色彩转换，并在浏览器中进行显示器软打样、像素级 **CIEDE2000 色差**分析与区域取色。源色与印后色以 LittleCMS 原生 `TYPE_Lab_DBL`（C double）高精度 Lab 取得，不再经过 Pillow 8 位 `LAB` 模式的额外量化。

此外内置 **印刷配置版本发布与回归门禁** 模块：把同一配置族的候选印刷 ICC 存为按 SHA-256 固定的草稿，从已保存的代表性任务固化门禁基线，服务按草稿快照自动重跑并逐项给出 ΔE 均值 / P95 / 超阈面积比例 / 遮罩变化像素的通过-失败判定与变化像素定位；**全部通过才能在事务中发布为当前版本**，发布、撤回、恢复（回滚）均带期望版本号并发控制并写不可变哈希链审计。

软打样页还提供 **局部 CMYK 修色**：从**已保存任务**框选不超过 **64×64** 像素的矩形，给定目标 Lab、ΔE00 容差、单通道偏移绝对值上限（0–8）与总墨量上限（0–1020），后端以**原任务分色结果为起点、按原印刷 ICC** 穷举搜索一个对选区每个像素统一施加的**整数 CMYK 偏移**；逐像素满足通道 0–255、偏移及总墨量约束后，以 LittleCMS 回算选区平均 Lab，按「目标 ΔE00 → 偏移绝对值和 → (C,M,Y,K) 字典序」择优。**无可行解或最优解未达容差时标记“不可达”且不生成成品**；达标时导出**选区外 CMYK 字节与原分色逐字节一致、嵌入原印刷 ICC 的 TIFF** 及记录原任务/ICC 摘要/约束/偏移的报告。原任务印刷 ICC **缺失或 SHA-256 摘要不符一律拒绝**。

- 后端：Python 3.12 · FastAPI · SQLite · liblcms2 直接 ctypes（高精度 Lab）+ Pillow（图像 I/O/ImageCms）· NumPy
- 前端：原生 HTML / CSS / JavaScript（无框架、无构建步骤）
- 部署：Docker Compose 一键启动

---

## 一键启动

需要安装 Docker（含 Compose v2）。

```bash
docker compose up -d --build
```

启动后浏览器访问：

> **http://localhost:8000**

页面顶部导航可在两个视图间切换：

- **软打样**：上传图像、选择配置/意图/阈值，生成软打样与 ΔE 分析；在结果图上框选区域做局部统计，并可对**已保存任务**做**局部 CMYK 修色**（详见下文）；也可从「从已保存任务打开」下拉直接载入历史任务再框选修色；
- **印刷配置版本发布与回归门禁**：配置族、草稿、基线、门禁结果、版本发布/撤回/回滚与审计。

停止：

```bash
docker compose down
```

数据（上传的图像任务、ICC 配置、草稿/版本快照、门禁基线与产物、SQLite 数据库）保存在 Docker 卷 `softproof-data`（容器内 `/data`）中，容器重启后仍然存在。

### 不使用 Docker（本地开发）

```bash
pip install -r requirements.txt
# 还需要系统安装 LittleCMS 2 共享库，例如 Debian/Ubuntu：
# sudo apt-get install liblcms2-2
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 功能与色彩流程

### 处理链路

对每一张上传图像，后端依次执行（全部由 LittleCMS 完成转换，**绝不使用浏览器默认色彩解释**）：

1. **源图显示预览**：源像素 `源配置 → 显示配置`；
2. **沿 源 → 印刷 → Lab 回算色值**（Lab 均为 LittleCMS 原生 **TYPE_Lab_DBL 双精度**结果，不经过 8 位 PCS 量化）：
   - 源像素 `源配置 → 印刷配置` 得到设备 **CMYK** 值；
   - CMYK `印刷配置 → Lab PCS（double）` 得到印刷后 Lab；
   - 同时源像素 `源配置 → Lab PCS（double）` 得到原始 Lab；
3. **软打样图**：印刷后高精度 Lab `→ 显示配置`（可选择显示渲染意图，仍按所选显示 ICC 呈现）；
4. 对 `Lab_源` 与 `Lab_印刷后`（同一份双精度 Lab 结果）逐像素计算 **CIEDE2000（ΔE00）**；
5. 生成：
   - **ΔE 热图**（黑→蓝→青→绿→黄→红伪彩色）；
   - **热图叠加图**（半透明叠加在软打样上）；
   - **超阈值二值遮罩**（ΔE00 ≥ 阈值的像素为白色）；
   - 均值 / P95 / 最大 ΔE00 与**超阈值面积比例**统计。

**热图、遮罩、统计、框选区域的 Lab/色差值与导出报告全部取自同一份双精度 Lab/ΔE 结果**——阈值设在像素实际 ΔE00 附近时，不会因为源色/印后色被先写成 Pillow 8 位 LAB 再解码而错误翻转临界像素的遮罩归属或超阈面积比例。

输出给浏览器的 PNG 均嵌入 ICC 配置，浏览器不会按未标记数据自行解释：**源图显示预览与软打样嵌入所选择的显示配置**（像素本身位于该显示空间）；**ΔE 热图、遮罩与叠加图是无物理色度含义的合成可视化，嵌入 sRGB** 以保证颜色稳定呈现。

### 渲染意图

源→印刷、Lab→显示两个阶段分别可选：

- 感知（Perceptual）
- **相对色度（Relative Colorimetric，默认）**
- 饱和度（Saturation）
- 绝对色度（Absolute Colorimetric）

### 区域框选

在任一预览图上**鼠标拖拽框选矩形区域**，右侧面板显示该区域：

- 源 Lab 与印刷后 Lab 的均值/最小/最大；
- 印刷 **CMYK（C,M,Y,K 百分比，0% 无墨 / 100% 满版）**均值与最大；
- 区域 ΔE00 均值 / P95 / 最大 / 超阈值面积比例；
- 5×5 采样网格的各阶段数值。

### 局部 CMYK 修色（统一整数偏移搜索）

用于在不改版的前提下，对小块区域做整体追色。操作流程：

1. 先「生成软打样」保存一个任务，或在控制面板的 **从已保存任务打开** 下拉中选择历史任务；
2. 在结果图上**框选不超过 64×64 像素的矩形**（坐标自动回填；也可手工输入），点击 **用选区修色前平均 Lab 填充目标** 或直接填写：
   - **目标 Lab**：`L∈[0,100]`、`a/b∈[-128,127]`；
   - **ΔE00 容差**：非负数；
   - **通道偏移绝对值上限**：0–8 的整数（每个通道 `|Δ|` 的上限）；
   - **总墨量上限**：0–1020 的整数（四通道字节值之和的逐像素上限）；
3. 点击 **搜索统一 CMYK 偏移**。后端以**原任务分色结果为起点**，在 `(2s+1)^4`（s≤8，最多 17⁴=83 521）个**整数偏移**中穷举，并按**原印刷 ICC**（LittleCMS `TYPE_Lab_DBL`）回算 Lab：
   - 对选区**每个像素**：结果通道 `原值+偏移` 必须保持 **0–255（不做钳制）**、`|偏移通道|≤上限`、四通道之和 ≤ 总墨量上限；
   - 达标判据：**选区平均 Lab 与目标 Lab 的 CIEDE2000 ≤ 容差**（平均按像素等权）；
   - 择优严格按 **① 目标 ΔE00 → ② 偏移绝对值之和 → ③ (C,M,Y,K) 字典序** 取最小；
4. 页面显示**修色前/修色后选区软打样**（像素化放大）、修色后整图软打样、统一偏移、修色前后平均 Lab，以及两种残余色差：
   - 逐像素/平均**对目标**的 ΔE00（均值/P95/最大）；
   - 修色后相对修色前的 **残余 ΔE00**（均值/P95/最大）；
5. **不可达**（红色标记）分两种：`no_feasible_solution`（约束下不存在可行偏移）与 `tolerance_missed`（最优可行偏移仍超容差）——均**不生成 TIFF 与成品报告**，但仍给出最优可行偏移的前后软打样供判断；
6. **达标**时可下载：
   - `corrected_cmyk.tif`：全图分色 TIFF，**选区外 CMYK 字节与原分色逐字节一致**、选区内为原字节加统一整数偏移，**嵌入原任务印刷 ICC**（LZW 压缩；0=无墨/255=满版的 lcms 约定，不做通道反转）；
   - `correction_report.json`：记录**原任务**（job_id、原图哈希、尺寸/模式、两个渲染意图、阈值）、**三份 ICC 摘要**（名称/类/色彩空间/SHA-256）、选区与**全部约束**、搜索空间规模/择优规则、**最终偏移**、修色前后平均 Lab 与残余色差、成品 TIFF 的 SHA-256 及“选区外字节不变”标志。

**ICC 绑定校验**：修色只认原任务记录的印刷/显示 ICC。服务端比对当前文件的 SHA-256 与任务 `report.json` 记录的摘要——**印刷 ICC 缺失或摘要不符（或显示 ICC 缺失/不符）返回 `409` 并拒绝修色**，杜绝换配置后误用旧分色。相同请求（任务+选区+目标+约束）得到确定性的 `retouch_id` 与字节级一致的结果。

> 性能说明：可行性筛选是 O(候选数) 的区间/标量运算；Lab 回算只对选区内**唯一 CMYK 颜色**做。普通照片选区（唯一色远少于 4096）通常亚秒级；最坏情形（64×64 噪声图含 4096 个唯一色且数千候选可行）按核并行回算，约数秒至数十秒。

### 导出

每个任务可导出（单个或一键 ZIP 打包）：

- 源图显示预览 PNG、软打样 PNG、ΔE 热图 PNG、热图叠加 PNG、超阈值遮罩 PNG；
- `report.json`：**绑定原图 SHA-256、三份配置的名称/类/色彩空间/SHA-256 摘要、渲染意图与阈值参数、统计结果**。

### 确定性（重复输入结果一致）

任务 ID = 原图哈希 + 三份配置哈希 + 两个渲染意图 + 阈值 的 SHA-256。相同输入会命中同一任务目录与同一报告；报告不含时间戳，ZIP 条目时间固定为 1980-01-01，因此**重复输入产生字节级一致的结果**。

---

## 严格的配置/通道校验

以下情况后端一律返回 `422` 并拒绝处理（不会按浏览器默认色彩空间解释图像）：

| 情形 | 处理 |
| --- | --- |
| 未选择/找不到所需 ICC 配置 | 拒绝 |
| ICC 文件损坏、缺少 `acsp` 签名、lcms 无法打开 | 拒绝（上传配置时即拒绝） |
| 印刷配置不是 CMYK 输出配置（非 `prtr/spac` + `CMYK`） | 拒绝 |
| 显示配置不是 RGB 显示器配置（非 `mntr` + `RGB`） | 拒绝 |
| 图像通道数/色彩空间与源配置不匹配（例如 RGB 图配 CMYK 源） | 拒绝 |
| 勾选“使用内嵌配置”但图像无内嵌 ICC，或内嵌 ICC 无效 | 拒绝 |
| 16 位、索引色（P/LA/I 等）、GIF/WebP 等非支持格式 | 拒绝，请先在印前软件中转换并嵌入 ICC |
| RGBA 图 | 透明通道合成到白色后按 3 通道 RGB 处理（唯一的规范化操作） |
| 图像最长边超过上限（默认 3000px） | 拒绝 |

支持的输入容器：**TIFF / PNG / JPEG**；支持的图像色彩空间：**8 位 RGB、RGBA、CMYK、灰度 L**（灰度需搭配灰度源配置）。

---

## 内置与自定义 ICC 配置

首次启动时应用自动生成：

- `sRGB IEC61966-2.1 (built-in)`：默认可选的源 / 显示配置；
- `CIELAB identity (built-in)`：流水线内部使用的 Lab PCS 配置（ICC v2）；
- `Soft Proofing Demo SWOP-like CMYK (synthetic)`：**合成的、自包含的合法 CMYK 输出配置**，让开箱即可演示完整 RGB→CMYK→显示链路。其色域模型是简化模型（互补油墨 + sRGB 色度），**仅供演示，不可用于生产**。

生产使用时，在页面“**上传自定义 ICC 配置**”处上传真实的印刷机/打样机 CMYK 配置（如 GRACoL / SWOP / FOGRA `.icc/.icm`），或把文件放入数据目录（见下）。也可直接上传显示器校准后的 RGB 配置作为显示配置。

上传的配置按角色存放（容器内 `/data`）：

```
/data/profiles/source/    # 源（RGB/CMYK/灰度）
/data/profiles/press/     # 印刷（CMYK）
/data/profiles/display/   # 显示（RGB）
/data/profiles/builtin/   # 内置自动生成，勿手动修改
```

将 `.icc/.icm` 文件放进对应目录后刷新页面即可识别。

---

## 印刷配置版本发布与回归门禁

用于在更换/升级印刷 ICC 配置时，用固定的代表性任务做回归把关，防止配置改动造成大面积色差或超阈遮罩漂移。

### 概念

- **配置族（family）**：同一印刷机/工艺的一组草稿与已发布版本。族上有单调递增的 **族版本号 `revision`**，是发布/撤回/恢复操作的**期望版本号（乐观锁）**。
- **草稿（draft）**：上传到族上的候选印刷 ICC。服务校验其为合法 CMYK 输出配置后，按内容 **SHA-256** 存为不可变快照 `/data/release/snapshots/<sha256>.icc`；同一族再传新草稿会取代旧草稿的“开放”状态，但历史记录与快照保留。
- **基线（baseline）**：从一个**已保存的代表性任务**固化而来的自包含夹具。固定内容包括：
  - 原始图像字节（SHA-256 校验）；
  - 源 ICC（按任务的源配置解析，或图像内嵌 ICC）、显示 ICC（SHA-256 校验）；
  - 源→印刷、Lab→显示两个渲染意图与 ΔE 阈值；
  - 任务当时的二值超阈遮罩；
  - 四项允许上限：**ΔE 均值、ΔE P95、超阈面积比例（0..1）、遮罩变化像素数**。
- **门禁运行（gate run）**：服务**按草稿 ICC 的 SHA-256 取出固定快照**，替换基线任务的印刷配置，对族内**全部基线**重跑流水线，逐项返回 `passed/failed`、实测值、基线参考值、上限与中文原因；`run_id = SHA(草稿快照, 基线集合)`，**同输入重跑复用同一报告与产物**，因此历史报告可按版本复现。
- **版本发布**：只有全部门禁通过，才在**单个数据库事务**内完成：旧 `current → superseded`、草稿分配新版本号并置为 `current`、族 `current_version_id` 与 `revision` 前进、追加审计。失败则**保留草稿**、族当前版本不变。
- **撤回 / 恢复（回滚）**：可撤回当前版本（族暂时没有 current）；任何已发布的历史版本（被取代或已撤回）都可恢复为当前版本，恢复不需要重跑门禁——该版本发布时的门禁报告按 `gate_run_id` 仍可完整复现。两者都必须带期望版本号，过期返回 **409 Conflict**。
- **不可变审计**：族创建、草稿上传、基线固化、发布、撤回、恢复都会向 `audit_log` 追加一条带 `prev_hash/entry_hash` 的**哈希链**记录；表上建有 SQLite 触发器，任何 `UPDATE`/`DELETE` 都会被数据库直接拒绝。

### 页面操作流程

1. 在顶部进入 **印刷配置版本发布与回归门禁** 视图；
2. 左侧 **新建配置族**（如 “GRACoL2013-CRPC6”）；
3. 先到 **软打样** 视图跑一个代表性任务；回到发布视图，在 **① 门禁基线** 区从“最近已保存任务”下拉选中它，填写基线名称与四项允许上限，点击 **从所选任务固化基线**；
4. 在 **②** 区上传候选印刷 ICC 草稿（可打标签）；
5. 点击 **③ 运行回归门禁**：每个基线给出四项判定表、候选软打样/候选超阈遮罩/变化像素定位图（红=新增超阈、蓝=消失超阈、灰=一致）以及变化像素坐标 CSV 下载；
6. 全部通过后点 **④ 在事务中发布**（按钮显示当前族版本号作为期望版本号；失败时按钮禁用）；
7. 在 **版本与回滚** 表中可撤回当前版本、恢复（回滚）到任意历史版本、下载该版 ICC、打开发布时的门禁报告；左侧底部可展开查看当前族的哈希链审计日志。

### 进行中任务与历史版本的快照绑定

发布时，该版 ICC 以**不可变文件名** `release-<sha256前16位>.icc` 写入 `/data/profiles/press/` 并立即出现在软打样页的印刷配置下拉中；文件按内容寻址、**永不覆盖**。普通软打样任务的 `job_id` 由“图像哈希 + 三份配置哈希 + 意图 + 阈值”决定，因此：

- 发布新版本不会改动旧文件，**进行中/已存在的任务继续绑定原快照**，同输入永远命中原任务目录与报告；
- 新版本发布后，旧版本状态变为 `superseded` 但其 ICC 与门禁产物仍在，**历史报告可按版本（`gate_run_id`）字节级复现**；
- 回滚只切换数据库中的“当前版本”指针，不修改任何 ICC 文件。

### 模块数据目录（容器内 `/data/release`）

```
/data/release/
├── snapshots/<sha256>.icc          # 草稿/版本 ICC 不可变快照（内容寻址）
├── baselines/<baseline_id>/       # 基线自包含夹具
│   ├── image.bin                  #   固定的原始图像
│   ├── source.icc                 #   固定的源 ICC（内嵌源时为空）
│   ├── display.icc                #   固定的显示 ICC
│   ├── press-ref.icc              #   基线原印刷 ICC（参考复算用）
│   └── mask_ref.npy               #   基线二值超阈遮罩
└── gates/<run_id>/<baseline_id>/  # 门禁产物：softproof/mask/diff PNG、
                                   # changed_pixels.csv、mask_changed.npy、report.json
```

---

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/profiles` | 按 source/press/display 分组列出可用配置 |
| POST | `/api/profiles/{source\|press\|display}` | 上传并严格校验 ICC 配置（multipart `file`） |
| POST | `/api/jobs` | 上传图像并执行软打样（multipart 表单，见下） |
| GET | `/api/jobs` | 最近任务列表（SQLite） |
| GET | `/api/jobs/{id}` | 任务 JSON 报告 |
| GET | `/api/jobs/{id}/images/{name}.png` | preview / softproof / heatmap / overlay / mask |
| GET | `/api/jobs/{id}/report` | 下载 report.json |
| GET | `/api/jobs/{id}/export.zip` | 全部 PNG + JSON 打包下载 |
| POST | `/api/jobs/{id}/region` | 区域分析，body：`{x,y,width,height}` |
| POST | `/api/jobs/{id}/retouch` | 局部 CMYK 修色搜索，body：`{x,y,width,height,target_lab:[L,a,b],tolerance_de,max_shift,total_ink_limit}`（见下）；原任务 ICC 缺失/摘要不符返回 409 |
| GET | `/api/jobs/{id}/retouch/{rid}` | 修色结果 JSON（含状态、偏移、前后 Lab、残余色差、产物链接） |
| GET | `/api/jobs/{id}/retouch/{rid}/images/{name}.png` | `before_crop` / `after_crop` / `after` 软打样 PNG（嵌入显示 ICC） |
| GET | `/api/jobs/{id}/retouch/{rid}/corrected_cmyk.tif` | 成品 TIFF（仅达标存在，否则 404；选区外字节不变、嵌入印刷 ICC） |
| GET | `/api/jobs/{id}/retouch/{rid}/correction_report.json` | 修色报告（仅达标存在，否则 404） |

### 发布与回归门禁 API（前缀 `/api/release`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/families` | 配置族列表（含当前版本、开放草稿、版本/基线数、revision） |
| POST | `/families` | 新建配置族，body：`{name}` |
| GET | `/families/{fid}` | 族详情（全部版本、基线、最近门禁） |
| POST | `/families/{fid}/drafts` | 上传候选印刷 ICC 草稿（multipart `file` + 可选 `label`），严格 CMYK 校验，按 SHA-256 固定快照 |
| GET | `/versions/{vid}` | 版本/草稿详情（发布版本附带其门禁报告，可按版本复现） |
| GET | `/versions/{vid}/icc` | 下载该草稿/版本绑定的固定 ICC |
| GET | `/families/{fid}/baselines` | 列出基线 |
| POST | `/families/{fid}/baselines` | 从已保存任务固化基线，body：`{name, job_id, limit_de_mean, limit_de_p95, limit_over_ratio, limit_changed_pixels}` |
| POST | `/families/{fid}/gates` | 运行回归门禁，body：`{draft_version_id}`；逐项返回判定/实测/原因/定位 |
| GET | `/gates/{run_id}` | 门禁报告（确定性 run_id，可历史复现） |
| GET | `/gates/{run_id}/baselines/{bid}/softproof.png` | 候选软打样 |
| GET | `/gates/{run_id}/baselines/{bid}/mask.png` | 候选超阈遮罩 |
| GET | `/gates/{run_id}/baselines/{bid}/diff.png` | 变化像素定位图（红新增/蓝消失/灰一致） |
| GET | `/gates/{run_id}/baselines/{bid}/changed_pixels.csv` | 变化像素坐标清单（y,x,kind） |
| POST | `/families/{fid}/publish` | 事务内发布，body：`{draft_version_id, expected_revision, label?}`；门禁失败 422，期望版本号冲突 409 |
| POST | `/versions/{vid}/withdraw` | 撤回当前版本，body：`{expected_revision, reason?}` |
| POST | `/versions/{vid}/restore` | 恢复/回滚到历史版本，body：`{expected_revision, reason?}` |
| GET | `/audit?family_id=&limit=` | 哈希链不可变审计列表 |

错误码：请求语义/状态非法 `422`，资源不存在 `404`，期望版本号冲突 `409`；局部修色中原任务 ICC 缺失或摘要不符也返回 `409`。

`POST /api/jobs/{id}/retouch` JSON 字段：

- `x,y,width,height`：选区（整数；1≤width,height≤64，不得越界）；
- `target_lab`：`[L,a,b]`，L 0–100、a/b −128–127；
- `tolerance_de`：非负 ΔE00 容差；
- `max_shift`：每通道偏移绝对值上限（0–8 整数）；
- `total_ink_limit`：总墨量上限（0–1020 整数）。

返回 `status` 为 `reached` / `unreachable`（`reason=no_feasible_solution|tolerance_missed`）。

`POST /api/jobs` 表单字段：

- `image`：图像文件；
- `source_profile` / `press_profile` / `display_profile`：配置 ID；
- `source_intent` / `proof_intent`：`perceptual | relative_colorimetric | saturation | absolute_colorimetric`；
- `threshold`：ΔE00 阈值（默认 3.0）；
- `use_embedded`：`true/false`，使用图像内嵌 ICC 作为源配置。

---

## 实现说明

- **色差公式**：`app/colorscience.py` 中为矢量化 CIEDE2000，已对照 Sharma 等 (2005) 标准测试数据验证（最大偏差 < 1e-3）。
- **Lab 精度**：源色与印后色由 `app/lcms_native.py` 直接 ctypes 调用 `liblcms2`，以 `cmsCreateTransform` + `TYPE_Lab_DBL`（native C double，D50 CIELAB）取回；ΔE00、热图、遮罩、统计、框选区域与 `report.json` 共用这同一份未量化结果，落盘的 Lab/ΔE 数组也以 float64 保存。仅保留 ICC v2 8 位 Lab 编解码函数用于与历史路径对比的测试。
- **合成 CMYK 配置**：`app/lcms_native.py` 通过 ctypes 调用系统 `liblcms2` 的 `cmsCreateProfilePlaceholder` / CLUT16 pipeline API 生成 17³/17⁴ 网格的合法 ICC v2.4 CMYK 配置，保证开箱演示可用。合成配置在落内存字节时统一清零 ICC 头 24–35 字节的创建时间字段，保证内置 sRGB/Lab/演示配置**跨重启字节级稳定**（否则 lcms 会写入墙钟时间，导致 SHA-256 摘要每次启动漂移、破坏任务绑定）。
- **局部 CMYK 修色**：见 `app/retouch.py`（穷举搜索、区间化可行性筛选、按唯一颜色回算 Lab、线程池并行、成品导出）与 `app/main.py` 中的 `/retouch` 路由；`DeviceToLabTransform`（`app/lcms_native.py`）提供可重复调用的设备 8 位→`TYPE_Lab_DBL` 转换句柄。
- **环境变量**：
  - `SPD_DATA_DIR`（默认 `./data`）：数据根目录；
  - `SPD_MAX_DIMENSION`（默认 `3000`）：允许的图像最长边像素；
  - `SPD_MAX_UPLOAD_MB`（默认 `50`）：上传大小上限。

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由
│   ├── release_api.py   # 发布/门禁 HTTP API
│   ├── releases.py      # 配置族/草稿/基线/门禁/发布/撤回/恢复/审计 服务
│   ├── pipeline.py      # 色彩流水线、导出（支持按快照字节重跑）
│   ├── retouch.py       # 局部 CMYK 修色：统一整数偏移穷举搜索/约束/择优/TIFF 与报告
│   ├── colorscience.py  # CIEDE2000、热图（高精度 Lab 由 lcms_native 取得）
│   ├── profiles.py      # ICC 配置管理与严格校验
│   ├── regions.py       # 框选区域统计
│   ├── database.py      # SQLite（含发布模块表、不可变审计触发器、写事务锁）
│   ├── lcms_native.py   # liblcms2 ctypes 绑定（TYPE_Lab_DBL、可复用 transform、内置配置生成）
│   ├── config.py
│   └── static/          # 前端（软打样 + 局部修色 + 发布门禁视图）
├── tests/               # pytest 端到端测试（软打样 + 局部修色 + 发布门禁）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### 运行测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

测试覆盖：软打样原有全链路与校验，发布门禁模块（配置族/草稿 CMYK 校验、基线固化与参数校验、相同快照通过、异配置检出变化像素并拒绝发布、门禁 run 确定性与历史复现、发布后配置可选与不可变文件名、期望版本号 409、撤回/恢复状态机、审计哈希链与 UPDATE/DELETE 触发器拦截），以及局部 CMYK 修色（零偏移精确命中、达标成品 TIFF 选区外逐字节不变且嵌入原印刷 ICC、未达容差/无可行解不可达且不产出成品、参数/坐标校验、印刷 ICC 篡改与缺失返回 409、retouch_id 确定性、通道越界为硬约束、可行性筛选对暴力法的等价性、择优次序、64×64 选区穷举）。
