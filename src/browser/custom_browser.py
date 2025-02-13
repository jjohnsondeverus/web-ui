import asyncio
import logging
import os
from typing import Optional, Union, Dict, Any

from browser_use.browser.browser import Browser, BrowserConfig
from browser_use.browser.context import BrowserContext, BrowserContextConfig
from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import BrowserContext as PlaywrightBrowserContext
from playwright.async_api import Playwright, ViewportSize
from playwright.async_api import async_playwright

from .custom_context import CustomBrowserContext

logger = logging.getLogger(__name__)

class CustomBrowser(Browser):
    def __init__(self, config: Optional[BrowserConfig] = None):
        super().__init__(config=config or BrowserConfig())
        self._browser: Optional[PlaywrightBrowser] = None
        logger.info(f"CustomBrowser.__init__: Received config: {config}")

    async def new_context(
        self,
        config: BrowserContextConfig = BrowserContextConfig()
    ) -> CustomBrowserContext:
        if not self._browser:
            try:
                from webui import _global_playwright
            except ImportError:
                _global_playwright = None
            playwright = _global_playwright if _global_playwright is not None else await async_playwright().start()
            logger.info(f"CustomBrowser.new_context: chrome_instance_path = {self.config.chrome_instance_path}")
            if self.config.chrome_instance_path:
                # Use the existing Chrome instance via remote debugging
                self._browser = await self._setup_browser_with_instance(playwright)
            else:
                # Launch new Chromium instance
                self._browser = await playwright.chromium.launch(
                    headless=self.config.headless,
                    args=self.config.extra_chromium_args
                )
        
        # Create the context with proper viewport settings
        context = CustomBrowserContext(browser=self, config=config)
        context._context = await self._browser.new_context(
            viewport={'width': config.browser_window_size['width'], 'height': config.browser_window_size['height']} 
                    if config.browser_window_size else None
        )
        return context

    async def close(self):
        """Close the browser and clean up resources"""
        if self._browser:
            await self._browser.close()
            self._browser = None

    async def _setup_browser_with_instance(self, playwright: Playwright) -> PlaywrightBrowser:
        """Sets up and returns a Playwright Browser instance with anti-detection measures."""
        if not self.config.chrome_instance_path:
            raise ValueError('Chrome instance path is required')
        logger.debug(f"_setup_browser_with_instance: chrome_instance_path = {self.config.chrome_instance_path}")
        import subprocess
        import requests

        try:
            # Check if browser is already running
            response = requests.get('http://localhost:9222/json/version', timeout=2)
            logger.debug(f"Response status code from localhost:9222/json/version: {response.status_code}")
            if response.status_code == 200:
                logger.info('Reusing existing Chrome instance via CDP')
                self._browser = await playwright.chromium.connect_over_cdp(
                    endpoint_url='http://localhost:9222',
                    timeout=20000  
                )
                logger.debug('Successfully connected to Chrome via CDP')
                return self._browser
        except requests.ConnectionError:
            logger.debug('No existing Chrome instance found, will start a new instance')

        # Start a new Chrome instance
        subprocess.Popen(
            [
                self.config.chrome_instance_path,
                '--remote-debugging-port=9222',
            ] + self.config.extra_chromium_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
  
        # try to connect first in case the browser have not started
        for _ in range(10):
            try:
                response = requests.get('http://localhost:9222/json/version', timeout=2)
                if response.status_code == 200:
                    break
            except requests.ConnectionError:
                pass
            await asyncio.sleep(1)

        # Attempt to connect again after starting a new instance
        try:
            self._browser = await playwright.chromium.connect_over_cdp(
                endpoint_url='http://localhost:9222',
                timeout=20000,  # 20 second timeout for connection
            )
            return self._browser
        except Exception as e:
            logger.error(f'Failed to start a new Chrome instance.: {str(e)}')
            raise RuntimeError(
                'To start chrome in Debug mode, you need to close all existing Chrome instances and try again otherwise we can not connect to the instance.'
            )