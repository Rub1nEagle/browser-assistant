from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from .observe import ObserveResult, observe


class BrowserError(RuntimeError):
    pass


class StaleElementError(BrowserError):
    pass


ScrollDirection = Literal["down", "up", "top", "bottom"]
WaitCondition = Literal["network_idle", "load", "url_contains", "text_visible"]


class BrowserController:
    """Owns the Playwright lifecycle and the current page.

    Element addressing piggy-backs on Playwright's AI-mode aria snapshot:
    each observe() returns elements tagged `[ref=eN]`, and the controller
    resolves those via the `aria-ref=eN` selector engine. Playwright
    invalidates refs after the next observe, which we surface as
    StaleElementError when an old ref is used.
    """

    def __init__(self, *, profile_dir: Path, headless: bool = False):
        self._profile_dir = profile_dir
        self._headless = headless
        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._refs: set[str] = set()
        # `ref → "<role> 'name'"` derived from the latest observe(). The
        # destructive-action guardrail in the tool registry reads this to
        # decide whether a click/type/etc. needs user confirmation.
        self._ref_labels: dict[str, str] = {}

    async def start(self) -> None:
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir),
            headless=self._headless,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def stop(self) -> None:
        # Tear down both halves independently. Without try/finally a failure
        # in context.close() (common after a page crash) would skip
        # playwright.stop(), leaving the driver process and the profile lock
        # behind — the next start() then trips over its own corpse.
        try:
            if self._context is not None:
                try:
                    await self._context.close()
                except PlaywrightError:
                    pass
                self._context = None
        finally:
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except PlaywrightError:
                    pass
                self._pw = None
            self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserError("controller not started")
        return self._page

    # --- Navigation -------------------------------------------------------

    async def navigate(self, url: str, *, timeout_ms: int = 20_000) -> str:
        try:
            await self.page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except PlaywrightTimeout:
            return (
                f"navigated to {url} (timed out waiting for DOMContentLoaded; "
                f"page may be partial — call observe() to inspect)"
            )
        return f"navigated to {self.page.url}"

    async def go_back(self) -> str:
        # Playwright returns None for both 'no history' and 'navigation
        # didn't produce a Response' (cached page, data: scheme, etc.) —
        # so the URL diff is the honest signal here.
        url_before = self.page.url
        await self.page.go_back(wait_until="domcontentloaded")
        if self.page.url == url_before:
            return "no previous page in history"
        return f"went back to {self.page.url}"

    async def go_forward(self) -> str:
        url_before = self.page.url
        await self.page.go_forward(wait_until="domcontentloaded")
        if self.page.url == url_before:
            return "no forward page in history"
        return f"went forward to {self.page.url}"

    # --- Observation ------------------------------------------------------

    async def observe(self) -> ObserveResult:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=2_000)
        except PlaywrightTimeout:
            pass
        result = await observe(self.page)
        self._refs = result.refs
        self._ref_labels = result.labels
        return result

    def label_for(self, element_id: str) -> str:
        """Best-effort short label for a ref from the latest observe.
        Returns an empty string if the ref has no quoted name."""
        return self._ref_labels.get(element_id, "")

    def _resolve(self, element_id: str):
        ref = str(element_id).strip()
        if ref not in self._refs:
            raise StaleElementError(
                f"ref {ref!r} is not in the latest observe(); call observe() again"
            )
        return ref, self.page.locator(f"aria-ref={ref}")

    # --- Mutating actions -------------------------------------------------

    async def click(self, element_id: str, *, timeout_ms: int = 10_000) -> str:
        ref, locator = self._resolve(element_id)
        try:
            await locator.click(timeout=timeout_ms)
        except PlaywrightTimeout as e:
            raise BrowserError(f"click on {ref} timed out: {e}") from e
        except PlaywrightError as e:
            raise StaleElementError(f"click on {ref} failed: {e}") from e
        await self._settle()
        return f"clicked {ref}"

    async def type_text(
        self, element_id: str, text: str, *, submit: bool = False, timeout_ms: int = 10_000
    ) -> str:
        ref, locator = self._resolve(element_id)
        try:
            # `fill` clears the field first and is more reliable than `type`
            # for textboxes; for contenteditable Playwright still routes to
            # keyboard input under the hood.
            await locator.fill(text, timeout=timeout_ms)
            if submit:
                await locator.press("Enter", timeout=timeout_ms)
        except PlaywrightTimeout as e:
            raise BrowserError(f"type into {ref} timed out: {e}") from e
        except PlaywrightError as e:
            raise StaleElementError(f"type into {ref} failed: {e}") from e
        await self._settle()
        snippet = text if len(text) <= 60 else text[:57] + "…"
        suffix = " + Enter" if submit else ""
        return f"typed into {ref}: {snippet!r}{suffix}"

    async def press_key(
        self, element_id: str, key: str, *, timeout_ms: int = 5_000
    ) -> str:
        ref, locator = self._resolve(element_id)
        try:
            await locator.press(key, timeout=timeout_ms)
        except PlaywrightTimeout as e:
            raise BrowserError(f"press {key!r} on {ref} timed out: {e}") from e
        except PlaywrightError as e:
            raise StaleElementError(f"press {key!r} on {ref} failed: {e}") from e
        await self._settle()
        return f"pressed {key!r} on {ref}"

    async def select_option(
        self, element_id: str, value: str, *, timeout_ms: int = 5_000
    ) -> str:
        ref, locator = self._resolve(element_id)
        try:
            chosen = await locator.select_option(value, timeout=timeout_ms)
        except PlaywrightTimeout as e:
            raise BrowserError(f"select {value!r} on {ref} timed out: {e}") from e
        except PlaywrightError as e:
            raise StaleElementError(f"select {value!r} on {ref} failed: {e}") from e
        await self._settle()
        return f"selected {chosen!r} on {ref}"

    # --- Page-level actions ----------------------------------------------

    async def scroll(self, direction: ScrollDirection, *, amount: int = 800) -> str:
        page = self.page
        if direction == "down":
            await page.mouse.wheel(0, amount)
        elif direction == "up":
            await page.mouse.wheel(0, -amount)
        elif direction == "top":
            await page.evaluate("window.scrollTo({top: 0})")
        elif direction == "bottom":
            await page.evaluate("window.scrollTo({top: document.body.scrollHeight})")
        else:
            raise BrowserError(f"unknown scroll direction: {direction!r}")
        # No networkidle wait — scroll usually triggers lazy-load fetches
        # that may never settle. Caller can wait_for() if needed.
        return f"scrolled {direction}"

    async def wait_for(
        self,
        condition: WaitCondition,
        *,
        value: str | None = None,
        timeout_ms: int = 10_000,
    ) -> str:
        try:
            if condition == "network_idle":
                await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
                return "network is idle"
            if condition == "load":
                await self.page.wait_for_load_state("load", timeout=timeout_ms)
                return "page load event fired"
            if condition == "url_contains":
                if not value:
                    raise BrowserError("wait_for(url_contains) requires `value`")
                await self.page.wait_for_url(f"**{value}**", timeout=timeout_ms)
                return f"URL now contains {value!r}: {self.page.url}"
            if condition == "text_visible":
                if not value:
                    raise BrowserError("wait_for(text_visible) requires `value`")
                await self.page.get_by_text(value, exact=False).first.wait_for(
                    state="visible", timeout=timeout_ms,
                )
                return f"text {value!r} is visible"
        except PlaywrightTimeout as e:
            raise BrowserError(
                f"wait_for({condition}, value={value!r}) timed out after {timeout_ms}ms"
            ) from e
        raise BrowserError(f"unknown wait_for condition: {condition!r}")

    # --- Internal ---------------------------------------------------------

    async def _settle(self) -> None:
        # Many SPAs hold long-polling/WebSocket connections open, so
        # `networkidle` never fires. The previous 3s wait was paid in full
        # on every mutating action. 500ms is plenty for synchronous UI
        # transitions; the agent can still call wait_for() explicitly when
        # it expects a real network round-trip.
        try:
            await self.page.wait_for_load_state("networkidle", timeout=500)
        except PlaywrightTimeout:
            pass
