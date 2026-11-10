"""Request and response bodies for the HTTP API (camelCase on the wire)."""

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from rag_system.answering import format_pages
from rag_system.domain import Answer


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class AskRequest(ApiModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=20)


class SourceOut(ApiModel):
    number: int  # matches the [n] citations in the answer text
    document_name: str
    section: str
    page_start: int
    page_end: int
    pages_label: str
    score: float
    text: str


class AskResponse(ApiModel):
    answer: str
    model_id: str | None
    sources: list[SourceOut]

    @classmethod
    def from_answer(cls, answer: Answer) -> "AskResponse":
        return cls(
            answer=answer.text,
            model_id=answer.model_id,
            sources=[
                SourceOut(
                    number=number,
                    document_name=hit.chunk.document_name,
                    section=hit.chunk.section,
                    page_start=hit.chunk.page_start,
                    page_end=hit.chunk.page_end,
                    pages_label=format_pages(hit),
                    score=round(hit.score, 4),
                    text=hit.chunk.text,
                )
                for number, hit in enumerate(answer.sources, start=1)
            ],
        )


class ErrorBody(ApiModel):
    code: str
    message: str
    details: list[dict[str, str]] | None = None


class ErrorResponse(ApiModel):
    error: ErrorBody
