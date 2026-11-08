"""latex_measure: measure the typeset size of LaTeX snippets under a real document style.

    from latex_measure import LatexMeasurer
    m = LatexMeasurer(template_dir="path/to/DND-5e-LaTeX-Template")
    r = m.measure(r"\\begin{DndReadAloud}Some text.\\end{DndReadAloud}")

See README.md for usage and DESIGN.md for how it works.
"""
from .core import (
    DEFAULT_PREAMBLE_PATH,
    LatexMeasureError,
    LatexMeasurer,
    Measurement,
    MeasureRun,
    PageMetrics,
    Segment,
    SplitMeasurement,
    SplitRun,
    check_balance,
    extract_body,
    main,
    measure_latex,
    split_segments,
)

__version__ = "0.1.0"

__all__ = [
    "LatexMeasurer", "Measurement", "MeasureRun", "PageMetrics", "Segment",
    "SplitMeasurement", "SplitRun", "LatexMeasureError",
    "split_segments", "measure_latex", "check_balance", "extract_body",
    "DEFAULT_PREAMBLE_PATH", "main", "__version__",
]
