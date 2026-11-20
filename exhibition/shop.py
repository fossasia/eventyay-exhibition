import uuid
from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.datastructures import MultiValueDict
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from eventyay.control.forms import ExtFileField

from .forms import ExhibitionOrderForm
from .models import (
    ExhibitionOrder,
    ExhibitionOrderError,
    ExhibitionOrderStateError,
    ExhibitionOrderStatus,
    ExhibitionProduct,
    ExhibitionQuotaExceeded,
)
from .orders import payment_instructions, place_exhibition_order, send_order_email, usable_payment_providers


def checkout_session_key(event):
    return f"exhibition_checkout_{event.pk}"


def shop_products(event):
    """The exhibition products on sale now, in sales order, each told whether its quotas are sold out."""
    products = list(
        ExhibitionProduct.objects.filter(event=event)
        .available()
        .exclude(category__is_addon=True)
        .in_sales_order()
        .select_related("category")
    )
    for product in products:
        product.remaining = product.remaining_capacity()
        product.sold_out = product.remaining == 0
    return products


def group_by_category(products):
    groups = []
    for product in products:
        if groups and groups[-1][0] == product.category:
            groups[-1][1].append(product)
        else:
            groups.append((product.category, [product]))
    return groups


def shop_url(event, name="shop", **kwargs):
    return reverse(
        f"plugins:exhibition:{name}",
        kwargs={"organizer": event.organizer.slug, "event": event.slug, **kwargs},
    )


def answer_text(value):
    """A submitted custom field value as the buyer reads it back on the summary."""
    if value is True:
        return _("Yes")
    if isinstance(value, UploadedFile):
        return value.name
    if hasattr(value, "__iter__") and not isinstance(value, str):
        return ", ".join(str(item) for item in value)
    return str(value)


def packages_url(event):
    """Where buyers pick packages: the call for exhibitors page, at its package list."""
    return shop_url(event, "public_call") + "#exhibition-packages"


def delete_checkout_files(files):
    for info in files.values():
        default_storage.delete(info["path"])


class ShopIndexView(View):
    """Starts the checkout with the packages picked on the call for exhibitors page."""

    @cached_property
    def products(self):
        return shop_products(self.request.event)

    def get(self, request, *args, **kwargs):
        return redirect(packages_url(request.event))

    def post(self, request, *args, **kwargs):
        on_sale = {str(product.pk): product for product in self.products}
        chosen = [on_sale.get(pk) for pk in request.POST.getlist("product")]
        if not chosen:
            messages.error(request, _("Please select at least one product."))
            return redirect(packages_url(request.event))
        if None in chosen:
            messages.error(request, _("One of the selected products is no longer on sale."))
            return redirect(packages_url(request.event))
        sold_out = [product for product in chosen if product.sold_out]
        if sold_out:
            messages.error(
                request,
                _("{products} is sold out.").format(products=", ".join(str(product) for product in sold_out)),
            )
            return redirect(packages_url(request.event))
        key = checkout_session_key(request.event)
        cart = request.session.get(key, {})
        cart["products"] = [product.pk for product in chosen]
        cart.pop("addons", None)
        request.session[key] = cart
        if any(p.addons.exists() for p in chosen):
            return redirect(shop_url(request.event, "shop.checkout.addons"))
        return redirect(shop_url(request.event, "shop.checkout.details"))


