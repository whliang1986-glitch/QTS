"""Alembic 运行环境：只支持在线模式，连接由 server.storage.migrate 传入。"""

from __future__ import annotations

from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("请通过 server.storage.migrate 运行迁移，不要直接调用 alembic 命令行")

# 整次迁移在调用方开启的同一个事务里完成；batch 模式会重建表并丢失触发器，迁移必须自行重建触发器
context.configure(connection=connection, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
