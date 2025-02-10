import json
import logging
import os
from lxml import html
import base64

from browser_use.browser.browser import Browser
from browser_use.browser.context import BrowserContext, BrowserContextConfig
from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import BrowserContext as PlaywrightBrowserContext

logger = logging.getLogger(__name__)


class BrowserState:
    """Class to hold browser state information"""
    def __init__(self):
        self.url: str = ""
        self.title: str = ""
        self.content: str = ""
        self.screenshot: str | None = None  # Can be base64 string or None
        self.element_tree = None  # Required by agent
        self.tabs = []  # Required by agent
        self.current_tab = None  # Required by agent
        self.html = ""  # Required by agent
        self.pixels_above = 0  # Scroll position from top
        self.pixels_below = 0  # Remaining scroll distance to bottom
        self.selector_map = {}  # Map of element selectors

    def parse_html(self, content: str):
        """Parse HTML content into element tree"""
        self.html = content
        self.content = content
        try:
            self.element_tree = ElementTreeWrapper(html.fromstring(content))
            # Build selector map from clickable elements
            self.selector_map = {}
            for element in self.element_tree.element_tree.xpath('//*[@onclick or @role="button" or self::a or self::button or self::input[@type="submit" or @type="button"]]'):
                selector = self._build_selector(element)
                if selector:
                    self.selector_map[selector] = {
                        'tag': element.tag,
                        'text': element.text_content().strip() if element.text_content() else "",
                        'attributes': dict(element.attrib)
                    }
        except Exception as e:
            logger.error(f"Failed to parse HTML: {e}")
            self.element_tree = None
            self.selector_map = {}

    def _build_selector(self, element):
        """Build a unique selector for an element"""
        if element.get('id'):
            return f'#{element.get("id")}'
        elif element.get('class'):
            return f'.{".".join(element.get("class").split())}'
        elif element.text_content().strip():
            return f'//{element.tag}[contains(text(), "{element.text_content().strip()}")]'
        return None

class ElementTreeWrapper:
    """Wrapper for lxml element tree to add required functionality"""
    def __init__(self, element_tree):
        self.element_tree = element_tree

    def clickable_elements_to_string(self, include_attributes: bool = True) -> str:
        """Convert clickable elements to string representation
        
        Args:
            include_attributes: Whether to include element attributes in the output
        """
        clickable = []
        # Find all clickable elements (links, buttons, inputs)
        for element in self.element_tree.xpath('//*[@onclick or @role="button" or self::a or self::button or self::input[@type="submit" or @type="button"]]'):
            text = element.text_content().strip() if element.text_content() else ""
            tag = element.tag
            desc = f"{tag}"
            if text:
                desc += f" with text '{text}'"
            if include_attributes:
                classes = element.get('class', '')
                id_attr = element.get('id', '')
                if id_attr:
                    desc += f" id='{id_attr}'"
                if classes:
                    desc += f" class='{classes}'"
            clickable.append(desc)
        return "\n".join(clickable) if clickable else "No clickable elements found"

    def __getattr__(self, name):
        """Delegate unknown attributes to underlying element tree"""
        return getattr(self.element_tree, name)

class CustomBrowserContext(BrowserContext):
    def __init__(
        self,
        browser: "Browser",
        config: BrowserContextConfig = BrowserContextConfig()
    ):
        super().__init__(browser=browser)  # Pass browser to parent init
        self.config = config
        self._context = None
        self._page = None
        self.session = None  # Required by base class
        self._event_handlers = {}

    def on(self, event: str, handler):
        """Register an event handler"""
        if self._context:
            self._context.on(event, handler)
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    async def get_state(self, use_vision: bool = False):
        """Get the current state of the browser context"""
        if not self._context:
            raise RuntimeError("Browser context not initialized")
            
        if not self._page:
            self._page = await self._context.new_page()
        
        # Create state object with proper attributes    
        state = BrowserState()
        state.url = self._page.url
        state.title = await self._page.title()
        content = await self._page.content()
        state.parse_html(content)  # Parse HTML into element tree
        state.tabs = self.pages  # Set current tabs
        state.current_tab = self._page  # Set current tab
        
        # Calculate scroll positions
        js_scroll_info = """
            () => {
                const scrollTop = window.pageYOffset;
                const scrollHeight = document.documentElement.scrollHeight;
                const clientHeight = document.documentElement.clientHeight;
                return {
                    pixels_above: scrollTop,
                    pixels_below: Math.max(0, scrollHeight - clientHeight - scrollTop)
                };
            }
        """
        scroll_info = await self._page.evaluate(js_scroll_info)
        state.pixels_above = scroll_info['pixels_above']
        state.pixels_below = scroll_info['pixels_below']
        
        if use_vision:
            # Add screenshot if vision is enabled
            screenshot_bytes = await self._page.screenshot(type='jpeg', quality=50)
            state.screenshot = base64.b64encode(screenshot_bytes).decode('utf-8')
            
        return state

    @property
    def pages(self):
        """Get all pages in the context"""
        if self._context:
            return self._context.pages
        return []

    async def new_page(self):
        """Create a new page in the context"""
        if not self._context:
            raise RuntimeError("Browser context not initialized")
        self._page = await self._context.new_page()
        return self._page

    async def close(self):
        """Close the browser context"""
        if self._context:
            await self._context.close()
            self._context = None
            self._page = None
            self._event_handlers.clear()

    async def attach_page(self, page):
        """Attach an existing page to this context"""
        self._context = page.context
        self._page = page
        # Re-register any event handlers
        for event, handlers in self._event_handlers.items():
            for handler in handlers:
                self._context.on(event, handler)