class ShopCheckoutMixin:
    """A checkout step working on the products and answers kept in the buyer's session."""

    step = None

    @property
    def cart(self):
        return self.request.session.get(checkout_session_key(self.request.event), {})

    def save_cart(self, **values):
        key = checkout_session_key(self.request.event)
        cart = self.request.session.get(key, {})
        cart.update(values)
        self.request.session[key] = cart

    @cached_property
    def products(self):
        pk_list = self.cart.get("products", [])
        if not pk_list:
            return []
        products_by_id = {
            product.pk: product
            for product in ExhibitionProduct.objects.filter(event=self.request.event, pk__in=pk_list)
            .available()
            .select_related("category")
        }
        return [products_by_id.get(pk) for pk in pk_list]

    @property
    def has_addons_step(self):
        return any(p and p.addons.exists() for p in self.products)

    @cached_property
    def addon_items(self):
        """List of tuples: (base_index, ExhibitionProduct, Decimal(price))"""
        addons_data = self.cart.get("addons", [])
        if not addons_data:
            return []
        addon_pks = [item["product_id"] for item in addons_data]
        products_by_id = {
            p.pk: p
            for p in ExhibitionProduct.objects.filter(event=self.request.event, pk__in=addon_pks)
            .available()
            .select_related("category")
        }
        items = []
        for item in addons_data:
            prod = products_by_id.get(item["product_id"])
            if prod:
                price = Decimal(str(item.get("price", prod.price)))
                items.append((item["base_index"], prod, price))
        return items

    @cached_property
    def all_products(self):
        return [p for p in self.products if p] + [prod for _, prod, _ in self.addon_items]

    @cached_property
    def summary_entries(self):
        addons_by_base = defaultdict(list)
        for base_idx, prod, price in self.addon_items:
            addons_by_base[base_idx].append({"product": prod, "price": price})
        entries = []
        for idx, base_prod in enumerate(self.products):
            if base_prod:
                entries.append(
                    {
                        "product": base_prod,
                        "addons": addons_by_base.get(idx, []),
                    }
                )
        return entries

    @cached_property
    def total(self):
        base_total = sum((product.price for product in self.products if product), Decimal("0.00"))
        addons_total = sum((price for _, _, price in self.addon_items), Decimal("0.00"))
        return base_total + addons_total

    def dispatch(self, request, *args, **kwargs):
        if not self.cart.get("products"):
            messages.info(request, _("Please select the products you want to order."))
            return redirect(packages_url(request.event))
        if None in self.products:
            messages.error(request, _("One of the selected products is no longer on sale."))
            return redirect(packages_url(request.event))
        return self.check_step() or super().dispatch(request, *args, **kwargs)

    def check_step(self):
        """A redirect to an earlier step when this one cannot be shown yet."""
        return None

    def stored_files(self):
        files = MultiValueDict()
        for name, info in self.cart.get("files", {}).items():
            if default_storage.exists(info["path"]):
                files[name] = UploadedFile(
                    file=default_storage.open(info["path"]),
                    name=info["name"],
                    size=default_storage.size(info["path"]),
                )
        return files

    def stored_order_form(self):
        details = self.cart.get("details")
        if details is None:
            return None
        return ExhibitionOrderForm(
            data=MultiValueDict(details),
            files=self.stored_files(),
            event=self.request.event,
            products=self.all_products,
        )

    def checkout_flow(self):
        steps = []
        if self.has_addons_step:
            steps.append(("addons", _("Add-ons"), "shop.checkout.addons"))
        steps.append(("details", _("Your details"), "shop.checkout.details"))
        if self.total:
            steps.append(("payment", _("Payment"), "shop.checkout.payment"))
        steps.append(("confirm", _("Review order"), "shop.checkout.confirm"))
        identifiers = [identifier for identifier, label, name in steps]
        current = identifiers.index(self.step) if self.step in identifiers else 0
        return [
            {
                "label": label,
                "url": shop_url(self.request.event, name),
                "done": position < current,
                "current": position == current,
            }
            for position, (identifier, label, name) in enumerate(steps)
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["event"] = self.request.event
        context["products"] = self.products
        context["total"] = self.total
        context["summary_entries"] = self.summary_entries
        context["checkout_flow"] = self.checkout_flow()
        return context


class ShopAddOnsView(ShopCheckoutMixin, TemplateView):
    """Where the buyer chooses add-ons for packages in their cart."""

    template_name = "exhibitors/shop_addons.html"
    step = "addons"

    def check_step(self):
        if not self.has_addons_step:
            return redirect(shop_url(self.request.event, "shop.checkout.details"))
        return None

    def get_addon_groups(self):
        existing_addons = self.cart.get("addons", [])
        groups = []
        for base_idx, base_prod in enumerate(self.products):
            if not base_prod or not base_prod.addons.exists():
                continue
            cat_groups = []
            for rule in base_prod.addons.select_related("addon_category").order_by("position", "id"):
                products = list(
                    ExhibitionProduct.objects.filter(event=self.request.event, category=rule.addon_category)
                    .available()
                    .in_sales_order()
                )
                selected_prod_id = None
                for p in products:
                    p.remaining = p.remaining_capacity()
                    p.sold_out = p.remaining == 0
                    matching_count = sum(
                        1
                        for a in existing_addons
                        if a.get("base_index") == base_idx
                        and a.get("rule_id") == rule.pk
                        and a.get("product_id") == p.pk
                    )
                    p.selected_count = matching_count
                    if matching_count > 0:
                        selected_prod_id = p.pk
                cat_groups.append(
                    {
                        "rule": rule,
                        "category": rule.addon_category,
                        "products": products,
                        "selected_product_id": selected_prod_id,
                    }
                )
            groups.append(
                {
                    "index": base_idx,
                    "product": base_prod,
                    "categories": cat_groups,
                }
            )
        return groups

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["addon_groups"] = self.get_addon_groups()
        context["prev_url"] = packages_url(self.request.event)
        return context

    def post(self, request, *args, **kwargs):
        chosen_addons = []
        errors = []
        for base_idx, base_prod in enumerate(self.products):
            if not base_prod or not base_prod.addons.exists():
                continue
            for rule in base_prod.addons.select_related("addon_category").order_by("position", "id"):
                products = list(
                    ExhibitionProduct.objects.filter(event=request.event, category=rule.addon_category)
                    .available()
                    .in_sales_order()
                )
                products_by_id = {p.pk: p for p in products}
                rule_total_count = 0

                if rule.max_count == 1 and not rule.multi_allowed:
                    key = f"addon_{base_idx}_{rule.pk}"
                    prod_pk = request.POST.get(key)
                    if prod_pk:
                        try:
                            prod_pk = int(prod_pk)
                            p = products_by_id.get(prod_pk)
                            if p:
                                p.remaining = p.remaining_capacity()
                                if p.remaining == 0:
                                    errors.append(_("{product} is sold out.").format(product=p))
                                else:
                                    rule_total_count += 1
                                    chosen_addons.append(
                                        {
                                            "base_index": base_idx,
                                            "product_id": p.pk,
                                            "price": "0.00" if rule.price_included else str(p.price),
                                            "rule_id": rule.pk,
                                        }
                                    )
                            else:
                                errors.append(_("Invalid add-on product selected."))
                        except (ValueError, TypeError):
                            pass
                else:
                    for p in products:
                        key = f"addon_{base_idx}_{rule.pk}_{p.pk}"
                        count_val = request.POST.get(key, 0)
                        try:
                            count = int(count_val)
                        except (ValueError, TypeError):
                            count = 0
                        if count > 0:
                            if not rule.multi_allowed and count > 1:
                                errors.append(_("You cannot select multiple of {product}.").format(product=p))
                            p.remaining = p.remaining_capacity()
                            if p.remaining == 0:
                                errors.append(_("{product} is sold out.").format(product=p))
                            else:
                                rule_total_count += count
                                price_str = "0.00" if rule.price_included else str(p.price)
                                for _count_idx in range(count):
                                    chosen_addons.append(
                                        {
                                            "base_index": base_idx,
                                            "product_id": p.pk,
                                            "price": price_str,
                                            "rule_id": rule.pk,
                                        }
                                    )

                if rule_total_count < rule.min_count:
                    errors.append(
                        _("Please select at least {min} options for {category} on {package}.").format(
                            min=rule.min_count,
                            category=rule.addon_category,
                            package=base_prod,
                        )
                    )
                elif rule_total_count > rule.max_count:
                    errors.append(
                        _("You can select at most {max} options for {category} on {package}.").format(
                            max=rule.max_count,
                            category=rule.addon_category,
                            package=base_prod,
                        )
                    )

        if errors:
            for err in errors:
                messages.error(request, err)
            return self.render_to_response(self.get_context_data())

        self.save_cart(addons=chosen_addons)
        return redirect(shop_url(request.event, "shop.checkout.details"))


class ShopDetailsView(ShopCheckoutMixin, TemplateView):
    """The order form: who is ordering, and the custom fields of the chosen products."""

    template_name = "exhibitors/shop_details.html"
    step = "details"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = kwargs.get("form") or self.stored_order_form()
        if form is None:
            user = self.request.user
            initial = {"email": user.email, "name": user.get_full_name()} if user.is_authenticated else {}
            form = ExhibitionOrderForm(initial=initial, event=self.request.event, products=self.all_products)
        context["form"] = form
        context["prev_url"] = (
            shop_url(self.request.event, "shop.checkout.addons")
            if self.has_addons_step
            else packages_url(self.request.event)
        )
        return context

    def post(self, request, *args, **kwargs):
        files = self.stored_files()
        for name in request.FILES:
            files[name] = request.FILES[name]
        form = ExhibitionOrderForm(data=request.POST, files=files, event=request.event, products=self.all_products)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        stored = dict(self.cart.get("files", {}))
        for name, field in form.fields.items():
            upload = request.FILES.get(name)
            if not isinstance(field, ExtFileField) or upload is None:
                continue
            if name in stored:
                default_storage.delete(stored[name]["path"])
            path = default_storage.save(
                f"exhibition/checkout/{request.event.pk}/{uuid.uuid4().hex}/{upload.name}",
                upload,
            )
            stored[name] = {"path": path, "name": upload.name}
        details = {key: values for key, values in request.POST.lists() if key != "csrfmiddlewaretoken"}
        self.save_cart(details=details, files=stored)
        next_step = "shop.checkout.payment" if self.total else "shop.checkout.confirm"
        return redirect(shop_url(request.event, next_step))


class ShopPaymentView(ShopCheckoutMixin, TemplateView):
    """The choice of how to pay, offering the event's payment methods that exhibition orders support."""

    template_name = "exhibitors/shop_payment.html"
    step = "payment"

    @cached_property
    def providers(self):
        return usable_payment_providers(self.request.event)

    def check_step(self):
        if not self.total:
            return redirect(shop_url(self.request.event, "shop.checkout.confirm"))
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["providers"] = [
            {"provider": provider, "form": provider.payment_form_render(self.request)}
            for provider in self.providers.values()
        ]
        context["selected"] = self.cart.get("payment")
        context["prev_url"] = shop_url(self.request.event, "shop.checkout.details")
        return context

    def post(self, request, *args, **kwargs):
        payment = request.POST.get("payment")
        if payment not in self.providers:
            messages.error(request, _("Please select a payment method."))
            return self.render_to_response(self.get_context_data())
        self.save_cart(payment=payment)
        return redirect(shop_url(request.event, "shop.checkout.confirm"))


class ShopConfirmView(ShopCheckoutMixin, TemplateView):
    """The summary of the order, where the buyer places it."""

    template_name = "exhibitors/shop_confirm.html"
    step = "confirm"

    @cached_property
    def order_form(self):
        return self.stored_order_form()

    @cached_property
    def payment_provider(self):
        return usable_payment_providers(self.request.event).get(self.cart.get("payment")) if self.total else None

    def check_step(self):
        if self.order_form is None or not self.order_form.is_valid():
            messages.error(self.request, _("Please check your details."))
            return redirect(shop_url(self.request.event, "shop.checkout.details"))
        if self.total and self.payment_provider is None:
            messages.error(self.request, _("Please select a payment method."))
            return redirect(shop_url(self.request.event, "shop.checkout.payment"))
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = self.order_form
        context["contact_info"] = [
            (form.fields[key].label, value) for key, value in form.order_details().items() if value
        ]
        context["answers"] = [
            (field.label, answer_text(form.cleaned_data.get(name)))
            for name, field in form.question_fields()
            if name not in form.hidden_question_fields and form.cleaned_data.get(name) not in (None, "", [], False)
        ]
        context["payment_provider"] = self.payment_provider
        if self.payment_provider is not None:
            context["payment"] = self.payment_provider.checkout_confirm_render(self.request)
        context["prev_url"] = shop_url(
            self.request.event, "shop.checkout.payment" if self.total else "shop.checkout.details"
        )
        return context

    def post(self, request, *args, **kwargs):
        form = self.order_form
        try:
            order = place_exhibition_order(
                request.event,
                self.products,
                addons=self.addon_items,
                **form.order_details(),
                order_form=form,
                payment_provider=self.payment_provider.identifier if self.payment_provider else "",
                user=request.user if request.user.is_authenticated else None,
            )
        except ExhibitionQuotaExceeded:
            messages.error(request, _("Sorry, one of the selected products has sold out in the meantime."))
            return redirect(packages_url(request.event))
        except ExhibitionOrderError:
            messages.error(request, _("Your order could not be placed. Please check your selection."))
            return redirect(packages_url(request.event))
        send_order_email(order)
        files = self.cart.get("files", {})
        transaction.on_commit(lambda: delete_checkout_files(files))
        request.session.pop(checkout_session_key(request.event), None)
        return redirect(shop_url(request.event, "shop.order", code=order.code, secret=order.secret) + "?thanks=1")


class ShopOrderMixin:
    """An order reached with its secret link from the checkout and the emails."""

    @cached_property
    def order(self):
        order = get_object_or_404(ExhibitionOrder, event=self.request.event, code=self.kwargs["code"])
        if order.secret != self.kwargs["secret"]:
            raise Http404()
        return order

    def order_url(self):
        return shop_url(self.request.event, "shop.order", code=self.order.code, secret=self.order.secret)


class ShopOrderView(ShopOrderMixin, TemplateView):
    """The buyer's page for one order."""

    template_name = "exhibitors/shop_order.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = self.order
        context["event"] = self.request.event
        context["order"] = order
        context["positions"] = list(order.positions.all())
        context["answers"] = list(order.answers.select_related("question").prefetch_related("options"))
        context["thanks"] = "thanks" in self.request.GET
        context["is_pending"] = order.status == ExhibitionOrderStatus.PENDING
        context["is_paid"] = order.status == ExhibitionOrderStatus.PAID
        context["payment_instructions"] = payment_instructions(order)
        provider = self.request.event.get_payment_providers().get(order.payment_provider)
        context["payment_method"] = provider.public_name if provider else ""
        context["can_change_payment"] = context["is_pending"] and bool(usable_payment_providers(self.request.event))
        return context


class ShopOrderPayChangeView(ShopOrderMixin, TemplateView):
    """Where a buyer of a pending order sees how to pay again or picks another payment method."""

    template_name = "exhibitors/shop_order_pay_change.html"

    @cached_property
    def providers(self):
        return usable_payment_providers(self.request.event)

    def dispatch(self, request, *args, **kwargs):
        if self.order.status != ExhibitionOrderStatus.PENDING:
            messages.error(request, _("This order is not awaiting a payment."))
            return redirect(self.order_url())
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["event"] = self.request.event
        context["order"] = self.order
        context["providers"] = [
            {"provider": provider, "form": provider.payment_form_render(self.request)}
            for provider in self.providers.values()
        ]
        context["selected"] = self.order.payment_provider
        context["order_url"] = self.order_url()
        return context

    def post(self, request, *args, **kwargs):
        payment = request.POST.get("payment")
        if payment not in self.providers:
            messages.error(request, _("Please select a payment method."))
            return self.render_to_response(self.get_context_data())
        try:
            self.order.change_payment_provider(payment)
        except ExhibitionOrderStateError:
            messages.error(request, _("This order is not awaiting a payment."))
        return redirect(self.order_url())
