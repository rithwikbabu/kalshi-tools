#!/usr/bin/env python3
import argparse
import asyncio
import curses
import locale
import math
import time
from typing import List, Optional, Tuple

import aiohttp

API_TEMPLATE = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook"


# ---------- fetch ----------
async def fetch_orderbook(session: aiohttp.ClientSession, ticker: str) -> Tuple[List[int], List[int]]:
    url = API_TEMPLATE.format(ticker=ticker)
    async with session.get(url, timeout=5) as resp:
        resp.raise_for_status()
        data = await resp.json()
    ob = (data or {}).get("orderbook", {}) or {}
    yes = ob.get("yes") or []
    no = ob.get("no") or []

    yes_bins = [0] * 101
    no_mapped_bins = [0] * 101
    for p, q in yes:
        p = int(p);
        q = int(q)
        if 0 <= p <= 100:
            yes_bins[p] += q
    for p, q in no:
        p = int(p);
        q = int(q)
        if 0 <= p <= 100:
            m = 100 - p
            no_mapped_bins[m] += q
    return yes_bins, no_mapped_bins


# ---------- utils ----------
locale.setlocale(locale.LC_ALL, "")
ENC = (locale.getpreferredencoding(False) or "").lower()
UNICODE_OK = "utf" in ENC


def clamp(v, lo, hi): return lo if v < lo else hi if v > hi else v


def transform(q: int, log_mode: bool) -> float:
    return math.log10(q + 1.0) if log_mode else float(q)


# ---------- palette (xterm-256 approximations) ----------
X256 = {
    "BID": 48,  # teal
    "ASK": 203,  # coral
    "CURSOR": 15,  # white
    "HEADER": 51,  # electric cyan
    "BEST_BID": 190,  # lime
    "BEST_ASK": 220,  # amber
}


class Colors:
    BID = 1
    ASK = 2
    CURSOR = 3
    HEADER = 4
    BEST_BID = 5
    BEST_ASK = 6


