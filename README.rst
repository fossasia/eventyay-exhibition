Eventyay Exhibition
===================

Eventyay Exhibition is a plugin for `eventyay`_ that adds exhibitor,
sponsor, exhibition booth, proposal, and lead-scanning features to events.

The plugin allows organizers to manage exhibitors and sponsors, publish
public exhibitor pages, collect exhibitor and sponsor applications, configure
custom application questions, and provide exhibitors with access to lead
scanning functionality.

Features
--------

- Manage event exhibitors, sponsors, partners, booths, and sponsor groups.
- Add exhibitor profiles with descriptions, website links, contact links,
  videos, logos, header images, and promotional PDF slides.
- Mark organizations as exhibitors, sponsors, both, or partners that are not
  exhibitors.
- Configure sponsor groups and show selected sponsor groups on the event front
  page.
- Publish public exhibitor lists and exhibitor detail pages for an event.
- Enable a public call for exhibitor and sponsor applications.
- Configure proposal fields and custom application questions.
- Review exhibitor and sponsor proposals with states such as submitted,
  accepted, rejected, withdrawn, and draft.
- Generate exhibitor access keys.
- Enable exhibitor lead scanning and retrieve scanned lead data.
- Support notes and tags for scanned leads.
- Provide REST API endpoints for exhibitor data and lead scanning workflows.

Repository branches
-------------------

The repository uses two main branches:

- ``dev``: active development branch and default branch.
- ``main``: stable branch for reviewed changes.

Contributors should normally create pull requests against ``dev`` unless a
maintainer requests a different target branch.

Development setup
-----------------

1. Make sure that you have a working `eventyay development setup`_.

2. Clone this repository, e.g., to ``local/eventyay-exhibition``::

       git clone https://github.com/fossasia/eventyay-exhibition.git local/eventyay-exhibition
       cd local/eventyay-exhibition

3. Check out the development branch::

       git checkout dev

4. Activate the `virtual environment <https://github.com/fossasia/eventyay?tab=readme-ov-file#getting-started>`_ you use for eventyay development.

5. Execute ``uv pip install -e .`` within this directory to register this application with the eventyay plugin registry::

       uv pip install -e .

6. Execute ``make`` within this directory to compile translations::

       make

7. Restart your local eventyay server. You can now use the plugin from this repository for your events by enabling it in
   the ``plugins`` tab in the settings.

Python version
--------------

The plugin currently targets Python 3.12.

Useful commands
---------------

Install the plugin locally::

    uv pip install -e .

Install test dependencies::

    uv pip install -e ".[test]"

Compile translations::

    make

Generate translation files::

    make localegen

Run tests::

    pytest

Run formatting and lint checks::

    ruff format --check .
    ruff check .

Auto-fix formatting and lint issues where possible::

    ruff format .
    ruff check --fix .

Code style
----------

The repository uses Ruff for formatting, import sorting, and linting. Run Ruff
and the test suite before opening a pull request.

To install the local commit hooks, install `pre-commit`_ and run::

    pre-commit install

Project structure
-----------------

Important files and directories:

- ``exhibition/models.py``: exhibitor, sponsor, proposal, question, lead, and
  tag models.
- ``exhibition/forms.py``: organizer-facing and public application forms.
- ``exhibition/views.py``: organizer and public web views.
- ``exhibition/api.py``: REST API serializers and lead scanning endpoints.
- ``exhibition/urls.py``: public, organizer, and API routes.
- ``exhibition/templates/``: HTML templates for exhibitor pages, settings,
  proposals, and public call pages.
- ``exhibition/static/``: JavaScript and static assets.
- ``tests/``: test suite.

Public routes
-------------

The plugin provides public event routes for:

- exhibitor list pages
- exhibitor detail pages
- public exhibition calls
- proposal submission and proposal editing by applicants

Organizer routes
----------------

Organizers can manage:

- exhibitor and sponsor entries
- sponsor groups
- exhibition settings
- public call settings
- proposal review
- custom application questions

API endpoints
-------------

The plugin exposes API endpoints for:

- exhibitor authentication by access key
- exhibitor data
- lead creation
- lead retrieval
- lead updates
- exhibitor lead tags

These endpoints are intended for integration with exhibitor-facing tools such
as lead scanning applications.

Each ``ExhibitorInfo`` carries three independent access flags, all returned
by the exhibitor-authentication endpoint so that any client can inspect
them, and enforced server-side where noted:

- ``lead_scanning_enabled``: the exhibitor's devices may scan badges and
  create new leads at all. Enforced by the lead-creation endpoint, which
  returns 403 when this is off.
- ``allow_lead_access``: the exhibitor may read, export, or annotate leads
  that were already scanned for them. This is separate from
  ``lead_scanning_enabled`` and is enforced by this plugin's lead
  retrieval, lead update, and lead-tag endpoints.
- ``allow_voucher_access``: whether personal attendee data (name, email,
  company, address, question answers) is attached to a scan at all. When
  off, scanning still records a lead so duplicate-scan detection keeps
  working, but only an empty note/tags shell is stored — no personal
  data. Enforced by the lead-creation endpoint.

Contributing
------------

- Use ``dev`` as the base branch for regular development pull requests.
- Keep pull requests focused and small enough to review.
- Include screenshots or screencasts for user interface changes.
- Add or update tests for behavior changes.
- Run Ruff checks and tests before opening a pull request.
- Avoid unrelated cleanups in feature or bug-fix pull requests.

License
-------

Copyright 2024 FOSSASIA

Released under the terms of the Apache License 2.0.

.. _eventyay: https://github.com/fossasia/eventyay
.. _eventyay development setup: https://github.com/fossasia/eventyay?tab=readme-ov-file#getting-started
.. _pre-commit: https://pre-commit.com/
