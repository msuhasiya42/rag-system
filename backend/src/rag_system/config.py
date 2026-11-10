"""Application settings, read from environment variables and backend/.env."""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_", env_file=".env", extra="ignore")

    # AWS: credentials come from the named CLI profile, never from this file.
    aws_profile: str | None = None
    aws_region: str = "ap-southeast-2"

    # Vectors from different models are not comparable: changing the provider, model or
    # dimensions needs a re-ingest of every document (into a new index if dimensions change).
    embedding_provider: Literal["pinecone", "bedrock"] = "pinecone"
    embedding_dimensions: int = Field(default=1024, description="Must match the Pinecone index")
    pinecone_embedding_model: str = "llama-text-embed-v2"
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"  # Bedrock provider
    embedding_concurrency: int = Field(default=8, ge=1, le=32)  # Bedrock: one text per call

    # Tried in order; the next one is used when a model is throttled or unavailable.
    answer_model_ids: list[str] = [
        "qwen.qwen3-235b-a22b-2507-v1:0",
        "global.amazon.nova-2-lite-v1:0",
    ]
    answer_max_tokens: int = 1200

    ocr_enabled: bool = True
    # A page with fewer extracted characters than this, and at least one image, is OCR'd.
    ocr_min_text_chars: int = 50

    pinecone_api_key: SecretStr | None = Field(default=None, validation_alias="PINECONE_API_KEY")
    pinecone_index: str = "rag-system"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"  # the free Starter plan only offers this region
    pinecone_namespace: str = "documents"

    chunk_size_chars: int = Field(default=1800, ge=200)
    chunk_overlap_chars: int = Field(default=300, ge=0)
    default_top_k: int = Field(default=6, ge=1, le=20)
