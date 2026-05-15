"""Visual banner for the watchmen CLI — comic-book aesthetic.

Bright-yellow wordmark on whatever the terminal background is, with dripping
yellow paint streaks underneath and a tagline. Riff on the Watchmen film
poster's palette without trying to render the smiley in ASCII (it never reads
right at this resolution).
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text


# Block-style "WATCHMEN" wordmark, ~74 chars wide. Mimics the poster's bold
# yellow sans-serif feel.
_WORDMARK = r"""
██╗    ██╗ █████╗ ████████╗ ██████╗██╗  ██╗███╗   ███╗███████╗███╗   ██╗
██║    ██║██╔══██╗╚══██╔══╝██╔════╝██║  ██║████╗ ████║██╔════╝████╗  ██║
██║ █╗ ██║███████║   ██║   ██║     ███████║██╔████╔██║█████╗  ██╔██╗ ██║
██║███╗██║██╔══██║   ██║   ██║     ██╔══██║██║╚██╔╝██║██╔══╝  ██║╚██╗██║
╚███╔███╔╝██║  ██║   ██║   ╚██████╗██║  ██║██║ ╚═╝ ██║███████╗██║ ╚████║
 ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝╚═╝     ╚═╝╚══════╝╚═╝  ╚═══╝
""".strip("\n")


# Dripping yellow paint streaks under the wordmark — riff on the poster's
# bottom-half dripping effect. Variable lengths for irregularity.
_DRIPS = r"""
 │  │   ││  │ ││  │  │   ││ │  ││   │  │  │ ││  │   │ ││  │  │
 │  │   ││    │   │  │      │  ││   │     │ │   │      │  │
    │       │        │      │      │             │
            │               │             │
            │
""".rstrip()


_TAGLINE = '"Who watches the watchmen?"'


def render(console: Console) -> None:
    console.print()
    for line in _WORDMARK.splitlines():
        console.print(Text(line, style="bold yellow"), justify="center")
    for line in _DRIPS.splitlines():
        console.print(Text(line, style="yellow"), justify="center")
    console.print()
    console.print(Text(_TAGLINE, style="dim italic yellow"), justify="center")
    console.print()
