# latex_measure

Measures the exact typeset size of a piece of LaTeX (a paragraph, a stat block, a table, a read-aloud box) under the real document style, so layout code can tell whether it will fit in the current column or page before writing the final document.

It's built for the [dndbook template](https://github.com/rpgtex/DND-5e-LaTeX-Template) and the Enigma Narrative house preamble, but any preamble works. The sizes come from a real TeX run (pdflatex by default), so they match the final document exactly as long as the preamble is the same.

How it works, with flowcharts: [DESIGN.md](DESIGN.md).

## Requirements

- Python 3.10 or newer (standard library only)
- A TeX distribution with `pdflatex` (or `lualatex`/`xelatex`) on `PATH`
- The dndbook template, either installed in your TeX tree or cloned somewhere and passed with `--template-dir` (or the `DNDBOOK_TEMPLATE` environment variable)

## Installing

From this folder:

```bash
pip install .
```

For working on the tool itself, install it editable so changes take effect without reinstalling:

```bash
pip install -e .
```

To use it from another project, such as the XML-to-LaTeX compiler, point pip at this folder:

```bash
pip install E:/Repos/LaTeX/measuring_tool
```

With uv, add it to that project as a path dependency:

```bash
uv add --editable E:/Repos/LaTeX/measuring_tool
```

Installing gives you the `latex-measure` command and the `latex_measure` Python package. There are no Python dependencies. Without installing, `python -m latex_measure` from this folder does the same as `latex-measure`.

## Files

| File | What it is |
|---|---|
| `latex_measure/core.py` | The measuring code and the command-line tool |
| `latex_measure/__init__.py` | The public API (`from latex_measure import LatexMeasurer`) |
| `latex_measure/__main__.py` | Makes `python -m latex_measure` work |
| `latex_measure/preamble.tex` | The default preamble: the standard Enigma Narrative dndbook preamble. It's installed with the package |
| `pyproject.toml` | Package metadata and the `latex-measure` command |
| `DESIGN.md` | How it works |

## Input files: body text only, no preamble

> **The `.tex` file you measure must contain body text only.** Leave out `\documentclass`, every `\usepackage`, all other preamble definitions, and `\begin{document}` / `\end{document}`. The tool supplies the preamble (the bundled `latex_measure/preamble.tex`, or your own through `--preamble`) and wraps each snippet itself.

Good input:

```latex
\section{The Brewer's Cache}
Durgan's brewhouse smells of sour mash and dust.

\begin{DndReadAloud}
  Rows of oak casks fill the brewhouse.
\end{DndReadAloud}
```

If a file contains `\begin{document}` anyway, the tool prints a warning, ignores everything before it (the file's own preamble isn't used), and measures only the body.

Custom macros the body uses (`\DndNoun`, `\MonAttack`, `DndDialogue` ...) have to be defined in the preamble you measure with, the same as in the real document.

## Command line

```bash
latex-measure body.tex --template-dir path/to/DND-5e-LaTeX-Template
```

Options:

