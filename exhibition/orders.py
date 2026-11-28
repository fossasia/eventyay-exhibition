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
    if exhibition_order.status != ExhibitionOrderStatus.PENDING or exhibition_order.require_approval:
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


def send_order_email(order, *, user=None, role=None):
    """Email the buyer after commit: paid confirmation, approval-needed, or payment instructions."""
    if role is None:
        if order.status == ExhibitionOrderStatus.PAID:
            role = mail.ORDER_PAID
        elif order.require_approval:
            role = mail.ORDER_PLACED_REQUIRE_APPROVAL
        else:
            role = mail.ORDER_PLACED
    transaction.on_commit(
        lambda: mail.queue_order_email(order.event, order, role, send_now=True, requestor=user),
        robust=True,
    )


def resolve_public_addon_list(products, addon_list):
    """Validate public add-on selections against current rules and recalculate prices.

    Organiser-placed orders skip this via ``check_availability=False``.
    """
    by_base = defaultdict(list)
    for base_idx, addon_prod, _price in addon_list:
        by_base[base_idx].append(addon_prod)

    resolved = []
    for base_idx, product in enumerate(products):
        selected = by_base.get(base_idx, [])
        rules = list(product.addons.select_related("addon_category").order_by("position", "id"))
        remaining = list(selected)
        for rule in rules:
            matching = [p for p in remaining if p.category_id == rule.addon_category_id]
            count = len(matching)
            if count < rule.min_count or count > rule.max_count:
                raise ExhibitionOrderError("Add-on selection does not match the current product rules.")
            if not rule.multi_allowed and len({p.pk for p in matching}) != count:
                raise ExhibitionOrderError("Add-on selection does not match the current product rules.")
            for addon_prod in matching:
                price = Decimal("0.00") if rule.price_included else addon_prod.price
                resolved.append((base_idx, addon_prod, price))
            remaining = [p for p in remaining if p.category_id != rule.addon_category_id]
        if remaining:
            raise ExhibitionOrderError("Add-on selection does not match the current product rules.")

    for base_idx in by_base:
        if base_idx < 0 or base_idx >= len(products):
            raise ExhibitionOrderError("Add-on selection does not match the current product rules.")

    return resolved


def place_exhibition_order(
    event,
    products,
    *,
    addons=None,
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

    addon_list = list(addons or [])

    now = timezone.now()
    for product in products:
        if product.event_id != event.pk or (check_availability and not product.is_available(now)):
            raise ExhibitionOrderError(f"{product} is not on sale.")

    for _base_idx, addon_prod, _price in addon_list:
        if addon_prod.event_id != event.pk or (check_availability and not addon_prod.is_available(now)):
            raise ExhibitionOrderError(f"{addon_prod} is not on sale.")

    if check_availability:
        addon_list = resolve_public_addon_list(products, addon_list)

    all_products = products + [p for _base_idx, p, _price in addon_list]
    base_total = sum((product.price for product in products), Decimal("0.00"))
    addons_total = sum((price for _base_idx, _p, price in addon_list), Decimal("0.00"))
    total = base_total + addons_total
    # Organiser-created paid orders bypass approval; public and unpaid bookings do not.
    needs_approval = (not paid) and any(product.require_approval for product in all_products)
    providers = usable_payment_providers(event)

    # Paid approval-gated orders skip choosing a method at checkout, but a usable method must
    # still exist so the buyer can pay after approval.
    if check_availability and total and not providers:
        raise ExhibitionOrderError("The payment method is not available for this event.")
    if check_availability and total and not needs_approval and payment_provider not in providers:
        raise ExhibitionOrderError("The payment method is not available for this event.")

    with transaction.atomic():
        reserve_quota_capacity(all_products)
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
            payment_provider=payment_provider if total and not needs_approval else "",
            require_approval=needs_approval,
            expires=(
                now + timedelta(days=event.settings.get("payment_term_days", as_type=int))
                if total and not paid and not needs_approval
                else None
            ),
        )
        base_positions = ExhibitionOrderPosition.objects.bulk_create(
            [ExhibitionOrderPosition.for_product(order, product) for product in products]
        )
        addon_positions = []
        for a_base_idx, addon_product, price in addon_list:
            if a_base_idx < 0 or a_base_idx >= len(base_positions):
                raise ExhibitionOrderError("Add-on selection does not match the current product rules.")
            addon_positions.append(
                ExhibitionOrderPosition.for_product(
                    order,
                    addon_product,
                    addon_to=base_positions[a_base_idx],
                    price=price,
                )
            )
        if addon_positions:
            ExhibitionOrderPosition.objects.bulk_create(addon_positions)

        if order_form is not None:
            order_form.save_exhibition_questions(order)
        order.log_action(
            LOG_ORDER_PLACED,
            data={
                "total": str(total),
                "products": [product.pk for product in all_products],
                "require_approval": needs_approval,
            },
            user=user,
        )
        if needs_approval:
            pass
        elif not total:
            order.mark_paid(provider=FREE_PAYMENT_PROVIDER, user=user)
        elif paid:
            order.mark_paid(provider=payment_provider, reference=payment_reference, user=user)
    return order


def expire_overdue_orders(now=None):
    """Expire pending orders whose payment term has passed and return how many were expired."""
    overdue = ExhibitionOrder.objects.filter(
        status=ExhibitionOrderStatus.PENDING,
        require_approval=False,
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
