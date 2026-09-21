"""The LinkedIn side: the browser extractor (P2) and the data-archive reader (P1-03).

Nothing in this package imports :mod:`netkeeper.models` or opens a database
session (spec 9.10, ADR 0005). Job specs and files go in; plain dataclasses
come out; the core maps them onto the database elsewhere.
"""
