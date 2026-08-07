# strip layout

A scrollable-strip layout for kitty. Every other layout divides the available
space among the windows, so the more you open the narrower each one gets. This
one inverts that: each column has an **absolute** width with a floor of
`min_columns` cells, and once the columns no longer fit you **scroll the strip
instead of shrinking them**.

The model comes from scrollable-tiling window managers (niri, PaperWM), where
the first class unit is the column rather than the tile. No terminal seems to
do it — the idea lives in window managers and in note taking apps (Obsidian's
"Sliding Panes"), but not in a terminal's own split system. On macOS there is no
good scrolling WM to lean on, which is exactly where it is missing.

![icon](icon/preview.png)

## Use

```conf
enabled_layouts strip:min_columns=80
```

`min_columns=0` removes the floor, which gives you back the behaviour of the
built in `horizontal` layout.

The scroll position is a pixel offset rather than a column index, so columns at
either edge are partially visible. kitty already clips a window whose geometry
falls outside the viewport, and that clipped sliver is the affordance: it is
what tells you the strip continues.

**The floor does nothing while everything fits.** If the columns add up to less
than the screen they are stretched to fill it, like any other layout. The floor
only starts to matter once the strip overflows.

## Actions

```conf
map cmd+shift+left   layout_action scroll -1      # one column, snaps
map cmd+shift+right  layout_action scroll 1
map cmd+ctrl+equal   layout_action equalize       # even out, keep total width
map cmd+shift+period layout_action resize_all 10  # every column +10 cells
map cmd+shift+comma  layout_action resize_all -10
map cmd+shift+0      layout_action fit            # squeeze back into the screen
map cmd+shift+minus  layout_action min_columns -10
map cmd+shift+equal  layout_action min_columns +10
```

`layout_action hscroll <px>` scrolls freely, without snapping to a column.

A horizontal trackpad gesture scrolls the strip, with the OS momentum intact.

Resizing one column leaves every other column's width alone, so the strip as a
whole grows or shrinks by the same amount — both by dragging a divider and via
`resize_window wider|narrower`.

## Changes outside the layout

- **`Layout.relayout_on_focus_change`** — `Tab.active_window_changed()` only
  refreshes visibility, which is enough for the stack layout but not for one
  whose geometry depends on which window is active. Without it the viewport
  cannot follow the focus.

- **`Layout.wants_horizontal_scroll` / `horizontal_scroll()`**, wired up in
  `mouse.c`. It is handled *before* the momentum gate: that gate drops momentum
  events whose window differs from the one the gesture started on, and scrolling
  a strip slides a different column under a stationary pointer, so the glide
  would otherwise die the moment a column boundary crossed the cursor. Gesture
  level axis locking keeps vertical scrolling untouched, and layouts that do not
  want the event fall through to the existing path.

- **`Strip.drag_resize_target_windows`** — kitty draws two coincident borders
  between adjacent windows, so which one a click lands on is luck, and the same
  drag would sometimes widen the left column and sometimes shrink the right one,
  silently doing nothing when the right one was already at the floor. A divider
  now always belongs to the column on its left.

Scrolling never resizes a pty: `Window.set_geometry` skips `screen.resize` when
the cell count is unchanged, and the columns only move, so no `SIGWINCH` is sent
however far you scroll.

## Files here

| | |
|---|---|
| `kitty.conf` | a config using the layout, styled after Ghostty (Smyck theme, JetBrains Mono, Ghostty's split keybindings) |
| `icon/strip.svg` | app icon: fixed width columns, the rightmost cut off by the edge |
| `icon/apply-icon.sh` | renders the SVG into the launcher bundle (macOS) |

The config needs `brew install --cask font-jetbrains-mono` for the font, and
assumes `/bin/zsh`.

## Known rough edges

- No vertical splits inside a column. A strip is a list of columns, not a tree;
  supporting them means pulling in the `splits` layout's tree.
- `resize_window` overshoots by a few percent, because the increment makes a
  round trip through cells → bias → cells.
- `layout_state` only persists the scroll offset, not the column widths.
