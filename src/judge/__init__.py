"""判定编排层：把一道题拆成若干互不依赖的原子判断，再把模型返回的判断用纯代码组合成判定轨迹。

本包的边界（不可妥协）：
**凡是读取数值概率并产出答案或告警的地方都留在 Python；其余一切进 spec。**
JEV 只返回类型化判定与概率，不返回推理文本，所以"怎么想"从来不在我们手里，
"怎么选、什么时候报警"才在——那部分是算术，必须是可审计的具名代码而不是配置。

模块划分
--------
- ``spec``           声明式 spec 的加载与静态校验（表、措辞、判断清单、state 组装）
- ``engine``         spec 驱动的 build()：唯一的生产载荷构造器
- ``strategies``     三个具名组合策略：唯一的生产轨迹构造器
- ``orchestrators``  S0（重构前）实现的逐字副本，只给 tests/ 对拍当参照系，生产不 import
- ``_legacy``        同一份 S0 compose 的第二份副本，与上一行互为校验

后两个模块的存在理由与限度见 ``tests/test_judge_contract.py``：它们必须继续逐字相等，
否则"新实现与重构前等价"这句断言就没有参照系了。
"""
from __future__ import annotations

from . import engine, spec, strategies  # noqa: F401
