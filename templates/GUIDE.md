# 从零制作一份题库（手把手）

这份文档教你**动手做出一份能上传、能改卷的题库**。字段的完整清单在
[README.md](README.md)（速查用），本文按"从一份卷子到改卷跑通"的顺序走一遍。

做之前先接受一条边界（它决定你要花多少精力复核）：

> **上传验收器只验结构**——字段、引用、编号、spec 认领。它查不出"题面抄错了一个字"
> 或"选项抄反了"。**题面错一行，改卷就错一题**，内容正确性由你负责。
> 所以流程的最后一步永远是"拿几道题和原件逐字核对"。

---

## 第 0 步：准备材料

你需要先把卷子整理成文本（这一步本项目不提供工具，用什么办法都行）：

- 每道题的**题干**（选择题就抄题干，不要抄选项）；
- **选项**（A/B/C/D…，字母要连续，A 到 D 中间不能缺）；
- **阅读材料**（原文段落；完形填空的原文要把空位标出来，见第 4 步）；
- **官方答案**（有就带上，改卷对照用；没有也能做题库，答案可以之后在界面里粘贴）；
- **分值**（可选，决定总分的计算）。

## 第 1 步：复制模板

把 `bank_template.json` 复制一份，**改成你的卷名**——文件名就是试卷 id
（例如 `2025期末英语.json`），顶栏用它区分试卷，轨迹目录也叫 `data/traces_2025期末英语/`。

打开文件，你会看到三个部分：`meta`（这是份什么卷）、`passages`（阅读材料，可空）、
`items`（题目清单）。sections 可选（见第 5 步）。

## 第 2 步：填 meta（3 个必填字段）

```json
"meta": {
  "title": "2025 学年期末英语",
  "subject": "英语",
  "specs": ["english"],
  "has_latex": false
}
```

- `specs` 决定用哪套判定方式，三选一：

  | 你的卷子 | specs 写什么 | 每道题的 kind |
  |---|---|---|
  | 英语类（四选一/完形/七选五） | `["english"]` | `mcq`（默认可省略）/ `cloze` / `seven` |
  | 行测等中文单选卷 | `["xingce"]` | `mcq`；材料题把材料放进 `material` |
  | 数学卷 | `["math", "math_multi"]` | **必写**：单选 `math_single`、多选 `math_multi` |

- `has_latex`：题干或选项里有 `$…$` 公式（数学卷）就填 `true`，界面走 KaTeX 渲染。

## 第 3 步：写第一道题（最小例子）

```json
"items": [
  {
    "no": 1,
    "stem": "What did the writer do first?",
    "options": { "A": "He read a book.", "B": "He watered the flowers.",
                 "C": "He opened the shop.", "D": "He called a friend." },
    "answer": "B"
  }
]
```

要点：

- `no` 是**卷面题号**（整数即可，连续不连续都行；缺了会自动编号并记一条说明）；
- `options` 的键必须是 **A–G 的单个大写字母**，而且**字母要连续**——
  写了 A、B、D 缺 C 会被整卷拒绝（"选项不齐不猜"），请补全或删掉；
- `answer` 写官方答案（可省略；多选写 `"ABC"`，顺序随意）。写了题库外字母会被忽略并记告警；
- 判断题：`"kind": "judge"`，`options` 只给 `{"A": "正确", "B": "错误"}` 两项。

## 第 4 步：阅读材料与完形填空

材料放 `passages`，题目用 `passage` 字段引用：

```json
"passages": [
  { "id": "P1", "label": "Passage One",
    "paragraphs": ["For thirty years, Mr. Bell kept a small bookshop…",
                   "Regulars say he never refused a customer…"] }
],
"items": [
  { "no": 21, "passage": "P1", "stem": "What did Mr. Bell do…", "options": {…} }
]
```

完形填空（`kind: "cloze"`）在材料原文里把空位写成 `[题号]____`（题号要等于该题的 `no`）：

```json
"paragraphs": ["Most of them began as parking lots. [3]____ , in one small town, …"]
```

系统据此登记题空，并给这道题自动取"前后各一句"的上下文参与判定。
七选五（`kind: "seven"`）同理。多段材料各给一个 `id`（P1、P2…）。

## 第 5 步：小节与分值（可选）

想在界面上分小节、按题计分，给条目加 `section`（可选 `part`），并在顶层声明：

```json
"sections": [ { "name": "阅读理解", "part": "一", "per": 2, "count": 2 } ],
"items": [
  { "no": 1, "part": "一", "section": "阅读理解", "stem": "…", "options": {…}, "score": 2 }
]
```

