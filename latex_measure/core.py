"""latex_measure.core: measure the typeset size of LaTeX snippets under a real document style.

Each snippet is typeset by a real TeX engine (pdflatex by default) inside a box of
a chosen width, under the same preamble as the real document, and the box's size is
read back. The numbers are what TeX itself computes, so layout code can use them
to decide whether a block still fits in the current column or page.

Use it as a module::

    from latex_measure import LatexMeasurer
    m = LatexMeasurer(template_dir="path/to/DND-5e-LaTeX-Template")
    r = m.measure(r"\\begin{DndReadAloud}Some text.\\end{DndReadAloud}")
    print(r.height_pt, r.top_total_pt, r.column_fraction)

or from the command line (the .tex file holds body text only, no preamble)::

    latex-measure body.tex --split blocks --template-dir path/to/template
    python -m latex_measure body.tex ...      (the same, without installing the script)

See README.md for usage and DESIGN.md for how it works.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

__all__ = [
    "LatexMeasurer", "Measurement", "MeasureRun", "PageMetrics", "Segment",
    "SplitMeasurement", "SplitRun",
    "LatexMeasureError", "split_segments", "measure_latex", "check_balance", "extract_body",
    "DEFAULT_PREAMBLE_PATH", "main",
]

DEFAULT_PREAMBLE_PATH = Path(__file__).with_name("preamble.tex")

TEX_PT_PER_INCH = 72.27       # TeX points ("pt"); PDF/PostScript points are "bp" (72/in)
TEX_PT_PER_BP = 72.27 / 72.0
TEX_PT_PER_MM = 72.27 / 25.4

# Width shortcuts; anything else is passed to TeX as a dimension expression.
WIDTH_ALIASES = {
    "column": r"\columnwidth", "col": r"\columnwidth",
    "full": r"\textwidth", "text": r"\textwidth", "page": r"\textwidth",
}

# Draft flags: typeset and measure, but skip writing the PDF.
ENGINE_DRAFT_FLAGS = {
    "pdflatex": ["-draftmode"],
    "lualatex": ["--draftmode"],
    "xelatex": ["-no-pdf"],
}

# ---------------------------------------------------------------------------
# The TeX side. Inserted between the preamble and \begin{document}.
#
#   \MTbegin{<index>}{<width>} <content> \MTend
#
# typesets <content> in a \vbox whose \hsize/\linewidth/\columnwidth are <width>,
# and writes one line to <jobname>.mtx:
#   SEG <index> <wd> <ht> <dp> <top-ht> <top-dp>
# "top" is the same box \vsplit at a forced break placed before the content, which
# makes TeX treat the content as the start of a fresh column: discardable space at
# the top (a heading's or table's \vspace above) is dropped and \topskip applied.
#
# Inside the box, floats are typeset in place (a float can't live in a box), and
# page-level commands (\clearpage, \chapter's \@topnewpage, \balance) are made
# harmless, so headings, figures and float=... sidebars can still be measured.
# ---------------------------------------------------------------------------
TEX_HARNESS = r"""
%% ---- latex_measure harness -------------------------------------------------
\makeatletter
\newbox\mt@box \newbox\mt@top \newdimen\mt@width
\newwrite\mt@out
\def\mt@inlinefloat#1#2[#3]{\par\begingroup\def\@captype{#1}\nointerlineskip
  \vbox\bgroup\hsize#2\relax\linewidth\hsize\@parboxrestore\@floatboxreset}
