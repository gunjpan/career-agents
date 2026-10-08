from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from jobagent.documents.model import CoverLetterDoc, ResumeDoc

_base = getSampleStyleSheet()["Normal"]
BODY = ParagraphStyle(
    "body", parent=_base, fontName="Helvetica", fontSize=9.5, leading=12.5, spaceAfter=2
)
CENTER = ParagraphStyle("center", parent=BODY, alignment=TA_CENTER)
NAME = ParagraphStyle("name", parent=CENTER, fontName="Helvetica-Bold", fontSize=17, leading=21)
HEAD = ParagraphStyle(
    "head", parent=BODY, fontName="Helvetica-Bold", fontSize=10.5, spaceBefore=7, spaceAfter=1
)
ENTRY = ParagraphStyle("entry", parent=BODY, spaceBefore=4, spaceAfter=1, keepWithNext=1)


def _p(text: str, style=BODY) -> Paragraph:
    return Paragraph(escape(text), style)


def _pdf(story: list, title: str) -> bytes:
    buf = BytesIO()
    SimpleDocTemplate(
        buf, pagesize=letter, title=title, leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.65 * inch, bottomMargin=0.65 * inch,
    ).build(story)  # fmt: skip
    return buf.getvalue()


def _heading(text: str) -> list:
    return [
        _p(text.upper(), HEAD),
        HRFlowable(width="100%", thickness=0.6, color="#808080", spaceAfter=3),
    ]


def resume_pdf(d: ResumeDoc) -> bytes:
    story: list = [_p(d.name.upper(), NAME), _p(d.contact, CENTER)]
    if d.headline:
        story.append(Paragraph(f"<b>{escape(d.headline)}</b>", CENTER))
    story += _heading("Summary") + [_p(d.summary)]
    story += _heading("Core strengths") + [_p(" · ".join(d.core_strengths))]
    story += _heading("Professional experience")
    for e in d.experience:
        head = f"<b>{escape(e.company)}</b>  |  " if e.company else ""
        dates = f"  |  {escape(e.dates)}" if e.dates else ""
        story.append(Paragraph(f"{head}<b><i>{escape(e.title)}</i></b>{dates}", ENTRY))
        story.append(
            ListFlowable(
                [ListItem(_p(b), leftIndent=14) for b in e.bullets],
                bulletType="bullet",
                start="•",
                leftIndent=14,
                bulletFontSize=8,
            )
        )
    if d.education:
        story += _heading("Education") + [_p(line) for line in d.education]
    return _pdf(story, f"{d.name} - Resume")


def cover_letter_pdf(d: CoverLetterDoc) -> bytes:
    story: list = [_p(d.name.upper(), NAME), _p(d.contact, CENTER), Spacer(1, 14), _p(d.date)]
    story += [Paragraph(f"<b>Re:</b> {escape(d.regarding)}", BODY), Spacer(1, 10), _p(d.greeting)]
    story += [
        Paragraph(escape(t), ParagraphStyle("para", parent=BODY, spaceAfter=8))
        for t in d.paragraphs
    ]
    story += [_p(d.closing), _p(d.name)]
    return _pdf(story, f"{d.name} - Cover letter")
