from functools import cache
from io import BytesIO
from xml.sax.saxutils import escape

from django.utils.formats import date_format
from django.utils.translation import gettext as _
from eventyay.base.templatetags.money import money_filter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import ExhibitionOrder, ExhibitionOrderStatus
from .orders import payment_instructions


@cache
def _register_invoice_fonts() -> tuple[str, str]:
    """Register and return Unicode-capable TrueType fonts for invoice rendering."""
    if "OpenSans" in pdfmetrics.getRegisteredFontNames() and "OpenSansBd" in pdfmetrics.getRegisteredFontNames():
        return "OpenSans", "OpenSansBd"

    try:
        from django.contrib.staticfiles import finders

        regular_path = finders.find("fonts/OpenSans-Regular.ttf")
        bold_path = finders.find("fonts/OpenSans-Bold.ttf")
        italic_path = finders.find("fonts/OpenSans-Italic.ttf")
        bold_italic_path = finders.find("fonts/OpenSans-BoldItalic.ttf")

        if regular_path and bold_path:
            pdfmetrics.registerFont(TTFont("OpenSans", regular_path))
            pdfmetrics.registerFont(TTFont("OpenSansBd", bold_path))
            if italic_path:
                pdfmetrics.registerFont(TTFont("OpenSansIt", italic_path))
            if bold_italic_path:
                pdfmetrics.registerFont(TTFont("OpenSansBI", bold_italic_path))
            pdfmetrics.registerFontFamily(
                "OpenSans",
                normal="OpenSans",
                bold="OpenSansBd",
                italic="OpenSansIt" if italic_path else "OpenSans",
                boldItalic="OpenSansBI" if bold_italic_path else "OpenSansBd",
            )
            return "OpenSans", "OpenSansBd"
    except Exception:
        pass

    return "Helvetica", "Helvetica-Bold"


def _format_address_block(order: ExhibitionOrder) -> str:
    parts = []
    if order.organization_name:
        parts.append(escape(order.organization_name))
    if order.name:
        parts.append(escape(order.name))
    if order.email:
        parts.append(escape(order.email))
    if order.phone:
        parts.append(escape(order.phone))
    if order.billing_address:
        lines = [line.strip() for line in order.billing_address.splitlines() if line.strip()]
        parts.extend(escape(line) for line in lines[:10])
    if order.vat_id:
        parts.append(f"{_('VAT ID')}: {escape(order.vat_id)}")
    return "<br/>".join(parts)


def _format_issuer_block(order: ExhibitionOrder) -> str:
    settings = order.event.settings
    from_text = settings.get("invoice_address_from")
    if from_text:
        return "<br/>".join(escape(line.strip()) for line in str(from_text).splitlines() if line.strip())

    parts = [escape(order.event.organizer.name), escape(str(order.event.name))]
    from_name = settings.get("invoice_address_from_name")
    if from_name:
        parts.append(escape(str(from_name)))
    from_city = settings.get("invoice_address_from_city")
    from_zip = settings.get("invoice_address_from_zipcode")
    city_zip = " ".join(filter(None, [from_zip, from_city]))
    if city_zip:
        parts.append(escape(city_zip))
    from_vat = settings.get("invoice_address_from_vat_id")
    if from_vat:
        parts.append(f"{_('VAT ID')}: {escape(str(from_vat))}")
    return "<br/>".join(parts)


