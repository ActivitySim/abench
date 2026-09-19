"""Keyboard-driven choices shared by experiment discovery and typed inputs."""

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension


def choose(title, labels, default_index=0):
    """Return an index on Enter; preserve defaults and restore the terminal on exit."""
    if not labels or not 0 <= default_index < len(labels):
        raise ValueError("choice menu requires options and a valid initial selection")
    selected = default_index
    bindings = KeyBindings()

    @bindings.add("up")
    def up(event):
        nonlocal selected
        selected = (selected - 1) % len(labels)

    @bindings.add("down")
    def down(event):
        nonlocal selected
        selected = (selected + 1) % len(labels)

    @bindings.add("enter")
    def accept(event):
        event.app.exit(result=selected)

    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("c-d")
    def eof(event):
        event.app.exit(exception=EOFError())

    def render():
        # Plain formatted-text fragments avoid interpreting labels as markup.
        # Reverse video + a marker remain visible without color support.
        fragments = []
        for index, label in enumerate(labels):
            label = " ".join(str(label).splitlines())
            fragments.append(
                (
                    "bold reverse" if index == selected else "",
                    f"{'>' if index == selected else ' '} {label}",
                )
            )
            if index < len(labels) - 1:
                fragments.append(("", "\n"))
        return fragments

    control = FormattedTextControl(
        render,
        focusable=True,
        show_cursor=False,
        get_cursor_position=lambda: Point(x=0, y=selected),
    )
    app = Application(
        layout=Layout(
            HSplit(
                [
                    Window(
                        FormattedTextControl(title + " (↑/↓ to move, Enter to choose)"),
                        height=1,
                    ),
                    # A bounded viewport scrolls to the selection for long lists.
                    Window(control, height=Dimension(max=10), wrap_lines=False),
                ]
            ),
            focused_element=control,
        ),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,
    )
    try:
        result = app.run()
    except EOFError as error:
        raise ValueError(
            "Input ended while choosing; experiment not started"
        ) from error
    print(f"{title}: {labels[result]}", flush=True)
    return result
