# vector_store.py - ChromaDB 真向量检索模块（多 embedding 后端）
# ---------------------------------------------------------------------------
# 职责（对应业务需求分析报告 §4.3 知识检索模块的向量化升级）：
#   1) 初始化 ChromaDB 持久化客户端（默认 faq_chromadb/，已被 .gitignore 屏蔽）
#   2) 自定义 embedding 函数：文本 → 向量（后端三选一，均免注册新平台）
#        - backend=dashscope   : 阿里云百炼 qwen3.7-text-embedding，复用已有 QWEN key（推荐）
#        - backend=siliconflow : SiliconFlow BAAI/bge-m3（需 SILICONFLOW_API_KEY）
#        - backend=local       : sentence-transformers 加载本地 BAAI/bge-m3（离线，免 key）
#   3) 把 客服FAQ.md 解析出的问答对写入 collection（携带 category 元数据）
#   4) 相似度查询（cosine，可按 category 过滤）
# 与 cs_supervisor.py 的关系：lookup_faq 委托本模块；本模块不可用时回退轻量检索。
# ---------------------------------------------------------------------------
from __future__ import annotations

import os
from typing import Optional

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")  # 关闭 chroma 遥测，避免日志噪音

import chromadb
from chromadb.config import Settings
from chromadb.utils.embedding_functions import EmbeddingFunction

MODEL_BGE_M3 = "BAAI/bge-m3"
MODEL_QWEN_EMBED = "qwen3.7-text-embedding"             # DashScope（百炼）嵌入模型
SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # 与 cs_supervisor 的 chat 同域名
EMBED_BATCH = int(os.getenv("AFTERSALE_EMBED_BATCH", "16"))  # 一次 API 调用最多嵌入条数（DashScope 单次上限 20 行）


class BgeM3EmbeddingFunction(EmbeddingFunction):
    """ChromaDB 自定义 embedding 函数：把文本批量转成向量。

    ChromaDB 要求 embedding 函数是「可调用对象」，签名 __call__(input: list[str])
    -> list[list[float]]；collection 建好后，add/query 时会自动用它嵌入文本。
    """

    def __init__(self, backend: str = "dashscope", model: Optional[str] = None,
                 api_key: Optional[str] = None):
        self.backend = backend
        if model is None:
            model = MODEL_QWEN_EMBED if backend == "dashscope" else MODEL_BGE_M3
        self.model = model
        self._dim = int(os.getenv("AFTERSALE_EMBED_DIM", "1024"))  # 仅 qwen3.7 支持 256~2560
        if backend in ("siliconflow", "dashscope"):
            from openai import OpenAI
            if not api_key:
                raise ValueError(f"backend={backend} 需要 API Key（见 .env.example）")
            base = SILICONFLOW_BASE if backend == "siliconflow" else DASHSCOPE_BASE
            self._client = OpenAI(api_key=api_key, base_url=base)
        elif backend == "local":
            from sentence_transformers import SentenceTransformer
            self._st = SentenceTransformer(model)  # 首次运行会从 HuggingFace 下载模型
        else:
            raise ValueError(f"未知 backend: {backend}（可选 dashscope / siliconflow / local / off）")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if self.backend == "siliconflow":
            resp = self._client.embeddings.create(model=self.model, input=texts)
            return [d.embedding for d in resp.data]
        if self.backend == "dashscope":
            resp = self._client.embeddings.create(
                model=self.model, input=texts, dimensions=self._dim)
            return [d.embedding for d in resp.data]
        # local：normalize_embeddings=True 使向量为单位长度，与 cosine 距离语义一致
        return self._st.encode(texts, normalize_embeddings=True).tolist()

    def __call__(self, input: list[str]) -> list[list[float]]:
        """ChromaDB 可能一次传入全部文档，分批防止 API 长度限制。"""
        out: list[list[float]] = []
        for i in range(0, len(input), EMBED_BATCH):
            out.extend(self._embed(input[i:i + EMBED_BATCH]))
        return out


