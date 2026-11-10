"""Flask application: thin routes over the answer service."""

import logging

from flask import Flask, Response, jsonify, request
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

from rag_system.answering import AnswerService
from rag_system.api.schemas import AskRequest, AskResponse, ErrorBody, ErrorResponse
from rag_system.ports import UpstreamServiceError

logger = logging.getLogger(__name__)


def create_app(answer_service: AnswerService, default_top_k: int) -> Flask:
    app = Flask(__name__)

    @app.get("/api/health")
    def health() -> Response:
        return jsonify({"status": "ok"})

    @app.post("/api/ask")
    def ask() -> Response:
        body = AskRequest.model_validate(request.get_json(silent=True) or {})
        answer = answer_service.answer(body.question.strip(), body.top_k or default_top_k)
        return jsonify(AskResponse.from_answer(answer).model_dump(by_alias=True))

    @app.errorhandler(ValidationError)
    def invalid_request(error: ValidationError) -> tuple[Response, int]:
        details = [
            {"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
            for item in error.errors()
        ]
        return _error("invalid_request", "The request body is invalid.", 400, details)

    @app.errorhandler(UpstreamServiceError)
    def upstream_failed(error: UpstreamServiceError) -> tuple[Response, int]:
        logger.error("Upstream service failed: %s", error)
        return _error(
            "upstream_unavailable",
            "A model or search service is unavailable. Try again shortly.",
            502,
        )

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> tuple[Response, int]:
        code = error.code or 500
        return _error(error.name.lower().replace(" ", "_"), error.description or "", code)

    @app.errorhandler(Exception)
    def unexpected(error: Exception) -> tuple[Response, int]:
        logger.exception("Unhandled error")
        return _error("internal_error", "Something went wrong.", 500)

    return app


def _error(
    code: str, message: str, status: int, details: list[dict[str, str]] | None = None
) -> tuple[Response, int]:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return jsonify(body.model_dump(by_alias=True, exclude_none=True)), status