class TUI:
    def __init__(self, stdscr, ticker: str, refresh_ms: int):
        self.stdscr = stdscr
        self.ticker = ticker
        self.refresh_ms = refresh_ms

        self.cursor_price = 50  # 0..100
        self.order_size = 1
        self.side = "YES"
        self.log_mode = False

        self.yes_bins = [0] * 101
        self.no_bins = [0] * 101  # mapped to YES axis
        self.placed: List[str] = []
        self.err: Optional[str] = None

        self.char_bar = "█" if UNICODE_OK else "#"
        self.char_vline = "│" if UNICODE_OK else "|"
        self.char_hline = "─" if UNICODE_OK else "-"
        self.char_dot = "·" if UNICODE_OK else "."
        self.char_up = "▲" if UNICODE_OK else "^"
        self.char_dn = "▼" if UNICODE_OK else "v"

        self.has_color = False

    def setup(self):
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(Colors.BID, X256["BID"], -1)
            curses.init_pair(Colors.ASK, X256["ASK"], -1)
            curses.init_pair(Colors.CURSOR, X256["CURSOR"], -1)
            curses.init_pair(Colors.HEADER, X256["HEADER"], -1)
            curses.init_pair(Colors.BEST_BID, X256["BEST_BID"], -1)
            curses.init_pair(Colors.BEST_ASK, X256["BEST_ASK"], -1)
            self.has_color = True

    # ----- input -----
    def keyloop(self) -> Optional[str]:
        try:
            ch = self.stdscr.getch()
        except curses.error:
            return None
        if ch == -1:
            return None
        if ch in (ord("q"), ord("Q")): return "quit"
        if ch in (curses.KEY_LEFT, ord("h")): self.cursor_price = clamp(self.cursor_price - 1, 0, 100)
        if ch in (curses.KEY_RIGHT, ord("l")): self.cursor_price = clamp(self.cursor_price + 1, 0, 100)
        if ch in (curses.KEY_UP,):   self.order_size = clamp(self.order_size + 1, 1, 999_999)
        if ch in (curses.KEY_DOWN,): self.order_size = clamp(self.order_size - 1, 1, 999_999)
        if ch in (ord("g"), ord("G")): self.log_mode = not self.log_mode
        if ch in (ord("s"), ord("S")): self.side = "NO" if self.side == "YES" else "YES"
        if ch in (curses.KEY_ENTER, 10, 13):
            ts = time.strftime("%H:%M:%S")
            self.placed.insert(0, f"{ts} — {self.side} size {self.order_size} @ {self.cursor_price}c")
            self.placed = self.placed[:50]
        return None

    # ----- primitives -----
    def draw_hline(self, y: int, x1: int, x2: int, ch: str):
        W = self.stdscr.getmaxyx()[1]
        x1 = clamp(x1, 0, W - 1);
        x2 = clamp(x2, 0, W - 1)
        if x2 < x1: x1, x2 = x2, x1
        try:
            self.stdscr.addstr(y, x1, ch * max(0, x2 - x1 + 1))
        except curses.error:
            pass

    def draw_vdots(self, y0: int, h: int, x: int, pair: int):
        for i in range(h):
            if i % 2 == 0:
                try:
                    attr = curses.color_pair(pair) | curses.A_BOLD if self.has_color else curses.A_BOLD
                    self.stdscr.addstr(y0 + i, x, self.char_dot, attr)
                except curses.error:
                    pass

    def text_safe(self, y: int, x_center: int, s: str, attr=0, left_margin=2, right_margin=2):
        W = self.stdscr.getmaxyx()[1]
        max_w = max(0, W - left_margin - right_margin)
        if len(s) > max_w: s = s[:max_w]
        start = clamp(x_center - len(s) // 2, left_margin, left_margin + max_w - len(s))
        try:
            self.stdscr.addnstr(y, start, s, len(s), attr)
        except curses.error:
            pass

    # ----- best prices -----
    def best_yes_bid(self) -> Optional[int]:
        return next((p for p in range(100, -1, -1) if self.yes_bins[p] > 0), None)

    def best_no_bid(self) -> Optional[int]:
        m = next((m for m in range(0, 101) if self.no_bins[m] > 0), None)
        return (100 - m) if m is not None else None

    # ----- draw -----
    def draw(self):
        self.stdscr.erase()
        H, W = self.stdscr.getmaxyx()

        header_h = 4  # includes tooltip
        footer_h = 6
        plot_h = max(8, H - header_h - footer_h)
        plot_y = header_h

        # ----- inner geometry (full available area) -----
        inner_left = 2
        inner_right = W - 3
        inner_w = max(1, inner_right - inner_left + 1)
        inner_top = plot_y + 1
        inner_h = max(1, plot_h - 2)

        # One price = 2 columns (YES, NO). How many prices fit?
        prices_fit = max(1, min(101, inner_w // 2))
        # Center the viewport around the cursor when possible
        start = clamp(self.cursor_price - prices_fit // 2, 0, 101 - prices_fit)
        end = start + prices_fit - 1

        # Center content with padding if inner_w has leftover columns
        left_pad = (inner_w - prices_fit * 2) // 2

        # ------ content frame bounds (chart only) ------
        content_left = inner_left + left_pad
        content_right = content_left + prices_fit * 2 - 1  # inclusive

        # x mappers (keep your fix: NO has no +1)
        def x_yes(p: int) -> int:
            return content_left + 2 * (p - start)

        def x_no(p: int) -> int:
            return content_left + 2 * (p - start)

        # ----- header (after viewport known) -----
        byb = self.best_yes_bid()
        bnb = self.best_no_bid()
        bya = (100 - bnb) if bnb is not None else None
        spread = (bya - byb) if (byb is not None and bya is not None) else None
        line1 = f"{self.ticker} | side={self.side} | size={self.order_size} | scale={'log10' if self.log_mode else 'linear'}"
        line2 = f"YES Bid {byb if byb is not None else '-'}c | YES Ask {bya if bya is not None else '-'}c | Spread {spread if spread is not None else '-'}c"
        try:
            self.stdscr.addnstr(0, 0, line1, W - 1, curses.color_pair(Colors.HEADER) if self.has_color else 0)
            self.stdscr.addnstr(1, 0, line2, W - 1)
        except curses.error:
            pass
        if self.err:
            try:
                self.stdscr.addnstr(2, 0, f"Error: {self.err}", W - 1, curses.A_BOLD)
            except curses.error:
                pass

        # ----- chart frame (ONLY around visible content) -----
        frame_left = max(1, content_left - 1)
        frame_right = min(W - 2, content_right + 1)
        self.draw_hline(plot_y, frame_left, frame_right, self.char_hline)
        self.draw_hline(plot_y + plot_h - 1, frame_left, frame_right, self.char_hline)
        for yy in range(plot_y + 1, plot_y + plot_h - 1):
            try:
                self.stdscr.addstr(yy, frame_left, self.char_vline)
                self.stdscr.addstr(yy, frame_right, self.char_vline)
            except curses.error:
                pass

        # ----- scaling (visible range only) -----
        maxT = 1.0
        for p in range(start, end + 1):
            maxT = max(maxT, transform(self.yes_bins[p], self.log_mode))
            maxT = max(maxT, transform(self.no_bins[p], self.log_mode))

        def height_for(q: int) -> int:
            t = transform(q, self.log_mode)
            return 0 if maxT <= 0 else int((t / maxT) * inner_h)

        # ----- CURSOR FIRST (white line + triangles), side-aligned -----
        if start <= self.cursor_price <= end:
            cx_cur = x_yes(self.cursor_price) if self.side == "YES" else x_no(self.cursor_price)
            try:
                for yy in range(inner_top, inner_top + inner_h):
                    self.stdscr.addstr(yy, cx_cur, self.char_vline,
                                       curses.color_pair(
                                           Colors.CURSOR) | curses.A_BOLD if self.has_color else curses.A_BOLD)
                # ▼ above, ▲ below (kept inside frame)
                self.stdscr.addstr(plot_y - 1, cx_cur, self.char_dn,
                                   curses.color_pair(
                                       Colors.CURSOR) | curses.A_BOLD if self.has_color else curses.A_BOLD)
                self.stdscr.addstr(plot_y + plot_h, cx_cur, self.char_up,
                                   curses.color_pair(
                                       Colors.CURSOR) | curses.A_BOLD if self.has_color else curses.A_BOLD)
            except curses.error:
                pass

        # ----- BARS (after cursor so overlap hides cursor) -----
        attr_yes = curses.color_pair(Colors.BID) if self.has_color else 0
        attr_no = curses.color_pair(Colors.ASK) if self.has_color else 0

        for p in range(start, end + 1):
            h_yes = height_for(self.yes_bins[p])
            h_no = height_for(self.no_bins[p])

            yes_attr = attr_yes | (curses.A_BOLD if (p == self.cursor_price and self.side == "YES") else 0)
            no_attr = attr_no | (curses.A_BOLD if (p == self.cursor_price and self.side == "NO") else 0)

            # YES column
            x = x_yes(p)
            for dy in range(h_yes):
                yy = inner_top + inner_h - 1 - dy
                try:
                    self.stdscr.addstr(yy, x, self.char_bar, yes_attr)
                except curses.error:
                    pass

            # NO→YES column
            x = x_no(p)
            for dy in range(h_no):
                yy = inner_top + inner_h - 1 - dy
                try:
                    self.stdscr.addstr(yy, x, self.char_bar, no_attr)
                except curses.error:
                    pass

        # ----- Best markers (aligned to the same x map) -----
        left_margin = content_left
        right_margin = W - 1 - content_right

        if byb is not None and start <= byb <= end:
            cx = x_yes(byb)
            self.draw_vdots(inner_top, inner_h, cx, Colors.BEST_BID)
            self.text_safe(inner_top, cx, f"Bid {byb}c",
                           curses.color_pair(Colors.BEST_BID) | curses.A_BOLD if self.has_color else curses.A_BOLD,
                           left_margin=left_margin, right_margin=right_margin)

        if bya is not None and start <= bya <= end:
            cx = x_no(bya)
            self.draw_vdots(inner_top, inner_h, cx, Colors.BEST_ASK)
            extra = 1 if (byb is not None and byb == bya) else 0
            self.text_safe(inner_top + extra, cx, f"Ask {bya}c",
                           curses.color_pair(Colors.BEST_ASK) | curses.A_BOLD if self.has_color else curses.A_BOLD,
                           left_margin=left_margin, right_margin=right_margin)

        # ----- Tooltip (cursor info) -----
        cur_yes = self.yes_bins[self.cursor_price]
        cur_no = self.no_bins[self.cursor_price]
        tags = []
        if byb is not None and self.cursor_price == byb: tags.append("BestBid")
        if bya is not None and self.cursor_price == bya: tags.append("BestAsk")
        tagstr = f" [{', '.join(tags)}]" if tags else ""
        info = f"cursor {self.cursor_price}c — YES bid qty {cur_yes} | NO→YES ask qty {cur_no}{tagstr}"
        try:
            self.stdscr.addnstr(2, 0, info, W - 1)
        except curses.error:
            pass

        # ----- Footer -----
        try:
            self.stdscr.addnstr(H - 6, 0, f"(Enter places {self.order_size} {self.side})", W - 1, curses.A_BOLD)
            self.stdscr.addnstr(H - 5, 0, "Placed (fake) orders:", W - 1)
            for i, line in enumerate(self.placed[:4]):
                self.stdscr.addnstr(H - 4 + i, 0, line, W - 1)
        except curses.error:
            pass

        self.stdscr.refresh()

    async def run(self):
        self.setup()
        async with aiohttp.ClientSession() as session:
            next_fetch = 0.0
            while True:
                if self.keyloop() == "quit":
                    return
                now = time.time()
                if now >= next_fetch:
                    try:
                        y, n = await fetch_orderbook(session, self.ticker)
                        self.yes_bins, self.no_bins = y, n
                        self.err = None
                    except Exception as e:
                        self.err = str(e)
                    next_fetch = now + (self.refresh_ms / 1000.0)
                self.draw()
                await asyncio.sleep(0.01)


def main():
    ap = argparse.ArgumentParser(description="Kalshi Orderbook TUI — per-price viewport (no clustering)")
    ap.add_argument("--ticker", required=True, help="Kalshi market ticker")
    ap.add_argument("--refresh-ms", type=int, default=250, help="Refresh interval in ms (default 250)")
    args = ap.parse_args()

    def _wrap(stdscr):
        tui = TUI(stdscr, args.ticker, args.refresh_ms)
        return asyncio.run(tui.run())

    try:
        curses.wrapper(_wrap)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
