"""业务域层:``collect``(提取) · ``session``(登录管家) · ``ops``(巡检/清理/冷备/自检)。

**依赖纪律**(段1 §5.2 硬约束):

1. 依赖方向**单向向下**,禁止反向 import;
2. **域之间不互相 import** —— ``collect`` 需要会话就去调 ``session`` 的 service,
   不碰它的实现;
3. **域之间不直接 join 别人的表**。
"""

from __future__ import annotations

__all__: list[str] = []
