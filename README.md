# JEV Grader (基于 JEV 的智能改卷系统)

![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
<!-- 推送到 GitHub 后，把下面两行的 OWNER/REPO 换成你的仓库，即可显示 CI 徽章：
[![tests](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)
-->

> ⚠️ **本项目只做改卷工作台，不做试卷提取**
>
> 本仓库不含 PDF 解析、抄题、公式转录等任何提取工具与代码。题库由使用者按
> [`templates/`](templates/README.md) 的模板制作后上传（或放进 `data/banks/`）。
> 仓库同样不分发任何题库与求解轨迹——题目内容的版权归各自权利人。

> 实测 JEV 模型：**jev-1.13.0**（2026-09；接口契约见 `src/jev_client.py`）。
> JEV 是 TypeSafe 的产品，本仓库与其无隶属关系，使用接口需自备账号与密钥。

让 JEV 像考生一样逐题作答，再按官方答案自动改卷：逐题判定、总分与得分率、
错题清单、依据强弱全部摊开给人看，也支持人工录入答案后改卷（客观题）。
三栏界面：**左栏题面 · 中栏答案 · 右栏 JEV 运算过程**。

这个项目关心的不是「AI 能不能答对」，而是**答案能不能被质疑和复核**——
每道题都留下判定依据、概率分布与置信度，并且会明确标出「这题我没依据」。
题库是输入，改卷是产品：把 PDF 变成可信题库是另一个独立的问题域，
因此从本仓库剥离，工作台的证据边界保持单一清晰。

## 功能特性

- 🧩 **三栏工作台**：题面（原文按句编号、可点击高亮）、答案、运算过程三栏联动
- 🔍 **判定轨迹可视化**：每题拆成若干原子判断（题型识别 → 证据定位 → 逐选项支持度 →
  最终判定 → 干扰项），步骤折叠但标题行保留结论摘要（如 `最高 B 93%`、`S11`）
- ▶️ **动态回放**：步骤依次点亮、证据句在左栏自动定位、答案延迟到「最终判定」才揭晓；
  支持暂停 / 重播 / 单步 / 调速（播放节奏为演示性质，界面以「演示回放」徽章标注）
- 📚 **题库模板与上传**：结构验收通过才入库；错误整体拒绝并逐条列出原因
- 📝 **答案汇总与自动改卷**：粘贴官方答案表（支持 `21 B`、`21:B`、多选 `9 ABC`），
  输出总分、得分率、正确率与错题清单，支持「只看错题」
- 🗂 **多试卷支持**：顶栏切换，各卷的题库、轨迹、答案表相互隔离；上传即注册
- 🧾 **诚实性优先**：`依据薄弱` / `两路判断分歧` / `低置信度` 三种告警；不可作答标注原因
  而不猜；题库未核验如实亮出（见「诚实性约束」）

## 环境要求

- Python 3.11+（CI 跑 3.11 与 3.13）
- Windows 10/11，或 Linux / macOS（`start.bat` 仅 Windows；其他平台用命令启动）
- JEV API Key：**仅真实求解需要**（浏览工作台、上传题库不需要）

## 一键启动（Windows）

双击 `start.bat`：首次运行自动安装依赖，随后打开
http://127.0.0.1:8848/ 。服务只监听本机。

### 默认端口

| 端口 | 用途 |
|---|---|
| **8848** | 工作台服务（FastAPI + 静态前端，同端口托管） |

### 自定义端口

```powershell
set JEV_GRADER_PORT=18848
start.bat
```

服务地址可用环境变量覆盖：`JEV_GRADER_PORT`（默认 8848）、`JEV_GRADER_HOST`（默认 127.0.0.1，仅本机）。

## 手动启动

```bash
pip install -r requirements.txt
cp .env.example .env          # 填入自己的 JEV API Key（只在真实求解时需要）
python src/server.py          # 打开 http://127.0.0.1:8848
python tests/run_all.py       # 零配额门禁自检（不联网，不消耗额度）
```

## 题库从哪来（模板与上传）

| 你想做的事 | 怎么做 |
|---|---|
| 第一次做题库 | [`templates/GUIDE.md`](templates/GUIDE.md)：手把手流程（准备材料 → 逐题型示例 → 上传排错表 → 自检清单） |
| 拿到模板 | 工作台左栏「下载题库模板」，或直接看 [`templates/bank_template.json`](templates/bank_template.json)（原创演示题，可直接上传跑通） |
| 查字段 | [`templates/README.md`](templates/README.md)：meta／sections／passages／items 每个字段、kind 一览、specs 怎么选 |
| 上传 | 顶栏「上传题库」；或把 JSON 放进 `data/banks/`（文件名即试卷 id）；或 `POST /api/upload` |
| 知道为什么被拒 | 结构验收不通过会**整体拒绝并逐条列出原因**（缺字段、坏选项字母、悬空篇章引用、kind 没有 spec 认领……） |
| 知道题库干不干净 | 「答案汇总 → 题库体检」：校验记录、损失账目、可作答性逐条原因 |

**验收器的边界（重要）**：它只验**结构**——字段、引用、编号、spec 认领。
「题面是否与原卷一致」在没有原件物证的情况下机器无从判断，所以上传题库的
「题库核验」会一直显示**未核验**，这是如实的。**题面错一行，改卷就错一题**，
内容正确性由制作者负责。

判定方式由题库自己声明（`meta.specs`），可选值只有四份 spec：

| specs | 适用于 | 条目 kind |
|---|---|---|
| `["english"]` | 英语类卷（默认） | `mcq`／`cloze`／`seven`，可省略（默认 mcq） |
| `["xingce"]` | 行测等中文单选卷 | 材料题给 `material`；「选词填空」小节自动按 cloze 处理 |
| `["math", "math_multi"]` | 数学卷 | 必写 kind：`math_single`／`math_multi` |

旧版硬编码注册的五份实测卷（english／math／xc_*）有两条迁移通道：
① 这些题库文件还留在原路径（`data/exam*.json`）就能按老 id 继续用，不需要重新上传；
② 想把它们转成上传件（比如统一到 `data/banks/` 管理），用
`python tools/envelope_to_upload.py <旧信封.json> --out-dir data/upload_ready`——
转换只做字段改名（`printed_no→no`、`section_name→section`、`passage_id→passage`、
`standard_answer→answer`）并补 `meta.specs`／`meta.id_prefix`，不动题目内容；
把旧轨迹目录复制成 `data/traces_<文件名>/` 后，上传即可直接看到历史判定。

## 目录结构

### 项目结构

```
.
├── src/
│   ├── bank.py           # 题库信封（bank envelope）唯一构造器：schema、校验记录、损失账目
│   ├── upload.py         # 上传管线：模板驱动的归一化 + 结构验收（error 即整卷拒绝）
│   ├── papers.py         # 试卷注册表：动态发现 data/banks/*.json
│   ├── solver.py         # 并发求解与轨迹落盘
│   ├── server.py         # FastAPI：静态资源 + /api/*（含 /api/upload、/api/template）
│   ├── jev_client.py     # JEV 客户端（Bearer 鉴权、429/529 指数退避）
│   └── judge/            # 判定编排
│       ├── spec.py       #   spec 加载与静态校验
│       ├── engine.py     #   构造发给 JEV 的载荷
│       ├── strategies.py #   三个具名组合策略（唯一读概率的地方）
│       └── specs/*.json  #   英语／数学单选／数学多选／行测 四份判据
├── web/                  # 三栏前端（原生 ES 模块，无构建步骤；KaTeX 内联）
├── templates/            # 题库模板与字段说明（bank_template.json + README.md）
├── tools/                # envelope_to_upload.py：旧版信封题库 → 上传件
├── tests/                # 七道零配额门禁（python tests/run_all.py）
├── .github/workflows/    # CI：干净 clone 上跑全套门禁
├── start.bat             # Windows 一键启动
├── CONTRIBUTING.md / SECURITY.md
└── README.md
```

### 运行时数据目录（不属于仓库）

以下目录由服务/工具在运行时生成或由使用者放置，均已通过 `.gitignore` 排除，
**不要**提交到版本库：

| 目录 | 作用 |
|---|---|
| `data/banks/` | 上传的题库（文件名即试卷 id） |
| `data/traces_<id>/` | 求解轨迹（调用 JEV 接口产生，重新求解需消耗配额） |
| `data/upload_ready/` | `tools/envelope_to_upload.py` 的迁移产物 |
| `data/math_items/` | 数学卷题图（条目 `images` 字段引用 `/data/...`） |
| `data/exam*.json` | 旧版直接可用的题库（迁移通道 ①） |

## 系统架构

```
┌─────────────────────────────────────────────┐
│  题库（使用者提供）                            │
│  templates/ 模板 → 顶栏上传 / data/banks/      │
└───────────────────┬─────────────────────────┘
                    │ ① upload.py 结构验收 + bank.py 封装
                    │    （error 即拒绝；缺损降级不删题）
                    ▼
┌─────────────────────────────────────────────┐
│  data/banks/<id>.json （v2 题库信封）          │
│  meta / sections / passages / items           │
│  validations（结构化记录）/ meta.loss（账目）    │
└───────────────────┬─────────────────────────┘
                    │ ② judge：spec → engine.build 构造载荷
                    │    （每题 N 个互不依赖的原子判断）
                    ▼
┌─────────────────────────────────────────────┐
│  JEV System One API（使用者自备密钥）          │
│  只返回类型化判定与概率，不产推理文本            │
└───────────────────┬─────────────────────────┘
                    │ ③ strategies.compose（唯一读概率的代码）
                    │    折成答案 + 三种告警
                    ▼
┌─────────────────────────────────────────────┐
│  data/traces_<id>/<id>.json （判定轨迹）        │
│  载荷原样落盘 → 缓存按内容指纹认领 / 可回放      │
└───────────────────┬─────────────────────────┘
                    │ ④ FastAPI :8848
                    ▼
┌─────────────────────────────────────────────┐
│  web/ 三栏工作台                               │
│  题面 ↔ 判定轨迹回放 ↔ 答案 / 汇总改卷          │
└─────────────────────────────────────────────┘
```

## 验证步骤

1. **门禁自检**（零配额、不联网）：

   ```bash
   python tests/run_all.py
   ```

   预期 7/7 通过；其中两道需要历史轨迹的门禁在干净 clone 上会打印
   **SKIP：那不是通过，是未生效**。

2. **上传模板卷跑通全流程**（不消耗配额）：

   ```bash
   # 打开工作台 → 空态左栏「下载题库模板」→ 顶栏「上传题库」选中该文件
   # 左栏出现 6 个题号，第 6 题（听力）标注不可作答，「答案汇总」可见题库体检
   ```

3. **真实求解**（消耗 API 配额）：`.env` 填入 `jevkey` 后，
   顶栏「求解本题 / 求解全卷」——轨迹会落盘到 `data/traces_<id>/`，
   重算同一题会按内容指纹复用缓存，不重复计费。

## API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/papers` | GET | 试卷清单（含轨迹版本、题库读取失败原因） |
| `/api/exam?paper=X` | GET | 题库信封（题目、篇章、体检记录） |
| `/api/traces?paper=X` | GET | 该卷全部判定轨迹 |
| `/api/run?paper=X` | GET | 最近一次求解的墙钟信息（并发耗时） |
| `/api/solve` | POST | 求解（`{paper, nos?, force?, concurrency?}`），消耗 JEV 配额 |
| `/api/clear` | POST | 删除判定轨迹（不带 `nos` 则清空该卷） |
| `/api/upload` | POST | 上传题库（`{name, bank}`）；结构验收不通过返回 422 + 逐条原因 |
| `/api/template` | GET | 下载题库模板（原创演示卷） |
| `/data/...` | GET | 题库引用的图片等静态文件（如数学卷题图） |
| `/static/...` | GET | 前端资源 |

## 配置（.env）

```bash
cp .env.example .env
# jevkey=apikey_xxxxxxxx    JEV API Key（仅服务端读取，前端从不接触）
```

Key 只从 `.env` 或环境变量读取，`.env` 已被 `.gitignore` 排除。

## 诚实性约束（项目核心设计）

1. **JEV 不产出推理文本。** 它只返回类型化判定与概率
   （`choice` / `noul` / `score` + `probabilities` + `confidence`）。
   所以右栏是**结构化判定轨迹的可视化**，不是模型的思维链——不存在"它这么想的"这种话。
2. **告警不等于正确。** 三个告警：`依据薄弱`（所有选项的支持度都低于 50%）、
   `两路判断分歧`（直接作答与逐选项支持度结论不同）、`低置信度`（自报置信度 < 50%）。
   数学卷实测的 4 道错题中**没有一道被这三个告警捕获**——第 1 题置信度 0.92、两路自洽，但答案是错的。
   「两路一致」只说明模型内部自洽。
3. **不可作答的题不猜。** 需要音频、需要生成数值或文本、选项本身是图片的题一律标注原因，不做填充。
4. **缺数据不静默回退。** 未注册的试卷 id 直接给指引态，不会悄悄换成默认卷的数据；
   题库文件损坏时把坏在哪个字段说出来，而不是从清单里消失；轨迹格式变更时旧轨迹标记为
   「轨迹过旧」，而不是按新格式误读。
5. **上传题库的核验边界写在明面上。** 结构验收通过 ≠ 内容正确；`meta.verified`
   对上传题库恒为 False，界面一直亮着「未核验」——盖章需要的物证（与原件逐题比对）
   不在本项目的范围内，我们不假装有。

## 实测记录（题库不随仓库分发，数字保留作参考）

| 试卷 | 可作答 / 入库 | 已求解（有轨迹） | 判定结果 |
|---|---|---|---|
| 高考英语（全国卷） | 35 / 67 | 35 | 平均置信度 93% |
| 2026 国考行测（副省级） | 113 / 120 | 110 | 平均置信度 63% |
| 2026 国考行测（地市类） | 112 / 122 | 102 | 平均置信度 62% |
| 2026 国考行测（行政执法类） | 123 / 130 | 108 | 平均置信度 62% |
| 高考数学（新Ⅰ卷）· 理科样板 | 11 / 19 | 11 | **6 / 10 正确（60%）**，答案已独立核对 |

口径说明：这些数字来自上述题库仍在仓库链路内制作时的实测；现在题库由使用者
上传，可作答题数与正确率取决于你自己的题库与官方答案。行测不可作答的部分主要是
**选项在原件里是图片**的图形推理题（副省级 7、地市类 9、行政执法 7 题），
它们以「不可作答：选项为图形」出现在账面上，而不是静默消失。

## 已知限制

- **行测的图形推理等小节不可作答**，因为它们的**选项在原件里是图片**，文本层里一个字母都没有。
  这类题在题库里以条目保留并写明原因，不静默删除。
- **正确率取决于你的题库与官方答案**：仓库不分发题库；中栏可手工录入答案，
  「答案汇总」里可批量粘贴。
- **上传题库不做内容核验**（见「诚实性约束」第 5 条）。
- 行测部分小节名为空或取自错误行（"（共N题）"上一行的启发式），属显示问题。
- 抄题端点（原先的 `src/extract/transcribe.py`）已随提取功能一并从本仓库移除
  （可从 git 历史 `63bff1b` 恢复）；需要在别处做 PDF → 题库的转换时，
  产出按 `templates/README.md` 对齐即可无缝接入。

## 七道零配额门禁

`python tests/run_all.py` 汇总：compose 对拍（spec+策略与重构前逐字节等价，
目标＝本机题库 + 随仓库模板卷，**任何机器上都真实生效**）、判定契约（S0 参照系
未被改动）、缓存身份（缓存按内容认领）、题库构造层与上传管线（不需要任何题库文件）。
历史请求重放与全路径重放需要本机的**历史轨迹**，没有时打印
**SKIP：那不是通过，是未生效**——汇总里会如实列出。CI（GitHub Actions）
在干净 clone 上跑同一套门禁，两条需要轨迹的门禁按设计显示 SKIP。

改动前的约定见 [CONTRIBUTING.md](CONTRIBUTING.md)；密钥与部署边界见
[SECURITY.md](SECURITY.md)。

## 安全

- **密钥零外泄**：JEV API Key 只在服务端使用，前端从不接触；`.env` 已被 `.gitignore` 排除。
- **仅本机**：服务默认只监听 `127.0.0.1:8848`，且 `/data/` 目录以静态资源方式挂载
  （用于数学卷题图）。**没有鉴权**，不要直接暴露到公网——细节见 [SECURITY.md](SECURITY.md)。

## 约束

- Python **3.11+**；pip 依赖见 `requirements.txt`（无构建步骤，前端零依赖工具链）。
- 默认端口 **8848**，可用 `JEV_GRADER_PORT` 覆盖。
- 只做**选择题与判断题**（含多选）；主观题、填空题、听力与写作不在产品范围内，
  在题库里明确标注「不可作答」及原因。
- 求解调用 TypeSafe JEV 接口，需自备 API Key 并自行承担配额消耗。

## 许可证

本项目基于 [Apache-2.0](LICENSE) 许可开源。
前端内联的 [KaTeX](https://github.com/KaTeX/KaTeX)（`web/vendor/katex/`）
以其自身的 MIT 许可发布（见 `web/vendor/katex/LICENSE.md`）。

## 社区

本项目在 [LINUX DO](https://linux.do) 社区进行开源推广，感谢社区佬友的交流、反馈与建议。

## 致谢 / 第三方组件

- [TypeSafe JEV](https://console.typesafe.ai/)（System One）：本项目调用的判定接口与模型。
- [FastAPI](https://github.com/fastapi/fastapi) / [uvicorn](https://github.com/encode/uvicorn) /
  [httpx](https://github.com/encode/httpx) / [Pydantic](https://github.com/pydantic/pydantic)
  及所有开源依赖的作者。
- 试卷原件与题目内容的版权归各自权利人所有，本仓库不主张任何权利、不随仓库分发；
  `templates/bank_template.json` 为原创演示题（随仓库分发，可自由使用）。
