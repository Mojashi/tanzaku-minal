#!/usr/bin/env python
# License: GPLv3 Copyright: 2026, strip layout prototype

"""
The strip layout: windows are columns in a horizontal strip that is allowed to
be wider than the screen.

Every other kitty layout divides the available space among the windows, so the
more windows you open the narrower each one gets. This layout inverts that: each
column has an *absolute* width with a floor of ``min_columns`` cells, and when
the columns no longer fit you scroll the strip instead of shrinking them.

Inspired by scrollable-tiling window managers (niri, PaperWM) where the core
primitive is the column rather than the tile.

The scroll position is a pixel offset, not a column index, so columns at either
edge are partially visible. kitty clips a window whose geometry falls outside
the viewport, and that clipped sliver is the affordance: it is what tells you
the strip continues.

Options::

    enabled_layouts strip:min_columns=80

Actions::

    map cmd+shift+left   layout_action scroll -1
    map cmd+shift+right  layout_action scroll 1
    map cmd+ctrl+equal   layout_action equalize
    map cmd+shift+period layout_action resize_all 10
    map cmd+shift+comma  layout_action resize_all -10
    map cmd+shift+0      layout_action fit
"""

import os
from collections.abc import Generator, Sequence
from typing import Any

from kitty.fast_data_types import BOTTOM_EDGE, LEFT_EDGE
from kitty.types import NeighborsMap, WindowMapper, WindowResizeDragData
from kitty.typing_compat import WindowType
from kitty.window_list import WindowGroup, WindowList

from .base import BorderLine, DragOverlayMode, Layout, LayoutData, LayoutOpts, lgd
from .vertical import borders


class StripLayoutOpts(LayoutOpts):

    min_columns: int = 80

    def __init__(self, data: dict[str, str]):
        try:
            self.min_columns = max(1, int(data.get('min_columns', 80)))
        except Exception:
            self.min_columns = 80

    def serialized(self) -> dict[str, Any]:
        return {'min_columns': self.min_columns}


