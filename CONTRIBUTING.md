# 贡献指南

这个项目最在意的不是"AI 答得多准"，而是**每一条结论都能被质疑和复核**。
贡献之前，请先读 README 的「诚实性约束」一节——它是这个仓库的宪法，
代码评审的第一条标准就是它。

## 本地开发

```bash
pip install -r requirements.txt
cp .env.example .env          # 填入自己的 JEV API Key（只在真实求解时需要）
python tests/run_all.py       # 提交前必须全绿；SKIP 不等于通过
python src/server.py          # http://127.0.0.1:8848
```

题库由使用者按 `templates/README.md` 制作并上传；仓库不分发任何题目内容，
测试所需的样例题库是 `templates/bank_template.json`（原创演示题）。

## 改动前必须知道的边界

1. **读概率、产答案、报警的算术只住在 `src/judge/strategies.py`**。
   它是封闭集合——新增策略的唯一正当理由是新题型真的需要新的算术，
   而且要有真实轨迹证据。判据表与措辞进 spec（`src/judge/specs/*.json`），
   一律在加载期静态校验。
2. **`src/judge/spec.py` 是照妖镜**：spec 的形状、引用、槽位都在加载期查完，
   配错要当场炸，不许把错误留成"看起来正常的轨迹"。
3. **不许静默丢数据**。缺损题降级为不可作答并写明原因（`answerable=False` +
   Validation 记录），绝不删除条目；发现新缺陷就新登记一个校验 code
   （`src/bank.py` 的 `validation()` 文档串 + `render_validation` 表）。
4. **界面上的「推断」必须说出来**。轨迹缺字段时前端按步骤名推断语义，
   这类结论一律带标注（见 `web/app.js` 的 `linkageNote` / `shapeOf`）。
5. **`src/judge/_legacy.py` 与 `src/judge/orchestrators.py` 是 S0 参照系的
   冻结副本，一行都不许改**。发现 bug 改 `strategies.py` 并同步更新门禁基线。
6. **`trace_version` 只在破坏性变更时递增**——递增会把本地全部缓存轨迹标成
   「过旧」，逼一次烧配额的重算。

## 提交要求

- `python tests/run_all.py` 必须全绿。缺数据的 SKIP 要在 PR 描述里说明
  ——把 SKIP 当成通过会被直接打回。
- 改了发给 JEV 的载荷（`engine.build`）或组合算术（`strategies.compose`），
  必须解释 `golden_compose` 为什么仍然等价，或为什么基线需要重钉、理由是什么。
- 涉及新校验 code、新 spec 字段、模板字段的改动，一并更新
  `templates/README.md` 与 `src/bank.py` 的 code 登记表。

## 报告问题

- 判定错误请附：题库文件（可脱敏）+ 轨迹 JSON（`data/traces_<id>/<id>.json`）
  + 完整复现命令。轨迹里带着当时发给 JEV 的载荷，是唯一能复现的证据。
- API Key 泄露或安全问题见 [SECURITY.md](SECURITY.md)。
