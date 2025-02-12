import json
import logging
import os
from lxml import html
import base64
from typing import Optional

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
        self.clickable_elements = []  # List of clickable elements with their selectors
        self.input_elements = []  # List of input elements with their selectors
        self.visible_text_content = []  # List of visible text content for visual matching

    def parse_html(self, content: str):
        """Parse HTML content into element tree and build selector maps"""
        self.html = content
        self.content = content
        try:
            self.element_tree = ElementTreeWrapper(html.fromstring(content))
            # Reset maps and lists
            self.selector_map = {}
            self.clickable_elements = []
            self.input_elements = []
            self.visible_text_content = []
            
            # Find all input elements
            for element in self.element_tree.element_tree.xpath('//input'):
                selectors = self._build_selectors(element)
                element_info = {
                    'tag': element.tag,
                    'type': element.get('type', ''),
                    'text': element.get('value', ''),
                    'placeholder': element.get('placeholder', ''),
                    'attributes': dict(element.attrib),
                    'selectors': selectors
                }
                
                # Store all possible selectors for this element
                for selector in selectors:
                    self.selector_map[selector] = element_info
                
                self.input_elements.append(element_info)
                
            # Find all clickable elements
            for element in self.element_tree.element_tree.xpath('//*[@onclick or @role="button" or self::a or self::button or self::input[@type="submit" or @type="button"]]'):
                selectors = self._build_selectors(element)
                text_content = element.text_content().strip() if element.text_content() else ""
                element_info = {
                    'tag': element.tag,
                    'text': text_content,
                    'attributes': dict(element.attrib),
                    'selectors': selectors
                }
                
                # Store all possible selectors for this element
                for selector in selectors:
                    self.selector_map[selector] = element_info
                
                self.clickable_elements.append(element_info)
                
                # Store visible text for visual matching
                if text_content:
                    self.visible_text_content.append({
                        'text': text_content,
                        'element': element_info
                    })
                    
        except Exception as e:
            logger.error(f"Failed to parse HTML: {e}")
            self.element_tree = None
            self.selector_map = {}
            self.clickable_elements = []
            self.input_elements = []
            self.visible_text_content = []

    def _build_selectors(self, element) -> list[str]:
        """Build multiple possible selectors for an element"""
        selectors = []
        
        # Try ID selector
        if element.get('id'):
            selectors.append(f'#{element.get("id")}')
            
        # Try class selectors
        if element.get('class'):
            selectors.append(f'.{".".join(element.get("class").split())}')
            
        # Try name selector
        if element.get('name'):
            selectors.append(f'[name="{element.get("name")}"]')
            
        # Try placeholder selector for inputs
        if element.get('placeholder'):
            selectors.append(f'[placeholder="{element.get("placeholder")}"]')
            
        # Try text content selector
        if element.text_content().strip():
            text = element.text_content().strip()
            selectors.append(f'//{element.tag}[contains(text(), "{text}")]')
            
        # Try role selector
        if element.get('role'):
            selectors.append(f'[role="{element.get("role")}"]')
            
        # Try type selector for inputs
        if element.tag == 'input' and element.get('type'):
            selectors.append(f'input[type="{element.get("type")}"]')
            
        return selectors

    def find_best_selector(self, target_text: str, element_type: Optional[str] = None) -> tuple[str, dict] | None:
        """Find the best selector for an element based on text content and type"""
        # First try exact matches in selector map
        for selector, info in self.selector_map.items():
            if (info['text'].lower() == target_text.lower() and 
                (not element_type or info['tag'] == element_type)):
                return selector, info
                
        # Try partial text matches
        for item in self.visible_text_content:
            if (target_text.lower() in item['text'].lower() and
                (not element_type or item['element']['tag'] == element_type)):
                # Return the first selector from the element's selector list
                return item['element']['selectors'][0], item['element']
                
        # Try matching against input placeholders
        if element_type == 'input':
            for element in self.input_elements:
                if (target_text.lower() in element.get('placeholder', '').lower() or
                    target_text.lower() in element.get('value', '').lower()):
                    return element['selectors'][0], element
                    
        return None

    def find_input_field(self, placeholder_or_label: Optional[str] = None) -> tuple[str, dict] | None:
        """Find an input field based on placeholder text or label"""
        # First check direct placeholder matches
        for element in self.input_elements:
            if placeholder_or_label:
                if (placeholder_or_label.lower() in element.get('placeholder', '').lower() or
                    placeholder_or_label.lower() in element.get('value', '').lower()):
                    return element['selectors'][0], element
            else:
                # If no specific text provided, return first text input
                if element.get('type') in ['text', 'search', None]:
                    return element['selectors'][0], element
                    
        # Try finding by nearby label text
        if placeholder_or_label:
            for selector, info in self.selector_map.items():
                if (info['tag'] == 'input' and
                    placeholder_or_label.lower() in info.get('text', '').lower()):
                    return selector, info
                    
        return None

    def get_all_text_content(self) -> str:
        """Get all visible text content from the page for visual understanding"""
        return "\n".join(item['text'] for item in self.visible_text_content)

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