class Strip(Layout):
    name = 'strip'
    main_is_horizontal = True
    no_minimal_window_borders = True
    # The viewport scrolls to follow the focus, so geometry (not just
    # visibility) changes when the active window changes.
    relayout_on_focus_change = True
    wants_horizontal_scroll = True
    drag_overlay_mode = DragOverlayMode.axis_y
    layout_opts = StripLayoutOpts({})

    # MARK: State

    def remove_all_biases(self) -> bool:
        # Column widths in cells, keyed by window group id. This is the whole
        # model: absolute widths, not ratios.
        self.widths: dict[int, int] = {}
        # Horizontal scroll position of the viewport, in pixels.
        self.offset: int = 0
        # (group, cells, x) where x is relative to the left edge of the viewport
        # and may be negative or beyond its right edge.
        self._plan: list[tuple[WindowGroup, int, int]] = []
        self._border_data: list[tuple[WindowGroup, LayoutData, LayoutData]] = []
        self._scrolled: bool = False
        self._more_before: bool = False
        self._more_after: bool = False
        # Free scrolling (trackpad) must not fight the focus-follow logic, so we
        # remember that the user is driving the viewport and stop re-centring on
        # the active column until the focus actually changes.
        self._user_scrolled: bool = False
        self._last_active_id: int = -1
        self._scroll_accum: float = 0.0
        # Live override of layout_opts.min_columns, so the floor can be dialled
        # in with a keybinding instead of editing the config and reloading.
        self._min_override: int | None = None
        return True

    @property
    def min_columns(self) -> int:
        return self.layout_opts.min_columns if self._min_override is None else self._min_override

    # MARK: Free scrolling

    def horizontal_scroll(self, delta: float) -> bool:
        """Trackpad / wheel horizontal scroll, in pixels. Positive delta means
        the finger moved right, which moves the viewport left."""
        self._scroll_accum += -delta
        step = int(self._scroll_accum)
        if step == 0:
            return False
        self._scroll_accum -= step
        before = self.offset
        self.offset = max(0, self.offset + step)
        self._user_scrolled = True
        # The real clamp needs the column sizes, which _compute_plan has; it
        # runs on the relayout this returns True for.
        return self.offset != before

    # MARK: Metrics

    def _decoration(self, wg: WindowGroup) -> int:
        bw = 0 if lgd.draw_minimal_borders else 1
        return wg.decoration('left', border_mult=bw) + wg.decoration('right', border_mult=bw)

    def _width_px(self, wg: WindowGroup, cells: int) -> int:
        return cells * lgd.cell_width + self._decoration(wg)

    def _sync_widths(self, groups: Sequence[WindowGroup]) -> None:
        minc = self.min_columns
        live = {g.id for g in groups}
        for gid in list(self.widths):
            if gid not in live:
                del self.widths[gid]
        for g in groups:
            self.widths.setdefault(g.id, minc)

    def _sizes(self, groups: Sequence[WindowGroup]) -> list[int]:
        return [self._width_px(g, self.widths[g.id]) for g in groups]

    def _max_offset(self, total: int) -> int:
        return max(0, total - lgd.central.width)

    # MARK: Scrolling

    def _scroll_active_into_view(self, all_windows: WindowList, groups: Sequence[WindowGroup], sizes: Sequence[int]) -> None:
        active = all_windows.active_group
        if active is None:
            return
        focus_changed = active.id != self._last_active_id
        self._last_active_id = active.id
        if self._user_scrolled and not focus_changed:
            # The user is dragging the viewport; don't yank it back.
            return
        self._user_scrolled = False
        try:
            idx = groups.index(active)
        except ValueError:
            return
        start = sum(sizes[:idx])
        end = start + sizes[idx]
        view = lgd.central.width
        if start < self.offset:
            self.offset = start
        elif end > self.offset + view:
            self.offset = end - view

    def _first_fully_visible(self, sizes: Sequence[int]) -> int:
        """Index of the first column entirely inside the viewport."""
        x = 0
        view = lgd.central.width
        for i, size in enumerate(sizes):
            if x >= self.offset and x + size <= self.offset + view:
                return i
            x += size
        # Nothing fits entirely; fall back to whatever starts the viewport.
        x = 0
        for i, size in enumerate(sizes):
            if x + size > self.offset:
                return i
            x += size
        return 0

    # MARK: Layout

    def _compute_plan(self, all_windows: WindowList) -> None:
        groups = list(all_windows.iter_all_layoutable_groups())
        self._plan = []
        self._scrolled = False
        self._more_before = False
        self._more_after = False
        if not groups:
            return
        self._sync_widths(groups)

        sizes = self._sizes(groups)
        total = sum(sizes)
        view = lgd.central.width

        if total <= view:
            # Everything fits: stretch to fill, exactly like every other layout.
            self.offset = 0
            usable = view - sum(self._decoration(g) for g in groups)
            cells_total = max(0, usable // lgd.cell_width)
            base = sum(self.widths[g.id] for g in groups) or 1
            assigned = 0
            x = 0
            for i, g in enumerate(groups):
                if i == len(groups) - 1:
                    cells = cells_total - assigned
                else:
                    cells = cells_total * self.widths[g.id] // base
                cells = max(1, cells)
                assigned += cells
                self._plan.append((g, cells, x))
                x += self._width_px(g, cells)
            return

        # Overflowing: scroll the viewport across the strip.
        self._scrolled = True
        max_offset = self._max_offset(total)
        self.offset = max(0, min(self.offset, max_offset))
        self._scroll_active_into_view(all_windows, groups, sizes)
        self.offset = max(0, min(self.offset, max_offset))

        x = 0
        for g, size in zip(groups, sizes):
            if x + size > self.offset and x < self.offset + view:
                self._plan.append((g, self.widths[g.id], x - self.offset))
            x += size
        self._more_before = self.offset > 0
        self._more_after = self.offset < max_offset

    def update_visibility(self, all_windows: WindowList) -> None:
        self._compute_plan(all_windows)
        shown = {wg.id for wg, _, _ in self._plan}
        active_window = all_windows.active_window
        for window, is_group_leader in all_windows.iter_windows_with_visibility():
            wg = all_windows.group_for_window(window)
            is_visible = window is active_window or (
                is_group_leader and wg is not None and wg.id in shown)
            window.set_visible_in_layout(is_visible)

    def do_layout(self, windows: WindowList) -> None:
        if not self._plan:
            self._compute_plan(windows)
        if not self._plan:
            return
        if len(self._plan) == 1 and not self._scrolled:
            self.layout_single_window_group(self._plan[0][0])
            self._border_data = []
            return
        bw = 0 if lgd.draw_minimal_borders else 1
        self._border_data = []
        for wg, cells, x in self._plan:
            size = self._width_px(wg, cells)
            xl = next(self.xlayout(iter((wg,)), start=lgd.central.left + x, size=size, border_mult=bw))
            yl = next(self.ylayout(iter((wg,)), border_mult=bw))
            self.set_window_group_geometry(wg, xl, yl)
            self._border_data.append((wg, xl, yl))
        if os.environ.get('KITTY_STRIP_DEBUG'):
            from kitty.utils import log_error
            log_error(
                f'[strip] central={lgd.central.left},w={lgd.central.width} cell_w={lgd.cell_width} '
                f'offset={self.offset} scrolled={self._scrolled} '
                f'more={self._more_before}/{self._more_after} widths={self.widths} '
                f'plan=' + ' '.join(f'(g{wg.id} cells={c} x={x})' for wg, c, x in self._plan))

    def minimal_borders(self, windows: WindowList) -> Generator[BorderLine, None, None]:
        if len(self._border_data) < 2 or not lgd.draw_minimal_borders:
            return
        # Keep the outermost border on a clipped side: that line marks where the
        # strip continues offscreen.
        #
        # The right hand border is kept unconditionally. Dividers belong to the
        # column on their left, so without it the last column would have no
        # grabbable edge at all and could never be widened by dragging. When the
        # strip is scrolled to its end that border sits exactly at the end of
        # the strip, which is the natural handle for "make the last column
        # wider". (Use resize_window when it is offscreen.)
        yield from borders(
            iter(self._border_data), True, windows,
            start_offset=0 if self._more_before else 1,
            end_offset=0)

    def neighbors_for_window(self, window: WindowType, windows: WindowList) -> NeighborsMap:
        wg = windows.group_for_window(window)
        if wg is None:
            return {}
        groups = tuple(windows.iter_all_layoutable_groups())
        try:
            idx = groups.index(wg)
        except ValueError:
            return {}
        ans: NeighborsMap = {}
        # A strip has ends, so navigation does not wrap.
        if idx > 0:
            ans['left'] = [groups[idx - 1].id]
        if idx < len(groups) - 1:
            ans['right'] = [groups[idx + 1].id]
        return ans

    # MARK: Interactive resize

    def drag_resize_target_windows(
        self, click_window: WindowType, x: float, y: float, edges: int, all_windows: WindowList
    ) -> WindowResizeDragData:
        """A divider belongs to the column on its left.

        kitty draws two coincident border lines between adjacent windows -- the
        right edge of the left one and the left edge of the right one -- and
        which of them a click lands on is essentially luck. The default target
        selection then resizes whichever window was hit, so the same drag would
        sometimes widen the left column and sometimes shrink the right one. Worse,
        when the right column already sits at min_columns the drag silently does
        nothing.

        Normalise it: a grab on a left edge is retargeted to the previous
        column's right edge, so dragging a divider always resizes the column to
        its left and the strip grows or shrinks with it.
        """
        vertical_id = click_window.id
        height_increases_downwards = bool(edges & BOTTOM_EDGE)
        if edges & LEFT_EDGE:
            wg = all_windows.group_for_window(click_window)
            groups = list(all_windows.iter_all_layoutable_groups())
            if wg is not None and wg in groups:
                idx = groups.index(wg)
                if idx > 0:
                    return WindowResizeDragData(
                        groups[idx - 1].active_window_id, True, vertical_id, height_increases_downwards)
            # The left edge of the first column is the edge of the strip itself;
            # there is nothing to its left to resize.
            return WindowResizeDragData(None, True, vertical_id, height_increases_downwards)
        return WindowResizeDragData(click_window.id, True, vertical_id, height_increases_downwards)

    def apply_bias(self, window_id: int, increment: float, all_windows: WindowList, is_horizontal: bool = True) -> bool:
        """Resize one column. Every other column keeps its width, so the strip
        as a whole grows or shrinks by the same amount."""
        if not is_horizontal:
            return False
        groups = list(all_windows.iter_all_layoutable_groups())
        if not groups or window_id >= len(groups):
            return False
        self._set_dimensions(all_windows)
        self._sync_widths(groups)
        g = groups[window_id]
        delta = int(round(increment * lgd.central.width / lgd.cell_width))
        if delta == 0:
            delta = 1 if increment > 0 else -1
        new = max(self.min_columns, self.widths[g.id] + delta)
        if new == self.widths[g.id]:
            return False
        self.widths[g.id] = new
        return True

    # MARK: Actions

    def layout_action(self, action_name: str, args: Sequence[str], all_windows: WindowList) -> bool | None:
        if os.environ.get('KITTY_STRIP_DEBUG'):
            from kitty.utils import log_error
            log_error(f'[strip] layout_action {action_name} {list(args)}')
        groups = list(all_windows.iter_all_layoutable_groups())
        if not groups:
            return None
        self._set_dimensions(all_windows)
        self._sync_widths(groups)
        minc = self.min_columns
        sizes = self._sizes(groups)
        total = sum(sizes)
        view = lgd.central.width

        if action_name == 'scroll':
            if total <= view:
                return None
            try:
                delta = int(args[0]) if args else 1
            except Exception:
                delta = 1
            # Snap to column boundaries so scrolling advances a whole column.
            bounds = [0]
            for size in sizes:
                bounds.append(bounds[-1] + size)
            max_offset = self._max_offset(total)
            if delta > 0:
                target = next((b for b in bounds if b > self.offset + 1), max_offset)
                new = min(target, max_offset)
            else:
                target = next((b for b in reversed(bounds) if b < self.offset - 1), 0)
                new = max(target, 0)
            if new == self.offset:
                return None
            self.offset = new
            # Carry the focus along, otherwise _scroll_active_into_view would
            # drag the viewport straight back on the next layout pass.
            active = all_windows.active_group
            idx = groups.index(active) if active in groups else 0
            start = sum(sizes[:idx])
            if start < self.offset or start + sizes[idx] > self.offset + view:
                all_windows.set_active_group_idx(self._first_fully_visible(sizes))
            return True

        if action_name == 'hscroll':
            # Free (non-snapping) scroll by a pixel amount. Same path the
            # trackpad uses; exposed as an action so it can be bound to keys.
            try:
                px = float(args[0]) if args else 100.0
            except Exception:
                px = 100.0
            return self.horizontal_scroll(-px) or None

        if action_name == 'min_columns':
            # Change the floor live. Accepts an absolute value or a relative
            # +N/-N. Every column is set to the new floor so the change is
            # immediately visible -- otherwise nothing appears to happen until
            # enough columns exist to overflow the screen.
            arg = args[0] if args else '+10'
            try:
                new = self.min_columns + int(arg) if arg[0] in '+-' else int(arg)
            except Exception:
                return None
            new = max(10, new)
            if new == self.min_columns:
                return None
            self._min_override = new
            for g in groups:
                self.widths[g.id] = new
            self.offset = 0
            return True

        if action_name == 'equalize':
            # 幅を全部合わせる: even out the columns, keeping the total width.
            avg = max(minc, sum(self.widths[g.id] for g in groups) // len(groups))
            for g in groups:
                self.widths[g.id] = avg
            return True

        if action_name == 'resize_all':
            # 全部の幅を等しく増やす / 減らす.
            try:
                delta = int(args[0]) if args else 10
            except Exception:
                delta = 10
            for g in groups:
                self.widths[g.id] = max(minc, self.widths[g.id] + delta)
            return True

        if action_name == 'fit':
            # 全部収める: squeeze the strip back into the screen. The floor
            # still wins, so this cannot make a column unreadably narrow.
            dec = sum(self._decoration(g) for g in groups)
            cells = max(0, (view - dec) // lgd.cell_width)
            each = max(minc, cells // len(groups))
            for g in groups:
                self.widths[g.id] = each
            self.offset = 0
            return True

        return None

    # MARK: Session state

    def layout_state(self) -> dict[str, Any]:
        return {'offset': self.offset}

    def set_layout_state(self, layout_state: dict[str, Any], map_group_id: WindowMapper) -> bool:
        self.offset = int(layout_state.get('offset', 0))
        return True
