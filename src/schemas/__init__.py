"""模型 spec 的 Pydantic 定义（防线①：结构合法性由 schema 保证）。

M1 只支持单队列多服务台模型（single_queue_multi_server），
场景族边界见 docs/non-goals.md（D-006：排队/服务流程场景）。
"""
