Exhibition
==========================

This is a plugin for `eventyay`_. 

This plugin enables you to add and control exhibitors in eventyay

Development setup
-----------------

1. Make sure that you have a working `eventyay development setup`_.

2. Clone this repository, e.g., to ``local/eventyay-exhibition``.

3. Activate the `virtual environment <https://github.com/fossasia/eventyay?tab=readme-ov-file#getting-started>`_ you use for eventyay development.

4. Execute ``uv pip install -e .`` within this directory to register this application with the eventyay plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local eventyay server. You can now use the plugin from this repository for your events by enabling it in
   the 'plugins' tab in the settings.


This plugin has CI set up to enforce a few code style rules. To check locally, you need these packages installed::

    pip install flake8 isort black

To check your plugin for rule violations, run::

    black --check .
    isort -c .
    flake8 .

You can auto-fix some of these issues by running::

    isort .
    black .

To automatically check for these issues before you commit, you can run ``.install-hooks``.


License
-------


Copyright 2024 FOSSASIA

Released under the terms of the Apache License 2.0



.. _eventyay-tickets: https://github.com/fossasia/eventyay-tickets 

Getting Started
===============

To run this project locally:

- Clone the repository
- Install dependencies
- Run the development server

More setup instructions can be added for beginners.
