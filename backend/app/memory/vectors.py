"""记忆库的向量表。

`rowid` 就是 `memories.id`。表名和维度记录键都与知识库分开：
知识库换嵌入模型时会整表重建 `chunks_vec`，不能顺带把记忆向量删掉。
"""

from __future__ import annotations

from app.vector_store import VectorTable

MEMORIES_VEC = VectorTable("memories_vec", "vec_dim:memories")
