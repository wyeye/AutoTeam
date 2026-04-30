"""Shared unit-test fixtures and optional dependency stubs."""

import sys
import types

try:
    import playwright.sync_api  # noqa: F401
except ModuleNotFoundError:
    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")

    class PlaywrightStubError(Exception):
        pass

    class PlaywrightStubTimeoutError(PlaywrightStubError):
        pass

    def sync_playwright():
        raise RuntimeError("playwright test stub: browser runtime is unavailable")

    sync_api_module.Error = PlaywrightStubError
    sync_api_module.TimeoutError = PlaywrightStubTimeoutError
    sync_api_module.sync_playwright = sync_playwright
    playwright_module.sync_api = sync_api_module
    sys.modules.setdefault("playwright", playwright_module)
    sys.modules.setdefault("playwright.sync_api", sync_api_module)
