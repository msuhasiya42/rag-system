"""Build the real services from settings. The only module that knows every concrete client."""

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from pinecone import Pinecone

if TYPE_CHECKING:  # stubs are a dev dependency
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

from rag_system.answering import AnswerService
from rag_system.clients.bedrock import BedrockChatModel, TitanEmbedder
from rag_system.clients.pinecone_inference import (
    PineconeEmbedder,
    PineconeReranker,
    PineconeSparseEncoder,
)
from rag_system.clients.pinecone_store import PineconeDenseStore, PineconeSparseStore
from rag_system.clients.textract import TextractOcr
from rag_system.config import Settings
from rag_system.ingestion.pipeline import IngestionService, SparseIndexing
from rag_system.ports import Embedder
from rag_system.retrieval import (
    DenseRetriever,
    HybridRetriever,
    Pipeline,
    RerankingRetriever,
    Retriever,
    SparseRetriever,
)

PINECONE_TIMEOUT_SECONDS = 60.0

# Bounded retries for transient AWS errors; every call here is safe to repeat.
_AWS_CONFIG = Config(
    connect_timeout=5,
    read_timeout=90,
    retries={"max_attempts": 4, "mode": "adaptive"},
    max_pool_connections=32,
)


class ConfigurationError(Exception):
    """A required setting is missing."""


class Pinecones:
    """The dense and sparse stores over one Pinecone client."""

    def __init__(self, settings: Settings) -> None:
        self.client = _pinecone_client(settings)
        self.dense = PineconeDenseStore(
            self.client,
            settings.pinecone_index,
            settings.pinecone_namespace,
            settings.embedding_dimensions,
        )
        self.sparse = PineconeSparseStore(
            self.client, settings.pinecone_sparse_index, settings.pinecone_namespace
        )

    def ensure_indexes(self, settings: Settings) -> list[str]:
        """Create missing indexes; returns the names of those created."""
        created = []
        for name, store in (
            (settings.pinecone_index, self.dense),
            (settings.pinecone_sparse_index, self.sparse),
        ):
            if store.ensure_index(settings.pinecone_cloud, settings.pinecone_region):
                created.append(name)
        return created


def build_retriever(settings: Settings, pipeline: Pipeline | None = None) -> Retriever:
    pipeline = pipeline or settings.retrieval_pipeline
    pinecones = Pinecones(settings)
    dense: Retriever = DenseRetriever(_embedder(settings, pinecones.client), pinecones.dense)
    if pipeline == "basic":
        return dense
    candidates = settings.retrieval_candidates
    reranker = PineconeReranker(pinecones.client, settings.rerank_model)

    def reranked(inner: Retriever) -> Retriever:
        return RerankingRetriever(
            inner, reranker, candidates, settings.rerank_blend_weight, settings.rrf_k
        )

    if pipeline == "basic-rerank":
        return reranked(dense)
    sparse = SparseRetriever(
        PineconeSparseEncoder(pinecones.client, settings.pinecone_sparse_model), pinecones.sparse
    )
    hybrid = HybridRetriever(
        [dense, sparse], candidates, [1.0, settings.hybrid_sparse_weight], settings.rrf_k
    )
    if pipeline == "hybrid":
        return hybrid
    return reranked(hybrid)


def build_answer_service(settings: Settings, pipeline: Pipeline | None = None) -> AnswerService:
    bedrock = _aws_session(settings).client("bedrock-runtime", config=_AWS_CONFIG)
    return AnswerService(
        retriever=build_retriever(settings, pipeline),
        model=BedrockChatModel(bedrock, settings.answer_model_ids, settings.answer_max_tokens),
    )


def build_ingestion_service(settings: Settings, pinecones: Pinecones) -> IngestionService:
    ocr = None
    if settings.ocr_enabled:
        ocr = TextractOcr(_aws_session(settings).client("textract", config=_AWS_CONFIG))
    sparse_encoder = PineconeSparseEncoder(pinecones.client, settings.pinecone_sparse_model)
    return IngestionService(
        embedder=_embedder(settings, pinecones.client),
        store=pinecones.dense,
        sparse=SparseIndexing(sparse_encoder, pinecones.sparse),
        ocr=ocr,
        ocr_min_text_chars=settings.ocr_min_text_chars,
        chunk_size_chars=settings.chunk_size_chars,
        chunk_overlap_chars=settings.chunk_overlap_chars,
    )


def _embedder(settings: Settings, pinecone: Pinecone) -> Embedder:
    if settings.embedding_provider == "pinecone":
        return PineconeEmbedder(
            pinecone, settings.pinecone_embedding_model, settings.embedding_dimensions
        )
    bedrock: BedrockRuntimeClient = _aws_session(settings).client(
        "bedrock-runtime", config=_AWS_CONFIG
    )
    return TitanEmbedder(
        bedrock,
        settings.embedding_model_id,
        settings.embedding_dimensions,
        settings.embedding_concurrency,
    )


def _pinecone_client(settings: Settings) -> Pinecone:
    if settings.pinecone_api_key is None:
        raise ConfigurationError("PINECONE_API_KEY is not set. Add it to backend/.env.")
    return Pinecone(
        api_key=settings.pinecone_api_key.get_secret_value(), timeout=PINECONE_TIMEOUT_SECONDS
    )


def _aws_session(settings: Settings) -> boto3.Session:
    return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
