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
import threading

from app.ai.providers import EmbedProvider, ProviderUsage, apply_hf_env
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
        self.runtime_device = f"local:{self.device}"
        self.hf_endpoint = settings.hf_endpoint
        self.hf_home = settings.hf_home
        self._model = None

    # ---- 成本 ----

    def estimate_cost(self, call: str, units: dict) -> float | None:
        """0 = **外部 API 费用为零**,不代表计算成本为零(D-52)。

        算力与电耗是真实的,故每次调用同时记录 duration_ms 与 device,
        让"零账单"不被误读成"零成本"。
        """
        return 0.0

    def usage_for(self, call: str, units: dict, result) -> ProviderUsage | None:
        """用量留 NULL:tokens 列只有"模型 token"一种语义,而本地 bge-m3 这次调用
        没有任何按 token 计费的口径 —— 把 texts 条数塞进 tokens_in 会让台账里
        "3" 和 DeepSeek 的"335 token"看起来同单位(D-58)。批大小不在此留痕,
        但可由 embedding_chunk 行数还原;算力开销由 duration_ms + device 体现(D-52)。"""
        return ProviderUsage(basis="estimated", cost_cents=0.0,
                             tokens_in=None, tokens_out=None)

    # ---- 推理 ----

    def _load(self):
        if self._model is not None:
            return self._model
        with _LOCK:
            if self.model_id not in _CACHE:
                # 直连 huggingface.co 在本机超时,默认走镜像;权重目录同样由 .env 指定
                apply_hf_env(self.hf_endpoint, self.hf_home)
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