\def\mt@endinlinefloat{\par\egroup\endgroup\par}
\def\mt@neutralize{%
  \def\@float##1{\@ifnextchar[{\mt@inlinefloat{##1}\columnwidth}{\mt@inlinefloat{##1}\columnwidth[]}}%
  \def\@dblfloat##1{\@ifnextchar[{\mt@inlinefloat{##1}\textwidth}{\mt@inlinefloat{##1}\textwidth[]}}%
  \let\end@float\mt@endinlinefloat \let\end@dblfloat\mt@endinlinefloat
  \let\clearpage\relax \let\cleardoublepage\relax \let\newpage\relax
  \def\@topnewpage[##1]{\par\nointerlineskip\vbox{\hsize\textwidth\linewidth\hsize
    \columnwidth\hsize\@parboxrestore##1}\par}%
  \let\balance\relax \let\nobalance\relax}
\newcommand\MTbegin[2]{\begingroup
  \mt@width=\dimexpr#2\relax \def\mt@idx{#1}%
  \setbox\mt@box=\vbox\bgroup
    \hsize\mt@width \linewidth\hsize \columnwidth\hsize \@totalleftmargin\z@
    \mt@neutralize
    \hrule\@height\z@\penalty-\@M\relax}
\newcommand\MTend{\par\egroup
  \setbox\mt@top=\copy\mt@box
  \splittopskip\topskip \splitmaxdepth\maxdepth \vbadness\@M \vfuzz\maxdimen
  \setbox\z@=\vsplit\mt@top to\z@
  \immediate\write\mt@out{SEG \mt@idx\space\the\wd\mt@box\space\the\ht\mt@box
    \space\the\dp\mt@box\space\the\ht\mt@top\space\the\dp\mt@top}%
  \endgroup}
\newcommand\MTmetrics{\begingroup
  \def\mt@m##1##2{##1=\the\dimexpr##2\relax\space}%
  \immediate\write\mt@out{METRICS
    \mt@m{columnwidth}\columnwidth \mt@m{textwidth}\textwidth
    \mt@m{textheight}\textheight \mt@m{columnsep}\columnsep
    \mt@m{baselineskip}\baselineskip \mt@m{topskip}\topskip
    \mt@m{maxdepth}\maxdepth \mt@m{parskip}\parskip
    \mt@m{floatsep}\floatsep \mt@m{textfloatsep}\textfloatsep
    \mt@m{intextsep}\intextsep \mt@m{dblfloatsep}\dblfloatsep
    \mt@m{dbltextfloatsep}\dbltextfloatsep
    \mt@m{paperwidth}\paperwidth \mt@m{paperheight}\paperheight}%
  \endgroup}
%% Split simulation: \MTsplitbegin{<index>}{<used>}{<depth>} <content> \MTsplitend
%% typesets <content> in the real page flow (not in a box) on a fresh page, after a
%% strut of height <used> and depth <depth> (the depth of the last line before the
%% block, which sets the space TeX puts between that line and the block), so floats, breakable boxes and paragraphs break exactly as
%% they would in the document. The output routine records the natural height of
%% every column that ends while a segment is active:
%%   START <index> <pagetotal> <pagegoal>      after the strut
%%   COL <index> <column height>               per finished column
%%   END <index> <pagetotal> <pagedepth> <pagegoal>
\newcount\mt@case \mt@case=\m@ne
\newbox\mt@col
%% strip trailing glue, kerns, penalties and empty boxes: count visible material only
%% (e-TeX \lastnodetype: 1/2 = box, 11 = glue, 12 = kern, 13 = penalty; anything else stops it)
\def\mt@strip{\let\mt@next\relax
  \ifnum\lastnodetype=1 \mt@stripbox\fi
  \ifnum\lastnodetype=2 \mt@stripbox\fi
  \ifnum\lastnodetype=11 \unskip\let\mt@next\mt@strip\fi
  \ifnum\lastnodetype=12 \unkern\let\mt@next\mt@strip\fi
  \ifnum\lastnodetype=13 \unpenalty\let\mt@next\mt@strip\fi
  \mt@next}
\def\mt@stripbox{\setbox\z@\lastbox
  \ifdim\dimexpr\ht\z@+\dp\z@\relax>\z@ \nointerlineskip\box\z@ \let\mt@next\mt@stopstrip
  \else\let\mt@next\mt@strip\fi}
\def\mt@stopstrip{}
\def\mt@colrec{\ifnum\mt@case>\m@ne \ifnum\outputpenalty<-\@M\else
  \setbox\mt@col=\vbox{\unvcopy\@cclv\mt@strip}%
  \immediate\write\mt@out{COL \the\mt@case\space\the\ht\mt@col}\fi\fi}
\newcommand\MTsplitbegin[3]{\clearpage
  \ifdim\dimexpr#2+#3\relax>\z@ \hbox{\vrule\@height\dimexpr#2\relax\@depth\dimexpr#3\relax\@width\z@}\fi
  \immediate\write\mt@out{START #1 \the\pagetotal\space\the\pagegoal}%
  \global\mt@case=#1\relax}
\newcommand\MTsplitend{\par\penalty\z@
  \immediate\write\mt@out{END \the\mt@case\space\the\pagetotal\space\the\pagedepth\space\the\pagegoal}%
  \global\mt@case\m@ne}
\AtBeginDocument{\immediate\openout\mt@out=\jobname.mtx\relax\MTmetrics
  \output=\expandafter{\expandafter\mt@colrec\the\output}}
\makeatother
%% ---- end harness -------------------------------------------------------------
"""


class LatexMeasureError(RuntimeError):
    """The engine failed, or a snippet produced no measurement."""

    def __init__(self, message: str, log_tail: str = ""):
        super().__init__(message + (f"\n--- log tail ---\n{log_tail}" if log_tail else ""))
        self.log_tail = log_tail


@dataclass
class PageMetrics:
    """Layout dimensions of the document class, in TeX points (pt)."""
    columnwidth: float
    textwidth: float
    textheight: float
    columnsep: float
    baselineskip: float
    topskip: float
    maxdepth: float
    parskip: float
    floatsep: float
    textfloatsep: float
    intextsep: float
    dblfloatsep: float
    dbltextfloatsep: float
    paperwidth: float
    paperheight: float


@dataclass
class Segment:
    """A piece of body text to measure."""
    name: str
    content: str
    start_line: int = 1          # line in the source file where the content starts
    width: str | None = None     # per-segment width override ("column", "full", "3.25in", ...)


@dataclass
class Measurement:
    """The typeset size of one segment, in TeX points (pt; 72.27 pt = 1 in).

    height_pt/depth_pt: the box as it sits in running text (any space above the
    content, e.g. a heading's or table's \\vspace, is included).
    top_height_pt/top_depth_pt: the same content starting a fresh column: TeX
    drops the discardable space at the top and applies \\topskip.
    """
    name: str
    index: int
    width_spec: str
    start_line: int = 1
    width_pt: float = 0.0
    height_pt: float = 0.0
    depth_pt: float = 0.0
    top_height_pt: float = 0.0
    top_depth_pt: float = 0.0
    measured: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    textheight_pt: float | None = None   # copied from the run's PageMetrics
    baselineskip_pt: float | None = None

    @property
    def ok(self) -> bool:
        return self.measured and not self.errors

    @property
    def total_pt(self) -> float:
        """Height + depth in running text."""
        return self.height_pt + self.depth_pt

    @property
    def top_total_pt(self) -> float:
        """Height + depth when the content starts a column."""
        return self.top_height_pt + self.top_depth_pt

    @property
    def column_fraction(self) -> float | None:
        """top_total_pt as a fraction of \\textheight (a full column)."""
        return self.top_total_pt / self.textheight_pt if self.textheight_pt else None

    @property
    def lines(self) -> float | None:
        """total_pt in body-text lines (\\baselineskip)."""
        return self.total_pt / self.baselineskip_pt if self.baselineskip_pt else None

    def fits(self, available_pt: float, at_top: bool = False) -> bool:
        """Whether the segment fits in `available_pt` of vertical space."""
        return (self.top_total_pt if at_top else self.total_pt) <= available_pt

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(ok=self.ok, total_pt=self.total_pt, top_total_pt=self.top_total_pt,
                 column_fraction=self.column_fraction, lines=self.lines)
        return d


@dataclass
class MeasureRun:
    """The result of one engine run: page metrics plus one Measurement per segment."""
    metrics: PageMetrics | None
    measurements: list[Measurement]
    errors: list[str] = field(default_factory=list)   # errors outside any segment
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and all(m.ok for m in self.measurements)

    def to_dict(self) -> dict:
        return {"ok": self.ok,
                "metrics": asdict(self.metrics) if self.metrics else None,
                "errors": self.errors,
                "measurements": [m.to_dict() for m in self.measurements]}


@dataclass
class SplitMeasurement:
    """Where a block lands when typeset in the real page flow after `used_pt` of a column.

    parts_pt[0] is the block's height in the starting column, parts_pt[1:] its height
    in each following column. One part: it fits. parts_pt[0] == 0 with more parts: it
    moved whole to the next column (an unbreakable table, or a break before it).
    """
    name: str
    index: int
    start_line: int = 1
    used_pt: float = 0.0          # requested space already used in the column
    start_pt: float = 0.0         # space actually used when the block starts (\topskip applies below 10pt)
    available_pt: float = 0.0     # space left in the column when the block starts
    parts_pt: list[float] = field(default_factory=list)
    end_pt: float = 0.0           # \pagetotal of the last column after the block: where the next block starts
    end_depth_pt: float = 0.0
    measured: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    textheight_pt: float | None = None

    @property
    def ok(self) -> bool:
        return self.measured and not self.errors

    @property
    def columns(self) -> int:
        """How many columns the block touches."""
        return len(self.parts_pt)

    @property
    def fits(self) -> bool:
        """The whole block stays in the starting column."""
        return self.measured and len(self.parts_pt) == 1

    @property
    def moved(self) -> bool:
        """Nothing of the block stays in the starting column."""
        return self.measured and len(self.parts_pt) > 1 and self.parts_pt[0] < 0.005

    @property
    def splits(self) -> bool:
        """Part of the block is in one column and the rest in a later one."""
        return self.measured and len([p for p in self.parts_pt if p >= 0.005]) > 1

    @property
    def overflow_pt(self) -> float:
        """Height of the block beyond the starting column (0 if it fits)."""
        return sum(self.parts_pt[1:])

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(ok=self.ok, columns=self.columns, fits=self.fits, moved=self.moved,
                 splits=self.splits, overflow_pt=self.overflow_pt)
        return d


@dataclass
class SplitRun:
    """The result of one split-simulation engine run."""
    metrics: PageMetrics | None
    measurements: list[SplitMeasurement]
    errors: list[str] = field(default_factory=list)
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and all(m.ok for m in self.measurements)

    def to_dict(self) -> dict:
        return {"ok": self.ok,
                "metrics": asdict(self.metrics) if self.metrics else None,
                "errors": self.errors,
                "measurements": [m.to_dict() for m in self.measurements]}


@dataclass
class _RawRun:
    metrics: PageMetrics | None
    records: list[list[str]]
    seg_errors: list[list[str]]
    seg_warnings: list[list[str]]
    run_errors: list[str]
    returncode: int


_MAXDIMEN = 16383.0   # \pagegoal of an empty page is \maxdimen (16383.99998pt)


def _pt(s: str) -> float:
    return float(s.removesuffix("pt"))


# ---------------------------------------------------------------------------
# Splitting a body file into segments
# ---------------------------------------------------------------------------
_MARKER_RE = re.compile(r"^\s*%+\s*measure:\s*(?P<name>.*?)\s*(?:\[width=(?P<width>[^\]]+)\])?\s*$")
_ENV_RE = re.compile(r"\\(begin|end)\s*\{([^}]*)\}")


def _strip_comment(line: str) -> str:
    """Drop a TeX comment (an unescaped %) from a line."""
    m = re.search(r"(?<!\\)(?:\\\\)*%", line)
    return line[:m.start()] if m else line


def _is_blank(text: str) -> bool:
    return not any(_strip_comment(l).strip() for l in text.splitlines())


def check_balance(content: str) -> str | None:
    """Return a description of unbalanced braces or environments, or None if balanced.

    A snippet with an unclosed group would swallow every snippet after it in the same
    engine run, so such snippets are rejected before TeX sees them.
    """
    depth, stack = 0, []
    for lineno, line in enumerate(content.splitlines(), 1):
        code = _strip_comment(line)
        if re.search(r"\\verb|\\begin\s*\{(?:verbatim|lstlisting|minted)\}", code):
            return None   # verbatim text can hold anything; let TeX judge
        plain = re.sub(r"\\.", "", code)
        for ch in plain:
            depth += {"{": 1, "}": -1}.get(ch, 0)
            if depth < 0:
                return f"unmatched '}}' on line {lineno}"
        for kind, env in _ENV_RE.findall(code):
            env = env.strip()
            if kind == "begin":
                stack.append(env)
            elif not stack or stack[-1] != env:
                return f"\\end{{{env}}} on line {lineno} without a matching \\begin" + \
                       (f" (open: {stack[-1]})" if stack else "")
            else:
                stack.pop()
    if depth > 0:
        return f"{depth} unclosed '{{'"
    if stack:
        return f"unclosed environment: {stack[-1]}"
    return None


def extract_body(text: str) -> str:
    """If `text` is a whole document, return what's between \\begin/\\end{document}."""
    b = re.search(r"\\begin\s*\{document\}", text)
    if not b:
        return text
    e = re.search(r"\\end\s*\{document\}", text[b.end():])
    return text[b.end(): b.end() + e.start()] if e else text[b.end():]


def split_segments(text: str, mode: str = "whole", name: str = "document") -> list[Segment]:
    """Split body text into segments.

    mode="whole":   the whole text is one segment.
    mode="markers": a line ``% measure: Name`` (optionally ``[width=full]``) starts a
                    segment; text before the first marker is its own segment if not blank.
    mode="blocks":  split at blank lines that are outside any environment or brace
                    group, i.e. one segment per top-level paragraph/environment.
    """
    lines = text.splitlines(keepends=True)
    if mode == "whole":
        return [Segment(name, text, 1)]

    if mode == "markers":
        segs: list[Segment] = []
        cur_name, cur_width, cur_start, buf = "(before first marker)", None, 1, []
        for i, line in enumerate(lines, 1):
            m = _MARKER_RE.match(line)
            if m:
                if buf and not _is_blank("".join(buf)):
                    segs.append(Segment(cur_name, "".join(buf), cur_start, cur_width))
                cur_name, cur_width = m.group("name") or f"segment {len(segs) + 1}", m.group("width")
                cur_start, buf = i + 1, []
            else:
                buf.append(line)
        if buf and not _is_blank("".join(buf)):
            segs.append(Segment(cur_name, "".join(buf), cur_start, cur_width))
        return segs

    if mode == "blocks":
        segs = []
        env_depth = brace_depth = 0
        buf: list[str] = []
        start = 1

        def flush():
            if buf and not _is_blank("".join(buf)):
                first = next(_strip_comment(l).strip() for l in buf if _strip_comment(l).strip())
                label = first if len(first) <= 48 else first[:45] + "..."
                segs.append(Segment(label, "".join(buf), start))

        for i, line in enumerate(lines, 1):
            code = _strip_comment(line)
            if not code.strip() and not line.strip().startswith("%") and env_depth == 0 and brace_depth <= 0:
                flush()
                buf, start = [], i + 1
                continue
            if not buf:
                start = i
            buf.append(line)
            for kind, env in _ENV_RE.findall(code):
                if env.strip() != "document":
                    env_depth += 1 if kind == "begin" else -1
            unescaped = re.sub(r"\\.", "", code)
            brace_depth += unescaped.count("{") - unescaped.count("}")
        flush()
        return segs

    raise ValueError(f"unknown split mode {mode!r} (use whole, markers or blocks)")


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------
_LINE_RE = re.compile(r"^l\.(\d+)")
_BOX_WARN_RE = re.compile(r"^((?:Overfull|Underfull) \\[hv]box .*?)(?: (?:in paragraph|in alignment) at lines (\d+)--(\d+)| detected at line (\d+)|has occurred while \\output is active)?$")


def _parse_log(log: str) -> tuple[list[tuple[str, int | None]], list[tuple[str, int | None]]]:
    """Return (errors, warnings) as (message, generated-file line or None)."""
    errors, warnings = [], []
    lines = log.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("! "):
            msg, lineno = line[2:].strip(), None
            for j in range(i + 1, min(i + 30, len(lines))):
                m = _LINE_RE.match(lines[j])
                if m:
                    lineno = int(m.group(1))
                    break
                if lines[j].startswith("! "):
                    break
            errors.append((msg, lineno))
        elif line.startswith(("Overfull \\", "Underfull \\")):
            m = _BOX_WARN_RE.match(line)
            if m and m.group(1).startswith("Overfull"):   # underfull boxes rarely matter for spill-over
                lineno = m.group(2) or m.group(4)
                warnings.append((line.strip(), int(lineno) if lineno else None))
        i += 1
    return errors, warnings


# ---------------------------------------------------------------------------
# The measurer
# ---------------------------------------------------------------------------
class LatexMeasurer:
    """Measures LaTeX snippets under one preamble.

    preamble:      preamble text, a path to a .tex preamble file, or None for the
                   bundled preamble.tex (the standard dndbook preamble).
    template_dir:  directory of the dndbook template if it isn't installed in TeX
                   (searched recursively). Defaults to $DNDBOOK_TEMPLATE if set.
    search_paths:  extra directories for \\input / \\includegraphics files.
    engine:        pdflatex, lualatex or xelatex.
    width:         default box width: "column" (\\columnwidth), "full" (\\textwidth)
                   or any TeX dimension ("3.25in", "0.5\\textwidth").
    keep_dir:      if set, the generated measure.tex/.log are written there and kept.
    """

    def __init__(self, preamble: str | os.PathLike | None = None, *,
                 template_dir: str | os.PathLike | None = None,
                 search_paths: Iterable[str | os.PathLike] = (),
                 engine: str = "pdflatex", width: str = "column",
                 timeout: float = 120, keep_dir: str | os.PathLike | None = None,
                 cache: bool = True):
        self.preamble = self._load_preamble(preamble)
        td = template_dir if template_dir is not None else os.environ.get("DNDBOOK_TEMPLATE")
        self.template_dir = Path(td) if td else None
        self.search_paths = [Path(p) for p in search_paths]
        self.engine = engine
        self.width = width
        self.timeout = timeout
        self.keep_dir = Path(keep_dir) if keep_dir else None
        self._cache: dict[tuple[str, str], Measurement] | None = {} if cache else None
        self.metrics: PageMetrics | None = None

    # -- public API ---------------------------------------------------------
    def measure(self, content: str, width: str | None = None, name: str = "snippet") -> Measurement:
        """Measure one snippet. Raises LatexMeasureError if it couldn't be measured;
        LaTeX errors that still let it typeset are in Measurement.errors."""
        m = self.measure_many([Segment(name, content, 1, width)]).measurements[0]
        if not m.measured:
            raise LatexMeasureError(f"snippet {name!r} was not measured: " + "; ".join(m.errors or ["no output"]))
        return m

    def measure_many(self, segments: Sequence[Segment | str | tuple[str, str]],
                     width: str | None = None) -> MeasureRun:
        """Measure many snippets in a single engine run.

        segments: Segment objects, plain strings, or (name, content) pairs.
        width:    default width for segments without their own.
        """
        segs = [self._as_segment(s, i) for i, s in enumerate(segments)]
        default_w = width or self.width
        results: list[Measurement | None] = [None] * len(segs)
        todo: list[int] = []
        for i, s in enumerate(segs):
            problem = check_balance(s.content)
            if problem:
                results[i] = Measurement(name=s.name, index=i, width_spec=s.width or default_w,
                                         start_line=s.start_line, errors=[f"not measured: {problem}"])
                continue
            key = (s.content, s.width or default_w)
            if self._cache is not None and key in self._cache and self.metrics is not None:
                cached = self._cache[key]
                results[i] = Measurement(**{**asdict(cached), "name": s.name, "index": i,
                                            "start_line": s.start_line})
            else:
                todo.append(i)
        run = MeasureRun(self.metrics, [], [])
        if todo:
            run = self._run([segs[i] for i in todo], default_w)
            for k, i in enumerate(todo):
                m = run.measurements[k]
                m.index = i
                results[i] = m
                if self._cache is not None and m.ok:
                    self._cache[(segs[i].content, segs[i].width or default_w)] = m
        for r in results:
            if r is not None and r.textheight_pt is None and self.metrics:
                r.textheight_pt, r.baselineskip_pt = self.metrics.textheight, self.metrics.baselineskip
        return MeasureRun(self.metrics, [r for r in results if r is not None], run.errors, run.returncode)

    def measure_split(self, content: str, used_pt: float = 0.0, used_depth_pt: float = 0.0,
                      name: str = "snippet") -> SplitMeasurement:
        """Typeset `content` in the real page flow, starting after `used_pt` of a fresh
        column, and report how much of it lands in each column. Raises
        LatexMeasureError if it couldn't be measured.

        used_depth_pt: how far the last line before the block hangs below its baseline
        (top_depth_pt of the preceding content's measurement). It sets the space TeX
        puts before the block; leaving it at 0 can move a break by one line.
        """
        r = self.measure_splits([Segment(name, content)], used_pt, used_depth_pt).measurements[0]
        if not r.measured:
            raise LatexMeasureError(f"snippet {name!r} was not measured: " + "; ".join(r.errors or ["no output"]))
        return r

    def measure_splits(self, segments: Sequence[Segment | str | tuple[str, str]],
                       used_pt: float | Sequence[float] = 0.0,
                       used_depth_pt: float | Sequence[float] = 0.0) -> SplitRun:
        """Split simulation for many snippets in one engine run.

        used_pt: space already used in the column before each snippet, in pt: one
        number for all, or one per snippet. used_depth_pt: the depth of the last line
        before each snippet (see measure_split). Each snippet starts on a fresh page,
        in the document's real column (Segment.width is ignored here).
        """
        segs = [self._as_segment(s, i) for i, s in enumerate(segments)]

        def per_seg(v, what):
            vals = [float(v)] * len(segs) if isinstance(v, (int, float)) else [float(u) for u in v]
            if len(vals) != len(segs):
                raise ValueError(f"{what} needs one value per segment")
            return vals
        used = per_seg(used_pt, "used_pt")
        depth = per_seg(used_depth_pt, "used_depth_pt")
        results: list[SplitMeasurement | None] = [None] * len(segs)
        todo = []
        for i, s in enumerate(segs):
            problem = check_balance(s.content)
            if problem:
                results[i] = SplitMeasurement(name=s.name, index=i, start_line=s.start_line, used_pt=used[i],
                                              errors=[f"not measured: {problem}"])
            else:
                todo.append(i)
        run = SplitRun(self.metrics, [], [])
        if todo:
            run = self._run_split([segs[i] for i in todo], [used[i] for i in todo], [depth[i] for i in todo])
            for k, i in enumerate(todo):
                run.measurements[k].index = i
                results[i] = run.measurements[k]
        for r in results:
            if r is not None and r.textheight_pt is None and self.metrics:
                r.textheight_pt = self.metrics.textheight
        return SplitRun(self.metrics, [r for r in results if r is not None], run.errors, run.returncode)

    def page_metrics(self) -> PageMetrics:
        """The document's layout dimensions (one engine run, then cached)."""
        if self.metrics is None:
            run = self._run([], self.width)
            if run.metrics is None:
                raise LatexMeasureError("could not read page metrics: " + "; ".join(run.errors))
        return self.metrics

    # -- internals ------------------------------------------------------------
    @staticmethod
    def _load_preamble(preamble) -> str:
        if preamble is None:
            text = DEFAULT_PREAMBLE_PATH.read_text(encoding="utf-8")
        elif isinstance(preamble, os.PathLike) or (isinstance(preamble, str) and "\n" not in preamble
                                                    and preamble.endswith(".tex") and Path(preamble).is_file()):
            text = Path(preamble).read_text(encoding="utf-8")
        else:
            text = str(preamble)
        # A whole document works as a preamble source: keep what's before \begin{document}.
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if re.search(r"\\begin\s*\{document\}", _strip_comment(line)):
                return "".join(lines[:i])
        return text

    @staticmethod
    def _as_segment(s, i: int) -> Segment:
        if isinstance(s, Segment):
            return s
        if isinstance(s, tuple):
            return Segment(s[0], s[1])
        return Segment(f"snippet {i + 1}", s)

    @staticmethod
    def _tex_width(spec: str) -> str:
        return WIDTH_ALIASES.get(spec.strip().lower(), spec)

    def _build(self, segs: list[Segment], wrap) -> tuple[str, list[tuple[int, int]]]:
        """Return the generated document and each segment's (first, last) line in it.

        wrap(i, segment) -> (line before the content, line after it).
        """
        pre = self.preamble.rstrip("\n")
        if any(re.search(r"\\begin\s*\{document\}", _strip_comment(l)) for l in pre.splitlines()):
            raise LatexMeasureError("the preamble must stop before \\begin{document}")
        out = [pre, TEX_HARNESS.strip("\n"), r"\begin{document}"]
        line = sum(p.count("\n") + 1 for p in out)   # lines used so far
        spans = []
        for i, s in enumerate(segs):
            before, after = wrap(i, s)
            body = s.content.rstrip("\n")
            out.append(before)
            first = line + 2
            out.append(body)
            line += 1 + body.count("\n") + 1
            out.append(after)
            line += 1
            spans.append((first, line))
        out.append(r"\end{document}")
        return "\n".join(out) + "\n", spans

    def _env(self) -> dict:
        env = dict(os.environ)
        paths = [str(p).replace("\\", "/") for p in self.search_paths]
        if self.template_dir:
            paths.append(str(self.template_dir).replace("\\", "/").rstrip("/") + "//")
        existing = env.get("TEXINPUTS", "")
        env["TEXINPUTS"] = os.pathsep.join(["."] + paths + ([existing] if existing else []) + [""])
        # Keep log lines unwrapped so messages and line numbers parse cleanly.
        env.setdefault("max_print_line", "10000")
        env.setdefault("error_line", "254")
        env.setdefault("half_error_line", "238")
        return env

    def _execute(self, segs: list[Segment], wrap) -> _RawRun:
        """Build the document, run the engine once, and sort the output by segment."""
        exe = shutil.which(self.engine)
        if not exe:
            raise LatexMeasureError(f"{self.engine} not found on PATH")
        doc, spans = self._build(segs, wrap)
        if self.keep_dir:
            self.keep_dir.mkdir(parents=True, exist_ok=True)
            return self._execute_in(self.keep_dir, exe, doc, segs, spans)
        with tempfile.TemporaryDirectory(prefix="latex_measure_") as tmp:
            return self._execute_in(Path(tmp), exe, doc, segs, spans)

    def _execute_in(self, wd: Path, exe: str, doc: str, segs, spans) -> _RawRun:
        (wd / "measure.tex").write_text(doc, encoding="utf-8")
        for stale in ("measure.mtx", "measure.log"):
            (wd / stale).unlink(missing_ok=True)
        cmd = [exe, "-interaction=nonstopmode", *ENGINE_DRAFT_FLAGS.get(self.engine, []), "measure.tex"]
        try:
            proc = subprocess.run(cmd, cwd=wd, env=self._env(), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=self.timeout, text=True, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise LatexMeasureError(f"{self.engine} timed out after {self.timeout}s")
        log = (wd / "measure.log").read_text(encoding="utf-8", errors="replace") if (wd / "measure.log").exists() else proc.stdout
        mtx = (wd / "measure.mtx").read_text(encoding="utf-8", errors="replace") if (wd / "measure.mtx").exists() else ""

        raw = _RawRun(metrics=None, records=[], seg_errors=[[] for _ in segs],
                      seg_warnings=[[] for _ in segs], run_errors=[], returncode=proc.returncode)
        for line in mtx.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "METRICS":
                vals = {k: float(v.removesuffix("pt")) for k, v in (p.split("=", 1) for p in parts[1:])}
                raw.metrics = PageMetrics(**vals)
            else:
                raw.records.append(parts)
        if raw.metrics is None:
            tail = "\n".join(log.splitlines()[-25:])
            raise LatexMeasureError(f"{self.engine} stopped before \\begin{{document}} "
                                    "(preamble error or missing dndbook class?)", tail)
        self.metrics = raw.metrics

        def owner(lineno):
            if lineno is None:
                return None
            for k, (a, b) in enumerate(spans):
                if a - 1 <= lineno <= b:          # include the lines before/after the content
                    return k
            return None

        def to_src(k, msg, lineno):
            # translate a generated-file line into the source file's numbering
            src = segs[k].start_line + (lineno - spans[k][0])
            return f"{msg} (line {max(src, segs[k].start_line)})"

        errors, warnings = _parse_log(log)
        for msg, lineno in errors:
            k = owner(lineno)
            if k is None:
                raw.run_errors.append(msg if lineno is None else f"{msg} (generated line {lineno})")
            else:
                raw.seg_errors[k].append(to_src(k, msg, lineno))
        for msg, lineno in warnings:
            k = owner(lineno)
            if k is not None:
                raw.seg_warnings[k].append(to_src(k, msg, lineno))
        return raw

    def _run(self, segs: list[Segment], default_w: str) -> MeasureRun:
        """Box measurement of each segment (\\MTbegin ... \\MTend)."""
        raw = self._execute(segs, lambda i, s: (f"\\MTbegin{{{i}}}{{{self._tex_width(s.width or default_w)}}}%",
                                                "\\MTend"))
        mt = raw.metrics
        vals = {int(p[1]): [_pt(x) for x in p[2:]] for p in raw.records if p[0] == "SEG" and len(p) == 7}
        measurements = []
        for k, s in enumerate(segs):
            m = Measurement(name=s.name, index=k, width_spec=s.width or default_w, start_line=s.start_line,
                            textheight_pt=mt.textheight, baselineskip_pt=mt.baselineskip,
                            errors=raw.seg_errors[k], warnings=raw.seg_warnings[k])
            if k in vals:
                m.width_pt, m.height_pt, m.depth_pt, m.top_height_pt, m.top_depth_pt = vals[k]
                m.measured = True
            elif not m.errors:
                m.errors.append("no measurement written (an earlier error may have swallowed \\MTend)")
            measurements.append(m)
        return MeasureRun(mt, measurements, raw.run_errors, raw.returncode)

    def _run_split(self, segs: list[Segment], used: list[float], depth: list[float]) -> SplitRun:
        """Real-flow typesetting of each segment after `used` pt of a fresh column."""
        raw = self._execute(segs, lambda i, s: (f"\\MTsplitbegin{{{i}}}{{{used[i]:.5f}pt}}{{{depth[i]:.5f}pt}}%",
                                                "\\MTsplitend"))
        mt = raw.metrics
        starts, cols, ends = {}, {}, {}
        for p in raw.records:
            if p[0] == "START" and len(p) == 4:
                starts[int(p[1])] = (_pt(p[2]), _pt(p[3]))
            elif p[0] == "COL" and len(p) == 3:
                cols.setdefault(int(p[1]), []).append(_pt(p[2]))
            elif p[0] == "END" and len(p) == 5:
                ends[int(p[1])] = (_pt(p[2]), _pt(p[3]), _pt(p[4]))
        results = []
        for k, s in enumerate(segs):
            r = SplitMeasurement(name=s.name, index=k, start_line=s.start_line, used_pt=used[k],
                                 textheight_pt=mt.textheight,
                                 errors=raw.seg_errors[k], warnings=raw.seg_warnings[k])
            if k in starts and k in ends:
                start_total, start_goal = starts[k]
                # an empty page has \pagegoal = \maxdimen and nothing on it yet
                r.start_pt = 0.0 if start_goal >= _MAXDIMEN else start_total
                r.available_pt = mt.textheight - r.start_pt if start_goal >= _MAXDIMEN else start_goal - start_total
                end_total, end_depth, end_goal = ends[k]
                end_empty = end_goal >= _MAXDIMEN
                columns = cols.get(k, [])
                if columns:
                    parts = [columns[0] - r.start_pt] + columns[1:]
                    if not end_empty:
                        parts.append(end_total)
                else:
                    parts = [end_total - r.start_pt]
                r.parts_pt = [max(0.0, p) for p in parts]
                r.end_pt = 0.0 if end_empty else end_total
                r.end_depth_pt = 0.0 if end_empty else end_depth
                r.measured = True
            elif not r.errors:
                r.errors.append("no measurement written (an earlier error may have swallowed \\MTsplitend)")
            results.append(r)
        return SplitRun(mt, results, raw.run_errors, raw.returncode)


def measure_latex(content: str, width: str = "column", **kwargs) -> Measurement:
    """One-shot convenience: LatexMeasurer(**kwargs).measure(content, width)."""
    return LatexMeasurer(**kwargs).measure(content, width)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _fmt_table(run: MeasureRun) -> str:
    out = []
    mt = run.metrics
    if mt:
        out.append(f"Page: column {mt.columnwidth:.2f}pt x {mt.textheight:.2f}pt "
                   f"(text width {mt.textwidth:.2f}pt, column sep {mt.columnsep:.2f}pt), "
                   f"baselineskip {mt.baselineskip:.2f}pt, topskip {mt.topskip:.2f}pt")
        out.append("")
    hdr = f"{'#':>3} {'line':>5}  {'name':<34} {'width':>8} {'height':>8} {'depth':>6} {'total':>8} {'@top':>8} {'%col':>6}  status"
    out.append(hdr)
    out.append("-" * len(hdr))
    for m in run.measurements:
        name = m.name if len(m.name) <= 34 else m.name[:31] + "..."
        status = "ok" if m.ok else ("ERROR" if m.errors else "?")
        if m.ok and m.warnings:
            status = "overfull"
        if m.measured:
            pct = f"{100 * m.column_fraction:5.1f}%" if m.column_fraction is not None else ""
            out.append(f"{m.index + 1:>3} {m.start_line:>5}  {name:<34} {m.width_pt:8.2f} {m.height_pt:8.2f} "
                       f"{m.depth_pt:6.2f} {m.total_pt:8.2f} {m.top_total_pt:8.2f} {pct:>6}  {status}")
        else:
            out.append(f"{m.index + 1:>3} {m.start_line:>5}  {name:<34} {'-':>8} {'-':>8} {'-':>6} {'-':>8} {'-':>8} {'':>6}  {status}")
        for e in m.errors:
            out.append(f"{'':11}error: {e}")
        for w in m.warnings:
            out.append(f"{'':11}warning: {w}")
    for e in run.errors:
        out.append(f"error (outside segments): {e}")
    if len(run.measurements) > 1:
        tot = sum(m.total_pt for m in run.measurements if m.measured)
        out.append("")
        out.append(f"Sum of totals: {tot:.2f}pt" + (f" ({100 * tot / mt.textheight:.1f}% of a column; "
                                                     "separate boxes, so inter-block glue is not included)" if mt else ""))
    out.append("All dimensions in TeX points (72.27pt = 1in).")
    return "\n".join(out)


def _fmt_split_table(run: SplitRun) -> str:
    out = []
    mt = run.metrics
    if mt:
        out.append(f"Page: column {mt.columnwidth:.2f}pt x {mt.textheight:.2f}pt; each block typeset in the real "
                   f"page flow after the used space")
        out.append("")
    hdr = f"{'#':>3} {'line':>5}  {'name':<34} {'used':>7} {'left':>7}  {'height per column':<24} {'end':>7}  result"
    out.append(hdr)
    out.append("-" * len(hdr))
    for r in run.measurements:
        name = r.name if len(r.name) <= 34 else r.name[:31] + "..."
        if r.measured:
            parts = " + ".join(f"{p:.2f}" for p in r.parts_pt)
            if r.fits:
                result = "fits"
            elif r.moved and not r.splits:
                result = "moves to the next column"
            elif r.moved:
                result = f"moves, then splits over {r.columns - 1} columns"
            else:
                result = f"splits over {r.columns} columns"
            if r.errors:
                result += " (ERROR)"
            out.append(f"{r.index + 1:>3} {r.start_line:>5}  {name:<34} {r.start_pt:7.2f} {r.available_pt:7.2f}  "
                       f"{parts:<24} {r.end_pt:7.2f}  {result}")
        else:
            out.append(f"{r.index + 1:>3} {r.start_line:>5}  {name:<34} {r.used_pt:7.2f} {'-':>7}  {'-':<24} {'-':>7}  ERROR")
        for e in r.errors:
            out.append(f"{'':11}error: {e}")
        for w in r.warnings:
            out.append(f"{'':11}warning: {w}")
    for e in run.errors:
        out.append(f"error (outside segments): {e}")
    out.append("")
    out.append("used: space taken before the block; left: space left in the column; end: where the next "
               "block would start in the last column.")
    out.append("All dimensions in TeX points (72.27pt = 1in).")
    return "\n".join(out)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="latex-measure",
        description="Measure the typeset size of LaTeX body text under a document's preamble. "
                    "The input file must contain body text only (no \\documentclass, no preamble, "
                    "no \\begin{document}).")
    p.add_argument("file", nargs="?", help="body-only .tex file to measure ('-' reads stdin); "
                                           "not needed with --metrics")
    p.add_argument("--split", choices=["whole", "markers", "blocks"], default="whole",
                   help="whole: one measurement (default); markers: split at '%% measure: Name' lines; "
                        "blocks: one measurement per top-level paragraph/environment")
    p.add_argument("--width", default="column",
                   help="box width: column (default), full, or a TeX dimension such as 3.25in or 0.5\\textwidth")
    p.add_argument("--used", type=float, metavar="PT",
                   help="split simulation: typeset each block in the real page flow after PT points of a "
                        "column are already used, and report how much lands in each column (0 = top of a column)")
    p.add_argument("--used-depth", type=float, default=0.0, metavar="PT",
                   help="with --used: depth of the last line before the block (the top_depth_pt of the "
                        "preceding content); sets the space before the block exactly (default 0)")
    p.add_argument("--preamble", help="preamble file or complete document (default: the bundled preamble.tex)")
    p.add_argument("--template-dir", help="dndbook template directory if the class isn't installed "
                                          "(default: $DNDBOOK_TEMPLATE)")
    p.add_argument("--texinputs", action="append", default=[], metavar="DIR",
                   help="extra directory to search for \\input and image files (repeatable; "
                        "the input file's own directory is always searched)")
    p.add_argument("--engine", default="pdflatex", choices=sorted(ENGINE_DRAFT_FLAGS))
    p.add_argument("--json", action="store_true", help="print results as JSON")
    p.add_argument("--metrics", action="store_true", help="print only the page metrics")
    p.add_argument("--keep", metavar="DIR", help="keep the generated measure.tex and its log in DIR")
    p.add_argument("--timeout", type=float, default=120, help="engine timeout in seconds (default 120)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.file is None and not args.metrics:
        parser.error("the following arguments are required: file")
    if args.file is None:
        text, src_dir = "", Path.cwd()
    elif args.file == "-":
        text, src_dir = sys.stdin.read(), Path.cwd()
    else:
        path = Path(args.file)
        if not path.is_file():
            print(f"error: {path} not found", file=sys.stderr)
            return 2
        text, src_dir = path.read_text(encoding="utf-8"), path.resolve().parent
    if re.search(r"\\begin\s*\{document\}", text):
        print("warning: input contains \\begin{document}; measuring only the body "
              "(the file's own preamble is ignored, use --preamble for that)", file=sys.stderr)
        # keep source line numbers: blank out everything up to \begin{document}
        b = re.search(r"\\begin\s*\{document\}", text)
        text = "\n" * text[:b.end()].count("\n") + extract_body(text)

    measurer = LatexMeasurer(args.preamble, template_dir=args.template_dir,
                             search_paths=[src_dir, *args.texinputs], engine=args.engine,
                             width=args.width, timeout=args.timeout, keep_dir=args.keep)
    try:
        if args.metrics:
            mt = measurer.page_metrics()
            print(json.dumps(asdict(mt), indent=2) if args.json
                  else "\n".join(f"{k:>16}: {v:.4f}pt" for k, v in asdict(mt).items()))
            return 0
        segs = split_segments(text, args.split, name=Path(args.file).stem if args.file != "-" else "stdin")
        if not segs:
            print("error: nothing to measure", file=sys.stderr)
            return 2
        if args.used is not None:
            if args.width != "column":
                print("warning: --width is ignored with --used (blocks go in the document's real column)",
                      file=sys.stderr)
            run = measurer.measure_splits(segs, args.used, args.used_depth)
        else:
            run = measurer.measure_many(segs)
    except LatexMeasureError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(run.to_dict(), indent=2))
    else:
        print(_fmt_split_table(run) if isinstance(run, SplitRun) else _fmt_table(run))
    return 0 if run.ok else 1


if __name__ == "__main__":
    sys.exit(main())