class FAQVectorStore:
    """FAQ 向量库：ChromaDB 持久化 + bge-m3 嵌入 + 相似度检索。"""

    def __init__(self, persist_dir: str = "faq_chromadb", collection_name: str = "faq",
                 embedding_function: Optional[BgeM3EmbeddingFunction] = None):
        if embedding_function is None:
            raise ValueError("必须提供 embedding_function（BgeM3EmbeddingFunction 实例）")
        self.persist_dir = persist_dir
        self.collection_name = collection_name
        self.embed_fn = embedding_function
        # 1) 初始化 ChromaDB 持久化客户端：数据落盘到 persist_dir，重启不丢
        self.client = chromadb.PersistentClient(
            path=persist_dir, settings=Settings(anonymized_telemetry=False))
        self.collection = self._get_or_create()

    def _get_or_create(self):
        meta = {"hnsw:space": "cosine", "embed": self.embed_fn.backend, "model": self.embed_fn.model}
        try:
            col = self.client.get_collection(self.collection_name)
            # 后端或模型变了（如换 qwen→bge-m3）→ 旧索引作废（维度可能不一致），删除重建
            col_meta = col.metadata or {}
            if col_meta.get("embed") != meta["embed"] or col_meta.get("model") != meta["model"]:
                self.client.delete_collection(self.collection_name)
                return self.client.create_collection(self.collection_name,
                                                     embedding_function=self.embed_fn,
                                                     metadata=meta)
            return col
        except Exception:
            # 2) 创建 collection：cosine 距离 + 挂自定义 embedding 函数
            return self.client.create_collection(self.collection_name,
                                                 embedding_function=self.embed_fn,
                                                 metadata=meta)

    def count(self) -> int:
        return self.collection.count()

    def rebuild(self, faq_list: list[dict]) -> int:
        """清空并重建索引。

        faq_list: [{category, question, answer}]（来自 cs_supervisor._load_faq）
        3) 插入文档：文本 = Q+答案（供相似度检索），元数据 category（供过滤）。
        """
        try:
            self.client.delete_collection(self.collection_name)
        except Exception:
            pass
        self.collection = self.client.create_collection(
            self.collection_name, embedding_function=self.embed_fn,
            metadata={"hnsw:space": "cosine", "embed": self.embed_fn.backend, "model": self.embed_fn.model})
        if not faq_list:
            return 0
        ids, docs, metas = [], [], []
        for i, f in enumerate(faq_list):
            ids.append(f"faq_{i}")
            docs.append(f"Q：{f['question']}\nA：{f['answer']}")
            metas.append({"category": f["category"]})
        self.collection.add(ids=ids, documents=docs, metadatas=metas)
        return len(ids)

    def search(self, query: str, k: int = 3, category: Optional[str] = None) -> list[dict]:
        """4) 相似度查询：cosine 距离，可选按 category 过滤，返回 top-k 标准问答。"""
        k = min(k, self.count() or 1)
        try:
            # 带元数据过滤查询（注意：过滤后不足 k 条时 chroma 会报错，下面兜底）
            where = {"category": category} if category else None
            res = self.collection.query(query_texts=[query], n_results=k, where=where,
                                        include=["documents", "metadatas", "distances"])
        except Exception:
            # 兜底：先按 k*3 全量取，再在 Python 侧按 category 过滤
            res = self.collection.query(query_texts=[query], n_results=min(k * 3, self.count() or 1),
                                        include=["documents", "metadatas", "distances"])
            docs_all = res.get("documents", [[]])[0]
            metas_all = res.get("metadatas", [[]])[0]
            dists_all = res.get("distances", [[]])[0]
            kept = [(d, m, dist) for d, m, dist in zip(docs_all, metas_all, dists_all)
                    if not category or m.get("category") == category][:k]
            docs, metas, dists = ([x[0] for x in kept], [x[1] for x in kept], [x[2] for x in kept])
        else:
            docs = res.get("documents", [[]])[0]
            metas = res.get("metadatas", [[]])[0]
            dists = res.get("distances", [[]])[0]

        out = []
        for doc, meta, dist in zip(docs, metas, dists):
            q, a = self._split_qa(doc)
            out.append({
                "category": (meta or {}).get("category", ""),
                "question": q,
                "answer": a,
                "score": round(1.0 - dist, 4),  # cosine distance → 相似度
            })
        return out

    @staticmethod
    def _split_qa(doc: str) -> tuple[str, str]:
        if "\nA：" in doc:
            q, a = doc.split("\nA：", 1)
            return (q[2:] if q.startswith("Q：") else q), a
        return doc, ""
