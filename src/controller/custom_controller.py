import pyperclip
from typing import Optional, Type
from pydantic import BaseModel
from browser_use.agent.views import ActionResult
from browser_use.browser.context import BrowserContext
from browser_use.controller.service import Controller, DoneAction


class CustomController(Controller):
    def __init__(self, exclude_actions: list[str] = [],
                output_model: Optional[Type[BaseModel]] = None
                ):
        super().__init__(exclude_actions=exclude_actions, output_model=output_model)
        self._register_custom_actions()

    def _register_custom_actions(self):
        """Register all custom browser actions"""

        @self.registry.action("Copy text to clipboard")
        def copy_to_clipboard(text: str):
            pyperclip.copy(text)
            return ActionResult(extracted_content=text)

        @self.registry.action("Paste text from clipboard", requires_browser=True)
        async def paste_from_clipboard(browser: BrowserContext):
            text = pyperclip.paste()
            # send text to browser
            page = await browser.get_current_page()
            await page.keyboard.type(text)
            return ActionResult(extracted_content=text)

    async def input_text(self, page, text, selector=None, xpath=None, index=None):
        """Input text into an element"""
        try:
            # Get the current browser state
            browser_state = await page.context.get_state(use_vision=True)
            
            # First try using provided selector/xpath
            if selector or xpath:
                element = await page.query_selector(selector) if selector else await page.query_selector(f"xpath={xpath}")
                if element:
                    await element.fill(text)
                    return True

            # If that fails, try finding input field visually
            if browser_state:
                # Try finding by placeholder/label that might match the text
                input_selector = None
                if browser_state.find_input_field():
                    input_selector, _ = browser_state.find_input_field()
                
                # If no match, try finding any search/text input
                if not input_selector and browser_state.find_input_field("search"):
                    input_selector, _ = browser_state.find_input_field("search")
                
                if input_selector:
                    element = await page.query_selector(input_selector)
                    if element:
                        await element.fill(text)
                        return True

            # Fall back to index as last resort
            if index is not None:
                elements = await page.query_selector_all('input')
                if 0 <= index < len(elements):
                    await elements[index].fill(text)
                    return True

            raise ValueError(f"Element not found with selector={selector}, xpath={xpath}, index={index}")
        except Exception as e:
            raise Exception(f"Error executing action input_text: {str(e)}")

    async def click_element(self, page, selector=None, xpath=None, index=None):
        """Click an element"""
        try:
            # Get the current browser state
            browser_state = await page.context.get_state(use_vision=True)
            
            # First try using provided selector/xpath
            if selector or xpath:
                element = await page.query_selector(selector) if selector else await page.query_selector(f"xpath={xpath}")
                if element:
                    await element.click()
                    return True

            # If that fails, try finding element visually
            if browser_state:
                # Try finding clickable element by visible text
                for clickable in browser_state.clickable_elements:
                    if clickable['selectors']:
                        element = await page.query_selector(clickable['selectors'][0])
                        if element:
                            await element.click()
                            return True

            # Fall back to index as last resort
            if index is not None:
                elements = await page.query_selector_all('button, input[type="submit"], input[type="button"], a')
                if 0 <= index < len(elements):
                    await elements[index].click()
                    return True

            raise ValueError(f"Element not found with selector={selector}, xpath={xpath}, index={index}")
        except Exception as e:
            raise Exception(f"Error executing action click_element: {str(e)}")

    async def send_keys(self, page, keys):
        """Send keyboard keys"""
        try:
            await page.keyboard.press(keys)
            return True
        except Exception as e:
            raise Exception(f"Error executing action send_keys: {str(e)}")

    async def scroll_down(self, page, amount):
        """Scroll the page down by a specified amount"""
        try:
            await page.evaluate(f"window.scrollBy(0, {amount})")
            return True
        except Exception as e:
            raise Exception(f"Error executing action scroll_down: {str(e)}")

    async def go_to_url(self, page, url):
        """Navigate to a URL"""
        try:
            await page.goto(url)
            return True
        except Exception as e:
            raise Exception(f"Error executing action go_to_url: {str(e)}")
