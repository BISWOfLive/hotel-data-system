"""比价平台实现包。

每个平台模块在类上带 ``@register`` 装饰器;由
:func:`hoteldata.domains.compare.load_platforms` 统一导入触发注册。

**不要把 import 写在这里** —— 平台模块依赖 ``contract`` / ``geo`` / ``human`` /
``price``,而 ``contract`` 又经由本包 ``__init__`` 暴露,直接 import 会形成循环。
见 :mod:`hoteldata.domains.compare` 的模块文档。
"""

from __future__ import annotations
