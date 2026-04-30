"""Phase 1 smoke test: exercise BrowserController + observe() without the LLM.

Run with: .venv/bin/python scripts/smoke_browser.py [--headless]
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from agent.browser.controller import BrowserController, StaleElementError


async def main() -> int:
    headless = "--headless" in sys.argv
    profile = Path("./.browser-profile-smoke").resolve()
    ctrl = BrowserController(profile_dir=profile, headless=headless)
    await ctrl.start()
    try:
        msg = await ctrl.navigate("https://example.com")
        print("navigate:", msg)
        result = await ctrl.observe()
        print("---- observe ----")
        print(result.rendered)
        print(f"---- {len(result.refs)} refs: {sorted(result.refs)}")

        # Find a "Learn more" / "More information" link by parsing snapshot.
        match = re.search(r'link "([^"]*(?:learn more|more information)[^"]*)" \[ref=(e\d+)\]',
                          result.rendered, re.IGNORECASE)
        if not match:
            print("FAIL: link not found in snapshot", file=sys.stderr)
            return 1
        link_text, ref = match.group(1), match.group(2)
        print(f"clicking {ref!r} ({link_text!r})")
        click_msg = await ctrl.click(ref)
        print("click:", click_msg)
        print("post-click URL:", ctrl.page.url)

        # Sanity-check stale-ref handling: same ref should now error.
        await ctrl.observe()
        try:
            await ctrl.click("e9999")
        except StaleElementError as e:
            print("stale-ref error (expected):", e)

        return 0
    finally:
        await ctrl.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
