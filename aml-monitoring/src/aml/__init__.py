"""AML transaction monitoring with graph analytics and case management.

Pipeline: ``synth`` builds a seeded world, ``detect`` runs the ``rules`` and ``graph``
detectors, ``cases`` scores and works the alerts, ``evaluate`` measures against ground
truth and ``report`` renders the results. ``run`` is the entry point.
"""

__version__ = "1.0.0"
