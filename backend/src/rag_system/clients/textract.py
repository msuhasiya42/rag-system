"""AWS Textract OCR for scanned PDF pages.

The synchronous API takes one image at a time and needs no S3 bucket, which fits the
page-by-page fallback in the PDF reader. DetectDocumentText returns text lines only and is the
cheapest Textract API; AnalyzeDocument would add table structure at about ten times the price.
"""

from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:  # stubs are a dev dependency
    from mypy_boto3_textract import TextractClient

from rag_system.ports import OcrEngine, UpstreamServiceError


class TextractOcr(OcrEngine):
    def __init__(self, client: "TextractClient") -> None:
        self._client = client

    def read_image(self, png_bytes: bytes) -> str:
        try:
            response = self._client.detect_document_text(Document={"Bytes": png_bytes})
        except (ClientError, BotoCoreError) as error:
            raise UpstreamServiceError(f"Textract OCR failed: {error}") from error
        lines = [
            block.get("Text", "")
            for block in response.get("Blocks", [])
            if block.get("BlockType") == "LINE"
        ]
        return "\n".join(lines)
