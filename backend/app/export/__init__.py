"""Portable research run exports."""

from app.export.mix import Mix, rank_cells
from app.export.report import render_html, render_markdown
from app.export.run import RunBundle, load_run
from app.export.sources import sources_bibtex, sources_csv

EXPORT_FORMATS = {
    "report.md": ("text/markdown; charset=utf-8", render_markdown),
    "report.html": ("text/html; charset=utf-8", render_html),
    "sources.csv": ("text/csv; charset=utf-8", sources_csv),
    "sources.bib": ("application/x-bibtex; charset=utf-8", sources_bibtex),
}

__all__ = [
    "RunBundle",
    "load_run",
    "Mix",
    "rank_cells",
    "render_markdown",
    "render_html",
    "sources_csv",
    "sources_bibtex",
    "EXPORT_FORMATS",
]