| Option | Meaning |
|---|---|
| `--split whole` | Measure the whole file as one block (default) |
| `--split markers` | One measurement per `% measure: Name` marker line (see below) |
| `--split blocks` | One measurement per top-level paragraph or environment (split at blank lines that aren't inside an environment or brace group) |
| `--width W` | Box width: `column` (default, `\columnwidth`), `full` (`\textwidth`), or any TeX dimension such as `3.25in` or `0.5\textwidth` |
| `--used PT` | Split simulation instead of a box measurement: typeset each block in the real page flow after `PT` points of a column are already used, and report how much lands in each column ([below](#where-does-it-split)) |
| `--used-depth PT` | With `--used`: how far the last line before the block hangs below its baseline (its `top_depth_pt`). Default 0 |
| `--preamble FILE` | Use this preamble instead of the bundled `latex_measure/preamble.tex`. A complete document works too: everything from `\begin{document}` on is ignored, so a compiled `.tex` can supply its own preamble |
| `--template-dir DIR` | Where the dndbook template is, if it isn't installed (searched recursively). Default: `$DNDBOOK_TEMPLATE` |
| `--texinputs DIR` | Another directory to search for `\input` and image files (repeatable). The input file's own directory is always searched |
| `--engine` | `pdflatex` (default), `lualatex` or `xelatex`. Use the one that builds the real document |
| `--json` | Print the results as JSON |
| `--metrics` | Print only the page layout dimensions (column width, text height, ...) |
| `--keep DIR` | Keep the generated `measure.tex` and its log in `DIR` for debugging |
| `--timeout S` | Engine timeout in seconds (default 120) |

`-` as the file name reads stdin.

Exit status: `0` all measured without errors, `1` some snippet had a LaTeX error (it may still have a measurement), `2` the tool couldn't run (no engine, preamble error, file not found).

### Marker lines

With `--split markers`, a comment line `% measure: Name` starts a new named block. Add `[width=...]` to give that block its own width:

```latex
% measure: Garrick's dialogue
\begin{DndDialogue}
  \DndSays{Garrick}{Six dogs. Six good dogs, gone in one night.}
\end{DndDialogue}

% measure: Full-width map [width=full]
\begin{figure*}[!t]
  \includegraphics[width=\linewidth]{map.png}
\end{figure*}
```

### Example output

```
Page: column 241.02pt x 703.91pt (text width 505.89pt, column sep 23.85pt), baselineskip 12.00pt, topskip 10.00pt

  #  line  name                                  width   height  depth    total     @top   %col  status
-------------------------------------------------------------------------------------------------------
  1    97  \chapter{Introduction}               505.89    42.00   0.00    42.00    42.00   6.0%  ok
  2   101  \DndDropCapLine{T}{his is a tes...   241.02    90.99   2.25    93.24    96.25  13.7%  ok
  3   167  \begin{DndSidebar}[float=!b]{No...   241.02   110.58   0.00   110.58   110.58  15.7%  ok
  4   673  \begin{DndMonster}{Giant Hyena}      241.02   317.68   0.00   317.68   305.68  43.4%  ok
```

## What the numbers mean

All dimensions are TeX points (`pt`, 72.27 per inch). PDF points are `bp` (72 per inch).

| Field | Meaning |
|---|---|
| `width_pt` | Width of the box. A chapter head or a `figure*` reports the full text width even in a column-width box |
| `height_pt`, `depth_pt` | The block as it sits in running text. Space the block puts above itself (a `\section`'s or `DndTable`'s `\vspace`) is included. Depth is how far the last line hangs below its baseline |
| `total_pt` | `height_pt + depth_pt` |
| `top_height_pt`, `top_depth_pt`, `top_total_pt` (`@top`) | The block as the first thing in a column: TeX drops the space at the top and inserts `\topskip` instead, exactly as it does at the top of a real column |
| `column_fraction` (`%col`) | `top_total_pt / \textheight`: how much of an empty column the block uses |
| `lines` | `total_pt` in body-text lines (`\baselineskip`) |
| `errors`, `warnings` | LaTeX errors in the snippet, and overfull boxes (text sticking out past the width) |

The `@top` numbers match TeX's own `\pagetotal` for the same content at the top of a real dndbook column to the hundredth of a point.

A full column holds content whose `top_height_pt` is at most `\textheight` (703.91pt here) and whose last line hangs no more than `\maxdepth` (5pt) below.

## Where does it split?

A box measurement says how tall a block is. When it doesn't fit, you also need to know what TeX does with it: a stat block (`DndMonster`) splits and continues in the next column, a table moves there whole, a paragraph breaks between lines. The split simulation answers that by typesetting the block in the real page flow, after a given amount of the column is already used, and recording how much of it lands in each column.

```bash
latex-measure dragon.tex --used 400 --used-depth 2.47
```

```
  #  line  name                                  used    left  height per column            end  result
-------------------------------------------------------------------------------------------------------
  1     1  dragon                              400.00  303.91  299.38 + 452.74           452.74  splits over 2 columns
```

The Ancient Gold Dragon, starting 400pt down a column: 299.38pt of it stays in that column and 452.74pt continues in the next, where the next block would start 452.74pt down.

| Field | Meaning |
|---|---|
| `used_pt` | The `--used` you asked for |
| `start_pt` (`used`) | Space actually used when the block starts. Below `\topskip` (10pt) TeX rounds it up to `\topskip` |
| `available_pt` (`left`) | Space left in the column when the block starts |
| `parts_pt` (`height per column`) | The block's height in each column it touches. `parts_pt[0]` is the starting column |
| `end_pt` (`end`) | How full the last column is after the block, including the space the block leaves after itself: where the next block would start. 0 means the next block starts a new column |
| `fits` | The whole block stays in the starting column |
| `moved` | Nothing of it stays in the starting column (`parts_pt[0]` is 0): a table that moved whole, or a stat block that broke before its first line |
| `splits` | It's in two or more columns, with something in each |
| `overflow_pt` | Total height beyond the starting column |

Pass the depth of the last line before the block (`--used-depth`, or `used_depth_pt=` in Python). It's the `top_depth_pt` of the preceding content's box measurement. TeX uses that depth to set the space between that line and the block. Without it, the space can be off by up to about 2.5pt, which occasionally moves a break by one line. With it, the simulation broke the Ancient Gold Dragon at exactly the same line as real preceding text in all 10 test cases (1 to 7 paragraphs, ending on full and short lines).

How to read it for layout:

- A broken stat block's first piece runs to about the bottom of the column, so `parts_pt[0]` is within a few points of `available_pt`. It can be a little more when TeX squeezed flexible space to fit (heights are reported at natural size).
- Each block starts on a fresh page, in the document's real column; `--width` doesn't apply.
- Floats (`figure`, `DndSidebar[float=...]`) go to TeX's float areas, not the text flow, so they show as `fits` with 0pt here. Use the box measurement for floats and add `\textfloatsep`.
- Page-level commands start a new page themselves (`\chapter`, `\clearpage`, `\frontmatter`), so on their own they report nothing useful. Put a chapter head in the same segment as what follows it; the content then flows into the shorter columns under the head, as in the document.

## Using it from Python

```python
from latex_measure import LatexMeasurer, Segment

m = LatexMeasurer(template_dir="path/to/DND-5e-LaTeX-Template")   # bundled preamble

# One snippet
r = m.measure(r"""
\begin{DndReadAloud}
  Hillsfort is quiet in the way of a place that has buried too many people.
\end{DndReadAloud}
""")
print(r.total_pt, r.top_total_pt, r.column_fraction, r.ok, r.errors)

# Many snippets in ONE engine run (much faster than one run each)
run = m.measure_many([
    ("hyena", hyena_statblock_tex),
    ("cache table", cache_table_tex),
    Segment("map", map_figure_tex, width="full"),
])
for x in run.measurements:
    print(x.name, x.top_total_pt)

# Page layout dimensions of the document class
metrics = m.page_metrics()           # .columnwidth, .textheight, .baselineskip, .textfloatsep, ...

# Does it fit in what's left of the column?
remaining = metrics.textheight - used_so_far
if not r.fits(remaining):
    ...  # start a new column, or move the block

# If not, where does it split? (used_depth_pt: the preceding content's top_depth_pt)
s = m.measure_split(dragon_statblock_tex, used_pt=used_so_far, used_depth_pt=prev.top_depth_pt)
print(s.fits, s.moved, s.splits, s.parts_pt, s.end_pt)
# many blocks, one engine run: m.measure_splits(segments, used_pt=[...], used_depth_pt=[...])
```

`LatexMeasurer` arguments: `preamble` (text, a path, or `None` for the bundled one), `template_dir`, `search_paths`, `engine`, `width`, `timeout`, `keep_dir`, `cache` (default on: an unchanged snippet at the same width isn't typeset twice).

`measure()` raises `LatexMeasureError` when a snippet produced no measurement at all. A snippet that typeset despite LaTeX errors still returns its size, with the errors in `.errors`; check `.ok`.

Split simulation: `measure_split(content, used_pt, used_depth_pt)` returns a `SplitMeasurement`; `measure_splits(segments, used_pt, used_depth_pt)` does many in one run (each `used` value can be one number for all or a list).

Other helpers: `split_segments(text, mode)` (the same splitting as `--split`), `measure_latex(content, width, **kwargs)` (one-shot), `check_balance(content)`.

## Integrating with a layout pass

- **Measure blocks together when you need their combined height.** Each segment is its own box, so the sum of separately measured blocks leaves out the spacing TeX puts between them (interline glue, `\parskip`). To ask "do these five blocks fit in the column?", measure them as one segment.
- **Floats are measured as their box only.** A `figure`, `table` or `DndSidebar[float=...]` is typeset in place so it can be measured. When TeX actually floats it to the top or bottom of a column it also adds `\textfloatsep` (`\dbltextfloatsep` for `figure*`) between the float and the text. Both are in `page_metrics()`.
- **Stat blocks break across columns; tables don't.** `DndMonster` is a breakable tcolorbox: when it doesn't fit, TeX splits it and continues it at the top of the next column. The box measurement is the unbroken height, which tells you *whether* it will split (`top_total_pt` against the space left); the [split simulation](#where-does-it-split) tells you where. `DndTable` can't break, so a table that doesn't fit moves to the next column whole. A stat block taller than a full column always splits; the tool still reports its unbroken height, which is exact up to `\textheight` (above that, tcolorbox's own sizing of oversized boxes can differ from the measurement by a few points).
- **Chapter heads span both columns.** `\chapter` is measured at `\textwidth` (the `width` field says so). It takes its height off both columns of the page.
- **Use the same engine and preamble as the real build.** A different font setup or a missing package changes the numbers. The bundled `latex_measure/preamble.tex` is a copy of the compiler's `PREAMBLE` (in `encompile/supplement.py`); when that changes, update the copy too, or pass the real preamble with `--preamble` / `preamble=`. One small block makes a difference: the compiler's ability-score patch, which puts each modifier under its score, adds a line (11pt) to the Werewolf and the Giant Hyena.
- **Flexible space is measured at its natural size.** `\raggedbottom` (in the house preamble) stops TeX stretching the space between blocks to fill a column. TeX can still *shrink* flexible space (a `DndTable` has `9pt plus 3pt minus 3pt` above and below) to squeeze a block in, so the natural numbers are slightly conservative: a block that measures as fitting will fit, and one a few points over sometimes fits too.
- Page-level commands in a snippet (`\clearpage`, `\newpage`, `\balance`) do nothing during measurement.

## Troubleshooting

- **`dndbook.cls not found`**: pass `--template-dir` or set `DNDBOOK_TEMPLATE` to the cloned template directory.
- **Errors with line numbers**: line numbers are lines in your input file. Use `--keep DIR` to see the generated document and the full log.
- **`not measured: 1 unclosed '{'`**: the snippet's braces or `\begin`/`\end` don't balance. It's rejected before TeX runs, because an open group would swallow every snippet after it.
- **`I do not know the key '/tcb/\NoValue'`** on `DndReadAloud`, `DndComment` and `DndSidebar` without options, and **`Extra alignment tab has been changed to \cr`** on `DndTable`: in testing, the current dndbook template (October 2026) raised both with TeX Live 2026 even in a plain document with no measuring involved. The boxes still typeset and measure, but check the numbers against your real build if you see them.
- **An image `not found`**: the image is looked for in the input file's directory and in each `--texinputs` directory. A missing image is measured as a draft placeholder, which is the wrong size.
