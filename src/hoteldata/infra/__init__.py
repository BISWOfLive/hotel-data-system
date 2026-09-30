"""基础设施层:db · browser · session_store · rate_limit · tasks · crypto · http · atomic · paths。

**依赖纪律**(段1 §5.2 硬约束):
  1. 依赖方向**单向向下**,禁止反向 import。
  2. **域之间不互相 import**。
  3. **域之间不直接 join 别人的表**。
"""

from __future__ import annotations

__all__: list[str] = []
