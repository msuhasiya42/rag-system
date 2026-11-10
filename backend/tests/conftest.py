from pathlib import Path

import pytest
from fpdf import FPDF
from PIL import Image, ImageDraw


@pytest.fixture
def mixed_pdf(tmp_path: Path) -> Path:
    """Page 1: bookmarked bold heading and body text. Page 2: an image only, like a scan.
    Page 3: blank."""
    image_path = tmp_path / "scan.png"
    image = Image.new("RGB", (800, 300), "white")
    ImageDraw.Draw(image).text((40, 120), "Scanned invoice total 4,200 USD", fill="black")
    image.save(image_path)

    pdf = FPDF()
    pdf.add_page()
    pdf.start_section("Revenue Overview")
    pdf.set_font("helvetica", style="B", size=12)
    pdf.cell(0, 10, "Revenue Overview", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=12)
    pdf.multi_cell(0, 8, "Annual revenue grew to 12 million dollars in 2025. " * 5)
    pdf.add_page()
    pdf.image(str(image_path), x=10, y=10, w=180)
    pdf.add_page()

    path = tmp_path / "Mixed Report.pdf"
    pdf.output(str(path))
    return path
