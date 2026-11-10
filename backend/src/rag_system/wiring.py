"""Build the real services from settings. The only module that knows every concrete client."""

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from pinecone import Pinecone

if TYPE_CHECKING:  # stubs are a dev dependency
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

from rag_system.answering import AnswerService
from rag_system.clients.bedrock import BedrockChatModel, TitanEmbedder
from rag_system.clients.pinecone_embedder import PineconeEmbedder
from rag_system.clients.pinecone_store import PineconeVectorStore
from rag_system.clients.textract import TextractOcr
from rag_system.config import Settings
from rag_system.ingestion.pipeline import IngestionService
from rag_system.ports import Embedder

PINECONE_TIMEOUT_SECONDS = 60.0


class ConfigurationError(Exception):
    """A required setting is missing."""


# Bounded retries for transient AWS errors; every call here is safe to repeat.
_AWS_CONFIG = Config(
    connect_timeout=5,
    read_timeout=90,
    retries={"max_attempts": 4, "mode": "adaptive"},
    max_pool_connections=32,
)


def build_vector_store(settings: Settings) -> PineconeVectorStore:
    return PineconeVectorStore(
        api_key=_pinecone_key(settings),
        index_name=settings.pinecone_index,
        namespace=settings.pinecone_namespace,
    )


def build_answer_service(settings: Settings) -> AnswerService:
    session = _aws_session(settings)
    bedrock = session.client("bedrock-runtime", config=_AWS_CONFIG)
    return AnswerService(
        embedder=_embedder(settings, bedrock),
        store=build_vector_store(settings),
        model=BedrockChatModel(bedrock, settings.answer_model_ids, settings.answer_max_tokens),
    )


def build_ingestion_service(
    settings: Settings, store: PineconeVectorStore | None = None
) -> IngestionService:
    session = _aws_session(settings)
    bedrock = session.client("bedrock-runtime", config=_AWS_CONFIG)
    ocr = None
    if settings.ocr_enabled:
        ocr = TextractOcr(session.client("textract", config=_AWS_CONFIG))
    return IngestionService(
        embedder=_embedder(settings, bedrock),
        store=store or build_vector_store(settings),
        ocr=ocr,
        ocr_min_text_chars=settings.ocr_min_text_chars,
        chunk_size_chars=settings.chunk_size_chars,
        chunk_overlap_chars=settings.chunk_overlap_chars,
    )


def _aws_session(settings: Settings) -> boto3.Session:
    return boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)


def _embedder(settings: Settings, bedrock: "BedrockRuntimeClient") -> Embedder:
    if settings.embedding_provider == "pinecone":
        client = Pinecone(api_key=_pinecone_key(settings), timeout=PINECONE_TIMEOUT_SECONDS)
        return PineconeEmbedder(
            client, settings.pinecone_embedding_model, settings.embedding_dimensions
        )
    return TitanEmbedder(
        bedrock,
        settings.embedding_model_id,
        settings.embedding_dimensions,
        settings.embedding_concurrency,
    )


def _pinecone_key(settings: Settings) -> str:
    if settings.pinecone_api_key is None:
        raise ConfigurationError("PINECONE_API_KEY is not set. Add it to backend/.env.")
    return settings.pinecone_api_key.get_secret_value()