def render_order_invoice_pdf(order: ExhibitionOrder) -> bytes:
    """Generate a PDF invoice for an exhibition order using reportlab."""
    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Invoice {order.code}",
        author="eventyay",
    )

    font_regular, font_bold = _register_invoice_fonts()
    base_styles = getSampleStyleSheet()
    heading_style = ParagraphStyle(
        "InvoiceHeading",
        parent=base_styles["Heading2"],
        fontName=font_bold,
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#374151"),
        spaceBefore=0,
        spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "InvoiceBody",
        parent=base_styles["BodyText"],
        fontName=font_regular,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#4b5563"),
    )
    bold_body_style = ParagraphStyle(
        "InvoiceBodyBold",
        parent=body_style,
        fontName=font_bold,
        textColor=colors.HexColor("#111827"),
    )

    story = []

    # Header: Issuer on left, Document title / Order code on right
    issuer_html = _format_issuer_block(order)
    provider = order.event.get_payment_providers().get(order.payment_provider)
    payment_method_name = str(provider.public_name) if provider else order.payment_provider

    header_table_data = [
        [
            Paragraph(f"<b>{escape(str(order.event.name))}</b><br/>{issuer_html}", body_style),
            Paragraph(
                f"<font size='18'><b>{_('INVOICE')}</b></font><br/>"
                f"<b>{_('Order code')}:</b> {escape(order.code)}<br/>"
                f"<b>{_('Date')}:</b> {date_format(order.created, 'SHORT_DATE_FORMAT')}<br/>"
                f"<b>{_('Status')}:</b> {escape(str(order.get_status_display()))}",
                body_style,
            ),
        ]
    ]
    header_table = Table(header_table_data, colWidths=[100 * mm, 74 * mm])
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 10 * mm))

    # Bill To block
    bill_to_html = _format_address_block(order)
    meta_lines = []
    if payment_method_name:
        meta_lines.append(f"<b>{_('Payment method')}:</b> {escape(payment_method_name)}")
    if order.payment_reference:
        meta_lines.append(f"<b>{_('Payment reference')}:</b> {escape(order.payment_reference)}")
    if order.payment_date:
        meta_lines.append(f"<b>{_('Payment date')}:</b> {date_format(order.payment_date, 'SHORT_DATE_FORMAT')}")
    if order.expires and order.status == ExhibitionOrderStatus.PENDING:
        meta_lines.append(f"<b>{_('Payment due')}:</b> {date_format(order.expires, 'SHORT_DATE_FORMAT')}")

    bill_to_table_data = [
        [
            Paragraph(f"<b>{_('Bill to')}</b><br/>{bill_to_html}", body_style),
            Paragraph("<br/>".join(meta_lines), body_style) if meta_lines else Paragraph("", body_style),
        ]
    ]
    bill_to_table = Table(bill_to_table_data, colWidths=[100 * mm, 74 * mm])
    bill_to_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(bill_to_table)
    story.append(Spacer(1, 8 * mm))

    # Items table
    items_data = [
        [
            Paragraph(f"<b>{_('Product / Description')}</b>", bold_body_style),
            Paragraph(f"<b>{_('Exhibition booth')}</b>", bold_body_style),
            Paragraph(f"<b>{_('Price')}</b>", bold_body_style),
        ]
    ]

    for position in order.positions.all():
        name_str = escape(position.localized_name)
        booth_str = _("Yes") if position.includes_booth else _("No")
        price_str = escape(money_filter(position.price, order.currency))
        items_data.append(
            [
                Paragraph(name_str, body_style),
                Paragraph(booth_str, body_style),
                Paragraph(price_str, body_style),
            ]
        )

    # Total row
    items_data.append(
        [
            Paragraph(f"<b>{_('Total')}</b>", bold_body_style),
            "",
            Paragraph(f"<b>{escape(money_filter(order.total, order.currency))}</b>", bold_body_style),
        ]
    )

    items_table = Table(items_data, colWidths=[105 * mm, 35 * mm, 34 * mm])
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 1), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f9fafb")),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 8 * mm))

    # Instructions or Notes
    if order.status == ExhibitionOrderStatus.PENDING:
        instructions = payment_instructions(order)
        if instructions:
            story.append(Paragraph(f"<b>{_('Payment instructions')}</b>", heading_style))
            story.append(Paragraph(escape(instructions).replace("\n", "<br/>"), body_style))
            story.append(Spacer(1, 4 * mm))

    footer_text = order.event.settings.get("invoice_footer_text")
    if footer_text:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(escape(str(footer_text)).replace("\n", "<br/>"), body_style))

    doc.build(story)
    return output.getvalue()
