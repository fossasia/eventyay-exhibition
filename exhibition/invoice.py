"""PDF invoices for paid exhibition orders."""

from io import BytesIO
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from django.contrib.staticfiles import finders
from django.http import HttpResponse
from django.utils.formats import date_format
from django.utils.timezone import localtime
from django.utils.translation import gettext as _
from eventyay.base.templatetags.money import money_filter
from i18nfield.strings import LazyI18nString
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle

from .models import ExhibitionOrder

FONT_FILES = {
    "OpenSans": "fonts/OpenSans-Regular.ttf",
    "OpenSansIt": "fonts/OpenSans-Italic.ttf",
    "OpenSansBd": "fonts/OpenSans-Bold.ttf",
    "OpenSansBI": "fonts/OpenSans-BoldItalic.ttf",
}
PAGE_WIDTH = A4[0] - 36 * mm
TEXT_COLOR = colors.HexColor("#374151")
LINE_COLOR = colors.HexColor("#d1d5db")
HEADER_BACKGROUND = colors.HexColor("#f3f4f6")


def register_fonts():
    """Register the Open Sans family the ticket invoices use, so names in any Latin or Cyrillic script render."""
    if "OpenSans" in pdfmetrics.getRegisteredFontNames():
        return
    for name, path in FONT_FILES.items():
        pdfmetrics.registerFont(TTFont(name, finders.find(path)))
    pdfmetrics.registerFontFamily(
        "OpenSans", normal="OpenSans", bold="OpenSansBd", italic="OpenSansIt", boldItalic="OpenSansBI"
    )


def _styles():
    body = ParagraphStyle("body", fontName="OpenSans", fontSize=9, leading=13, textColor=TEXT_COLOR)
    return {
        "body": body,
        "right": ParagraphStyle("right", parent=body, alignment=2),
        "title": ParagraphStyle("title", parent=body, fontName="OpenSansBd", fontSize=18, leading=22),
        "label": ParagraphStyle("label", parent=body, fontName="OpenSansBd"),
        "label_right": ParagraphStyle("label_right", parent=body, fontName="OpenSansBd", alignment=2),
    }


def _lines(*values):
    """Non-empty values as one escaped block, one per line."""
    lines = (line.strip() for value in values if value for line in str(value).splitlines())
    return "<br/>".join(escape(line) for line in lines if line)


def _issuer(order):
    settings = order.event.settings
    address = settings.get("invoice_address_from", as_type=LazyI18nString)
    lines = [order.event.organizer.name, order.event.name] if not address else [address]
    if settings.get("invoice_address_from_vat_id"):
        lines.append(f"{_('VAT ID')}: {settings.get('invoice_address_from_vat_id')}")
    return _lines(*lines)


def _bill_to(order):
    lines = [order.organization_name, order.name, order.email, order.phone, order.billing_address]
    if order.vat_id:
        lines.append(f"{_('VAT ID')}: {order.vat_id}")
    return _lines(*lines)


def _borderless(rows, widths):
    table = Table(rows, colWidths=widths)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _items(order, styles):
    rows = [
        [
            Paragraph(escape(_("Product")), styles["label"]),
            Paragraph(escape(_("Exhibition booth")), styles["label"]),
            Paragraph(escape(_("Price")), styles["label_right"]),
        ]
    ]
    for position in order.positions.all():
        rows.append(
            [
                Paragraph(escape(position.localized_name), styles["body"]),
                Paragraph(escape(_("Yes") if position.includes_booth else _("No")), styles["body"]),
                Paragraph(escape(money_filter(position.price, order.currency)), styles["right"]),
            ]
        )
    rows.append(
        [
            Paragraph(escape(_("Total")), styles["label"]),
            "",
            Paragraph(escape(money_filter(order.total, order.currency)), styles["label_right"]),
        ]
    )
    table = Table(rows, colWidths=[PAGE_WIDTH - 70 * mm, 35 * mm, 35 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HEADER_BACKGROUND),
                ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE_COLOR),
                ("LINEABOVE", (0, -1), (-1, -1), 1, TEXT_COLOR),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table


def _event_date(event, value):
    """A stored UTC time as a date in the event's timezone, the day the order page shows."""
    return date_format(localtime(value, ZoneInfo(event.settings.timezone)), "SHORT_DATE_FORMAT")


def render_order_invoice_pdf(order: ExhibitionOrder) -> bytes:
    """The invoice of a paid exhibition order, as PDF bytes. The order code is the invoice reference."""
    register_fonts()
    styles = _styles()
    event = order.event
    payment = [f"<b>{escape(_('Order code'))}:</b> {escape(order.code)}"]
    payment.append(f"<b>{escape(_('Order date'))}:</b> {escape(_event_date(event, order.created))}")
    if order.payment_date:
        payment.append(f"<b>{escape(_('Payment date'))}:</b> {escape(_event_date(event, order.payment_date))}")
    provider = event.get_payment_providers().get(order.payment_provider)
    if provider:
        payment.append(f"<b>{escape(_('Payment method'))}:</b> {escape(str(provider.public_name))}")
    if order.payment_reference:
        payment.append(f"<b>{escape(_('Payment reference'))}:</b> {escape(order.payment_reference)}")

    story = [
        _borderless(
            [
                [
                    Paragraph(_issuer(order), styles["body"]),
                    [
                        Paragraph(escape(_("Invoice")), styles["title"]),
                        Paragraph("<br/>".join(payment), styles["body"]),
                    ],
                ]
            ],
            [PAGE_WIDTH * 0.5, PAGE_WIDTH * 0.5],
        ),
        Spacer(1, 10 * mm),
        Paragraph(f"<b>{escape(_('Bill to'))}</b>", styles["body"]),
        Paragraph(_bill_to(order), styles["body"]),
        Spacer(1, 8 * mm),
        _items(order, styles),
    ]
    footer = event.settings.get("invoice_footer_text", as_type=LazyI18nString)
    if footer:
        story += [Spacer(1, 10 * mm), Paragraph(_lines(footer), styles["body"])]

    output = BytesIO()
    doc = BaseDocTemplate(
        output,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"{_('Invoice')} {order.code}",
        author=event.organizer.name,
    )
    # No frame padding, so text and tables share the same left edge.
    frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    doc.addPageTemplates([PageTemplate(id="invoice", frames=[frame])])
    doc.build(story)
    return output.getvalue()


def invoice_response(order: ExhibitionOrder) -> HttpResponse:
    """The invoice as a PDF download."""
    response = HttpResponse(render_order_invoice_pdf(order), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="invoice-{order.code}.pdf"'
    return response
