"""本地 Embedding:BAAI/bge-m3(sentence-transformers)。

为什么走本地:云端服务商按余额计费(实测 402),而检索向量化是每次问答与
每次索引都要调的环节;本地部署零按次费用,且 bge-m3 输出 **1024 维**,
与现有向量列 `Vector(EMBEDDING_DIM=1024)` 完全一致,不需要迁移重建(D-03)。

口径:
- is_mock = False(真实语义向量,不再是 bigram 字面重叠);
- 成本:本地推理服务商不按次收费 → estimate_cost/usage 返回 0 且口径 estimated。
  这只表示"没有按次账单",不等于没有算力与电耗。
- 模型**懒加载**:进程首次调用才加载,避免导入即占用数 GB 内存;
  同进程按模型 id 复用一份实例(API 与 worker 是不同进程,各自加载一次)。
"""
import os
import threading

from app.ai.providers import EmbedProvider, ProviderUsage
from app.core.config import Settings
from app.models.transcript import EMBEDDING_DIM

_CACHE: dict[str, object] = {}
_LOCK = threading.Lock()


class EmbedDimensionMismatch(RuntimeError):
    """模型实际输出维度与向量列不符(必须换模型或迁移重建)。"""


class LocalBgeM3Embed(EmbedProvider):
    name = "local-bge-m3"
    is_mock = False
    embedding_dim = EMBEDDING_DIM  # 1024,工厂会与实际输出再核对一次

    def __init__(self, settings: Settings):
        self.model_id = settings.embed_model
        self.model_name = f"{settings.embed_model}(local)"
        self.device = settings.embed_device or "cpu"
        self.batch_size = settings.embed_batch_size
        self._model = None

    # ---- 成本 ----

    def estimate_cost(self, call: str, units: dict) -> float | None:
        return 0.0  # 本地推理无按次账单(不是"未知")

    def usage_for(self, call: str, units: dict, result) -> ProviderUsage | None:
        texts = units.get("texts") or 0
        return ProviderUsage(basis="estimated", cost_cents=0.0,
                             tokens_in=texts, tokens_out=None)

    # ---- 推理 ----

    def _load(self):
        if self._model is not None:
            return self._model
        with _LOCK:
            if self.model_id not in _CACHE:
                # 直连 huggingface.co 在本机超时,默认走镜像(可被环境变量覆盖)
                os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                from sentence_transformers import SentenceTransformer

                _CACHE[self.model_id] = SentenceTransformer(
                    self.model_id, device=self.device
                )
            self._model = _CACHE[self.model_id]
        return self._model

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(
            [t if t.strip() else " " for t in texts],  # 空串会让部分模型报错,用空格占位
            normalize_embeddings=True,                  # 检索用余弦距离
            batch_size=self.batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        out = [[float(x) for x in v] for v in vectors]
        for v in out:
            if len(v) != EMBEDDING_DIM:
                raise EmbedDimensionMismatch(
                    f"{self.model_id} 实际输出 {len(v)} 维,"
                    f"向量列为 {EMBEDDING_DIM} 维:需换模型或新增迁移重建索引(D-03)"
                )
        return out