- `per` 是"每题分值"，`full` 是小节总分（二选一）；`count` 是期望题数，对不上只记告警；
- 条目自带 `score` 时优先用 `score`，否则退回小节的 `per`；
- 没声明小节也能用，系统会按条目里的小节名自动归桶；没给 `section` 的题会归入
  「未分节」——如果它不在你声明的 sections 里，会出现一条 `section_undeclared` 告警
  （不影响入库，只是如实登记）。

## 第 6 步：特殊题型的处理

| 情况 | 怎么写 |
|---|---|
| 公式（数学卷） | 行内写 `$…$`，如 `"stem": "已知 $x>0$，求 $\\frac{1}{x}+x$ 的最小值"`，并在 meta 里 `"has_latex": true` |
| 小问 / 表格 / 附注 | 可选字段 `subs`（字符串数组）、`table`（`{"header": […], "rows": [[…]]}`）、`appendices` |
| 选项在原件里是图片（图形推理） | 写不了选项就**显式声明不可作答**：`"answerable": false, "notice": "选项在原件中是图片"`——整卷不会被拒，界面上如实标注 |
| 听力 / 写作 / 语法填空 / 数学填空解答 | 天然超出"只改选择判断题"的边界，**可以不给选项**；系统按题型自动标注不可作答原因，无需你声明 |
| 一题多个答案（多选） | 数学多选：`"kind": "math_multi"`；文科多选：`"answer": "ACD"` |
| 没有官方答案 | 省略 `answer`；上传后在界面中栏手工录入，或「答案汇总」批量粘贴 |

## 第 7 步：上传与排错

三种方式任选：顶栏「上传题库」/ 放进 `data/banks/` / `POST /api/upload`
（详见 [README.md](README.md) 的"三种上传方式"）。上传被拒时会**逐条**列出原因，
对照下表修：

| 错误 code | 什么意思 | 怎么修 |
|---|---|---|
| `upload_item_invalid` | meta 缺 title/subject/specs，或条目缺 stem、no 不是整数 | 按提示补字段 |
| `options_letters_invalid` | 选项键不是 A–G 单字母 | 改成 `"A"`、`"B"`… |
| `options_incomplete` | 选项数不足或字母不连续（如缺 C） | 补全选项，或改成判断题 `judge` |
| `passage_unknown` | 条目引用了不存在的篇章 id | 检查 `passage` 与 `passages[].id` 是否拼错 |
| `spec_unknown` | `meta.specs` 写了不存在的 spec 名 | 只能填 english / xingce / math、math_multi |
| `kind_unclaimed` | 数学卷的**可作答**条目 kind 没写或写错 | 写 `math_single` / `math_multi` |
| `standard_answer_invalid`（告警） | 答案里有本题不存在的字母 | 已自动忽略该字母，核对后改对 |
| `section_count_mismatch` / `section_undeclared`（告警） | 小节题数对不上 / 小节没声明 | 不影响入库，核对小节划分 |
| `stem_suspicious`（告警） | 题干过短或为空 | 核对是否抄漏 |

## 第 8 步：上传后的自检（必做）

1. **题库体检**：「答案汇总 → 题库体检」看校验记录与损失账目——有没有告警、哪些题不可作答、原因是什么；
2. **抽题核对**：随便点 3 道题，把左栏题面和原件**逐字**比一遍（这是唯一机器查不了的环节）；
3. **试求解一题**：配好 `.env` 后点「求解本题」，看右栏轨迹——如果「依据薄弱」告警亮起，
   通常说明这道题依赖的材料（图表/听力）不在文本里；
4. **对答案**：中栏录入或批量粘贴官方答案，用「只看错题」过一遍错题清单。

## 用 AI 帮忙抄题？可以，但要复核

让多模态模型按模板输出 JSON 是可行的省力路径（把
[README.md](README.md) 的字段表当协议发给它，要求"只抄不编，抄不动就标不可作答"）。
两条经验：

- 上传验收器能挡住**结构错误**（字段缺漏、引用悬空、编号错乱），这是它的强项；
- 但它**查不出内容错误**：抄错一个字、编造一个选项、公式漏了分数线，机器都看不出来。
  所以第 8 步的抽题核对不能省；题量大时至少按题型分层抽样。

> 本仓库早期版本自带一条"抄题 + 纯代码验收"流水线（锚点定位、双向比对、公式墨迹物证），
> 因产品边界收敛而移除——需要重建时可在 git 历史 `63bff1b` 找到参考实现。
