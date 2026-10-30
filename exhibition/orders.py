from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from eventyay.base.templatetags.money import money_filter
from i18nfield.strings import LazyI18nString

from . import mail
from .models import (
    LOG_ORDER_PLACED,
    ExhibitionOrder,
    ExhibitionOrderError,
    ExhibitionOrderPosition,
    ExhibitionOrderStateError,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    reserve_quota_capacity,
)

SUPPORTED_PAYMENT_PROVIDERS = ("manual", "banktransfer")
FREE_PAYMENT_PROVIDER = "free"


def usable_payment_providers(event):
    """The event's enabled payment methods that exhibition orders can be paid with."""
    return {
        identifier: provider
        for identifier, provider in event.get_payment_providers().items()
        if identifier in SUPPORTED_PAYMENT_PROVIDERS and provider.is_enabled
    }


def payment_method_names(event):
    """The display name of every payment method an exhibition order can carry, keyed by identifier."""
    names = {identifier: str(provider.verbose_name) for identifier, provider in event.get_payment_providers().items()}
    names[FREE_PAYMENT_PROVIDER] = _("Free")
    return names


def paid_products_lack_payment_method(event):
    """Whether active products with a price are on offer while no usable payment method is enabled."""
    return ExhibitionProduct.objects.filter(
        event=event, active=True, price__gt=0
    ).exists() and not usable_payment_providers(event)


def payment_instructions(exhibition_order):
    """How to pay a pending order, as plain text from its payment method's settings."""
    if exhibition_order.status != ExhibitionOrderStatus.PENDING:
        return ""
    provider = exhibition_order.event.get_payment_providers().get(exhibition_order.payment_provider)
    if provider is None:
        return ""
    if exhibition_order.payment_provider == "banktransfer":
        lines = []
        if provider.settings.get("bank_details_type") == "sepa":
            lines += [
                f"{_('Account holder')}: {provider.settings.get('bank_details_sepa_name')}",
                f"{_('IBAN')}: {provider.settings.get('bank_details_sepa_iban')}",
                f"{_('BIC')}: {provider.settings.get('bank_details_sepa_bic')}",
                f"{_('Bank')}: {provider.settings.get('bank_details_sepa_bank')}",
            ]
        details = provider.settings.get("bank_details", as_type=LazyI18nString)
        if details:
            lines.append(str(details))
        lines.append(_("Please use the order code {code} as the payment reference.").format(code=exhibition_order.code))
        return "\n".join(lines)
    instructions = str(provider.settings.get("email_instructions", as_type=LazyI18nString) or "")
    return instructions.format_map(
        defaultdict(
            str,
            order=exhibition_order.code,
            amount=exhibition_order.total,
            currency=exhibition_order.currency,
            amount_with_currency=money_filter(exhibition_order.total, exhibition_order.currency),
        )
    )


def send_order_email(order, *, user=None):
    """Email the buyer after commit: the confirmation once paid, otherwise the order with how to pay it."""
    role = mail.ORDER_PAID if order.status == ExhibitionOrderStatus.PAID else mail.ORDER_PLACED
    transaction.on_commit(
        lambda: mail.queue_order_email(order.event, order, role, send_now=True, requestor=user),
        robust=True,
    )


def place_exhibition_order(
    event,
    products,
    *,
    name,
    email,
    organization_name="",
    phone="",
    website="",
    billing_address="",
    vat_id="",
    order_form=None,
    payment_provider="",
    user=None,
    check_availability=True,
    paid=False,
    payment_reference="",
):
    """Place an order at current prices; organisers skip the sale checks and may book it as paid, quotas still apply."""
    products = list(products)
    if not products:
        raise ExhibitionOrderError("An order needs at least one product.")
    if order_form is not None and not order_form.is_valid():
        raise ExhibitionOrderError("The order form has errors.")

    now = timezone.now()
    for product in products:
        if product.event_id != event.pk or (check_availability and not product.is_available(now)):
            raise ExhibitionOrderError(f"{product} is not on sale.")

    total = sum((product.price for product in products), Decimal("0.00"))
    if check_availability and total and payment_provider not in usable_payment_providers(event):
        raise ExhibitionOrderError("The payment method is not available for this event.")

    with transaction.atomic():
        reserve_quota_capacity(products)
        order = ExhibitionOrder.objects.create(
            event=event,
            organization_name=organization_name,
            name=name,
            email=email,
            phone=phone,
            website=website,
            billing_address=billing_address,
            vat_id=vat_id,
            total=total,
            payment_provider=payment_provider if total else "",
            expires=(
                now + timedelta(days=event.settings.get("payment_term_days", as_type=int))
                if total and not paid
                else None
            ),
        )
        ExhibitionOrderPosition.objects.bulk_create(
            ExhibitionOrderPosition.for_product(order, product) for product in products
        )
        if order_form is not None:
            order_form.save_exhibition_questions(order)
        order.log_action(
            LOG_ORDER_PLACED,
            data={"total": str(total), "products": [product.pk for product in products]},
            user=user,
        )
        if not total:
            order.mark_paid(provider=FREE_PAYMENT_PROVIDER, user=user)
        elif paid:
            order.mark_paid(provider=payment_provider, reference=payment_reference, user=user)
    return order


def expire_overdue_orders(now=None):
    """Expire pending orders whose payment term has passed and return how many were expired."""
    overdue = ExhibitionOrder.objects.filter(
        status=ExhibitionOrderStatus.PENDING,
        expires__lt=now or timezone.now(),
    ).select_related("event")
    expired = 0
    for order in overdue:
        try:
            order.expire()
        except ExhibitionOrderStateError:
            continue
        expired += 1
    return expired
