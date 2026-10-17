Selling sponsorship and exhibition packages
===========================================

This guide is for organisers. It explains how to sell sponsorship and
exhibition packages with the Exhibition plugin, from setting up products to
the organisation profiles that paid orders create.

Exhibition products are separate from tickets
---------------------------------------------

Exhibition and sponsorship products are their own products. They are managed
only under **Exhibition → Products** and sold on the event's
**Call for Exhibitors** page. They never appear in the ticket shop, and ticket
products never appear on the call for exhibitors page. Exhibition orders
are also separate from ticket orders: they are listed under
**Exhibition → Orders** and not in the ticket order list.

Creating products
-----------------

Go to **Exhibition → Products** and click **Create a new product**. Each
product has:

- a name, a description and a price; a price of 0 makes it a free product;
- an optional category, which groups products on the public page, for example
  "Sponsorship" and "Exhibition";
- **Active**, and an optional **Available from** / **Available until** window.
  The public page only lists active products inside their window;
- a **Product purpose**: *Sponsorship* or *Exhibition*;
- **Includes exhibition booth**.

Exhibition products always include a booth. Sponsorship products include a
booth by default, because most sponsorship packages come with a stand. Turn
**Includes exhibition booth** off for packages without physical space, such
as a digital or logo-only sponsorship.

What a paid order turns the buyer into:

============================  ===========  ================
Product                       Sponsor      Exhibition booth
============================  ===========  ================
Exhibition product            No           Yes
Sponsorship with a booth      Yes          Yes
Sponsorship without a booth   Yes          No
============================  ===========  ================

Limiting the number of booths
-----------------------------

Go to **Exhibition → Products → Quotas** and create a quota:

1. Give it a name, such as "Exhibition hall".
2. Set **Total capacity** to the number of booths you have. Leave it empty
   for an unlimited quota.
3. Tick **Booth capacity**. A booth capacity quota only accepts products that
   include a booth.
4. Select every product that takes a booth from this space.

Pending and paid orders take up a place. When an order is cancelled or
expires, its place is free again. Once a quota is full, its products show as
**Sold out** on the public page. The products page shows how many booths are
left, and warns you about booth products that are not on any booth quota.

The order form
--------------

Go to **Exhibition → Products → Order form**. Like the exhibitor form, it is
one table of fields: turn each field on or off, set whether it is required,
and drag rows to set the order buyers see them in.

The built-in fields are asked on every order. Organization, contact person
and email are always asked. Phone and website are on by default, and billing
address and VAT ID are off by default.

Click **New custom field** to add your own questions, such as booth power or
furniture needs. Each custom field lists the products it is asked for. A
buyer only answers the fields of the products they order, and a field with no
products is never asked.

Payment
-------

Exhibition orders can be paid by **Manual payment** or **Bank transfer**.
Enable and configure them in the event's payment settings, the same as for
tickets. Other payment methods are not offered for exhibition orders. If
products with a price are on sale but neither method is enabled, the products
page shows a warning.

A paid order stays pending until you receive the payment. The buyer has the
event's payment term to pay. After that, the order expires and frees its
quota. To record a payment, open the order and click **Mark as paid**. You can
also mark an expired order as paid while its quotas still have room.

While an order is pending, the buyer's order page shows how to pay and a
**Re-try payment or choose another payment method** button, the same as for
tickets. If the buyer has lost the email, click **Resend link** next to their
email address on the order page.

The public page and checkout
----------------------------

Products on sale are listed on the **Call for Exhibitors** page, below the
call itself, in the same layout as the ticket products on the event's front
page. The page and its tab are shown whenever a product is on sale, even if
the call is closed, private or turned off; in that case only the packages are
shown. Buyers do not need an account. The steps are:

1. select the packages; each one shows its price, its purpose and whether it
   includes an exhibition booth;
2. fill in the order form;
3. choose a payment method (skipped when the order is free);
4. review the order and place it;
5. see the confirmation page with the payment instructions. An email links
   back to this page.

Free products
-------------

A product with a price of 0 has no payment step. Its order is confirmed at
once, but otherwise works like a paid order: the buyer fills in the order
form, the order takes its quota place, it creates the organisation profile,
and the buyer gets the confirmation email. Use free products for community
booths.

Creating orders yourself
------------------------

For negotiated sponsorships, complimentary booths, invited exhibitors and
offline agreements, go to **Exhibition → Orders** and click **Create order**:

1. choose the products; inactive products and products outside their sales
   window can be booked here too;
2. fill in the order form. Custom fields are optional here;
3. set the payment status to *paid* or *pending*, with an optional payment
   method and reference;
4. choose whether the buyer gets the order email.

The order then behaves like one placed on the public page: it takes up quota
places, appears in the orders list, and creates the organisation profile once
it is paid.

Organisation profiles from orders
---------------------------------

When an order is paid, or confirmed because it is free, the plugin creates
an organisation profile for the buyer. If the buyer already has one, it is
updated instead:

- An existing profile is found by the organisation name only, because one
  email address can manage several organisations. An organisation that buys
  twice, or buys both a sponsorship and a booth, keeps one profile, which
  gains the roles of both orders.
- A new profile takes its name, email and website from the order and is not
  published. Review and publish it as usual.
- The profile shows the orders linked to it, the answers to their order form,
  and which order created it. The order page links back to the profile.

When an order is cancelled, its booth goes back to the quota. If the profile
was created by an order, and no other paid order and no accepted exhibitor
request keeps it, the profile is deactivated and taken off the public site.
It is not deleted, so a later payment, or reactivating it yourself, brings it
back. A profile you created yourself, or one from an exhibitor request, is
never hidden or reactivated by an order; its visibility stays as you set it.

Emails
------

Two emails go to the buyer. You can edit both under
**Exhibition → Message center → Templates**:

- **Order placed (awaiting payment)**, with the products, the total and how
  to pay;
- **Order confirmed**, sent when a free order is placed, and when you mark an
  order as paid and keep **Send the order confirmation email** ticked.
