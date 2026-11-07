"""统一事件时间线与记忆。

模块分工：

- `db`        ：建库建表、连接、统计
- `identity`  ：person_key 归一（网页 / QQ / 绑定后的同一人）
- `events`    ：写原始时间线
- `visibility`：读取前的可见性硬过滤
- `sensitivity`：敏感内容识别
- `store`     ：记忆读写、去重、向量索引
- `extract`   ：从一轮对话里提炼候选记忆
- `retrieval` ：检索、排序、拼装提示词片段
- `pipeline`  ：把上面这些接进网页与 QQ
- `maintenance`：衰减与过期清理（命令行）

导入本包不会创建数据库；`db.init_schema()` 才写磁盘。
"""

__all__: list[str] = []
