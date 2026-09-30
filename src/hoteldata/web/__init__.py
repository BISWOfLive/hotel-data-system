"""Web 接入层(FastAPI + Jinja2 服务端渲染)。

三个部分:

=========================================  ==================================================
模块                                         内容
=========================================  ==================================================
:mod:`~hoteldata.web.routes.health`          ``/healthz``(存活 + DB 可达)、``/status``(运行态 JSON)
:mod:`~hoteldata.web.routes.admin`           ``/admin`` 登录 + 总览(**统一口令 + 操作审计**)
:mod:`~hoteldata.web.routes.admin_pages`     ``/admin/*`` 各业务页面(酒店/账号/机器人/群绑定/比价/运行)
:mod:`~hoteldata.web.auth`                   PBKDF2 口令 + 签名 cookie 会话 + 审计写入
:mod:`~hoteldata.web.templating`             Jinja2 环境(模板随包发布)
=========================================  ==================================================

★ **不引入前端构建链**(总纲 §7.8):服务端渲染 + 一份手写 ``static/admin.css``。
  没有 npm、没有打包步骤、没有 node_modules。

★ 段1/段2 曾明确"不做 Web 后台";段3 之后(Phase 3)按总纲 §7.8 落地,
  范围是「账号/机器人/比价酒店增删 + 统一口令登录 + 操作审计」,
  **明确不做**:角色区分、权限树、API 鉴权细化。
"""

from __future__ import annotations

__all__: list[str] = []
