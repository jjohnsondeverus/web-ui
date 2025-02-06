from typing import List, Dict, Optional
import json
import os
from datetime import datetime
from playwright.async_api import BrowserContext, Page
import asyncio
import logging

logger = logging.getLogger(__name__)

class BrowserEventHandler:
    """Handles browser events and converts them to recorded steps"""
    
    def __init__(self, recorder):
        self.recorder = recorder
        self.last_url = None  # Track last URL to avoid duplicates

    async def handle_click(self, page: Page, event):
        """Handle click events"""
        try:
            element_info = event['element']
            
            # Build selector using the rich element info
            selector = None
            if element_info['id']:
                selector = f"#{element_info['id']}"
            elif element_info['name']:
                selector = f"[name='{element_info['name']}']"
            elif element_info['href']:
                selector = f"a[href='{element_info['href']}']"
            elif element_info['textContent']:
                selector = f"{element_info['tagName']}:text('{element_info['textContent']}')"
            else:
                selector = element_info['tagName']
            
            # Record the click with element details
            self.recorder.record_step(
                action="click",
                selector=selector,
                element_type=element_info['tagName'],
                text=element_info['textContent'],
                x=event['x'],
                y=event['y']
            )
            logger.debug(f"Recorded click on {selector}")
        except Exception as e:
            logger.error(f"Error recording click: {e}")

    async def handle_input(self, page: Page, event):
        """Handle input events"""
        try:
            element_info = event['element']
            
            # Build selector for input field
            selector = None
            if element_info['id']:
                selector = f"#{element_info['id']}"
            elif element_info['name']:
                selector = f"[name='{element_info['name']}']"
            else:
                selector = f"{element_info['tagName']}[type='{element_info['type']}']"
            
            # Record the input with element details
            self.recorder.record_step(
                action="input",
                selector=selector,
                element_type=element_info['tagName'],
                input_type=element_info['type'],
                value=event['value']
            )
            logger.debug(f"Recorded input in {selector}")
        except Exception as e:
            logger.error(f"Error recording input: {e}")

    async def handle_navigation(self, page: Page):
        """Handle navigation events with deduplication"""
        try:
            url = page.url
            # Only record if URL actually changed
            if url != self.last_url:
                self.last_url = url
                self.recorder.record_step(
                    action="navigate",
                    url=url
                )
        except Exception as e:
            logger.error(f"Error recording navigation: {e}")

class TaskRecorder:
    """Records browser interactions for task automation"""
    
    def __init__(self, save_dir: str = "recorded_tasks"):
        self.save_dir = save_dir
        self.recording = False
        self.current_task_name: Optional[str] = None
        self.recorded_steps: List[Dict] = []
        self.event_handler = BrowserEventHandler(self)
        self.current_context: Optional[BrowserContext] = None
        
        # Create save directory if it doesn't exist
        os.makedirs(save_dir, exist_ok=True)
        
    def start_recording(self, task_name: str) -> None:
        """Start recording a new task"""
        self.recording = True
        self.current_task_name = task_name
        self.recorded_steps = []
        
    def stop_recording(self) -> List[Dict]:
        """Stop recording and return the recorded steps"""
        self.recording = False
        return self.recorded_steps
        
    def record_step(self, action: str, **kwargs) -> None:
        """Record a single browser interaction step"""
        if not self.recording:
            return
            
        step = {
            "action": action,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.recorded_steps.append(step)
        logger.info(f"Recorded step: {step}")  # Changed to info for debugging
        
    def save_recording(self) -> str:
        """Save the current recording to a file"""
        if not self.current_task_name:
            raise ValueError("No task name set")
            
        filename = f"{self.current_task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(self.save_dir, filename)
        
        with open(filepath, 'w') as f:
            json.dump({
                "task_name": self.current_task_name,
                "recorded_at": datetime.now().isoformat(),
                "steps": self.recorded_steps
            }, f, indent=2)
            
        return filepath

    async def attach_to_browser(self, context: BrowserContext) -> None:
        """Attach event listeners to browser context"""
        self.current_context = context
        logger.info("Attaching to browser context")
        
        # Listen for new pages
        context.on('page', lambda page: 
            asyncio.create_task(self._setup_page(page))
        )
        
        # Setup existing pages
        for page in context.pages:
            await self._setup_page(page)
        
        # Create initial page if none exists
        if not context.pages:
            page = await context.new_page()
            await self._setup_page(page)

    async def _setup_page(self, page: Page):
        """Set up event recording for a new page"""
        try:
            logger.info(f"Setting up recording for page: {page.url}")
            
            # Wait for page to be ready
            await page.wait_for_load_state('domcontentloaded')

            # Add debug logging to see what events are actually happening
            await page.evaluate("""() => {
                const observer = new MutationObserver((mutations) => {
                    console.log('DOM changed:', mutations.length, 'mutations');
                });
                observer.observe(document.body, { 
                    childList: true, 
                    subtree: true, 
                    attributes: true 
                });
                
                // Debug click events
                document.addEventListener('click', (e) => {
                    console.log('Raw click event:', {
                        target: e.target.tagName,
                        id: e.target.id,
                        type: e.type,
                        x: e.clientX,
                        y: e.clientY
                    });
                }, true);
                
                // Debug input events
                document.addEventListener('input', (e) => {
                    console.log('Raw input event:', {
                        target: e.target.tagName,
                        id: e.target.id,
                        type: e.type,
                        value: e.target.value
                    });
                }, true);
            }""")

            # Add click listener with debug logging
            async def handle_click(click_info):
                logger.info("Playwright click event received")  # Debug log
                try:
                    element = click_info.element
                    if not element:
                        logger.info("No element in click event")  # Debug log
                        return
                        
                    # Get element properties
                    props = await element.evaluate("""(el) => ({
                        tagName: el.tagName.toLowerCase(),
                        id: el.id,
                        className: el.className,
                        textContent: el.textContent?.trim(),
                        href: el.href,
                        type: el.type,
                        name: el.name
                    })""")
                    
                    logger.info(f"Click detected on: {props}")
                    
                    await self.event_handler.handle_click(page, {
                        'element': props,
                        'x': click_info.position["x"] if hasattr(click_info, "position") else 0,
                        'y': click_info.position["y"] if hasattr(click_info, "position") else 0
                    })
                except Exception as e:
                    logger.error(f"Error handling click: {e}", exc_info=True)  # Added exc_info

            # Add input listener with debug logging
            async def handle_input(element_handle):
                logger.info("Playwright input event received")  # Debug log
                try:
                    props = await element_handle.evaluate("""(el) => ({
                        tagName: el.tagName.toLowerCase(),
                        id: el.id,
                        type: el.type,
                        name: el.name,
                        value: el.type === 'password' ? '********' : el.value
                    })""")
                    
                    logger.info(f"Input detected on: {props}")
                    
                    await self.event_handler.handle_input(page, {
                        'element': props,
                        'value': props['value']
                    })
                except Exception as e:
                    logger.error(f"Error handling input: {e}", exc_info=True)  # Added exc_info

            # Add Playwright event listeners
            page.on("click", handle_click)
            page.on("input", handle_input)
            
            # Add page lifecycle events with debug logging
            page.on('console', lambda msg: logger.info(f"Browser console: {msg.text}"))
            page.on('pageerror', lambda err: logger.error(f"Browser error: {err}"))
            page.on('load', lambda: logger.info(f"Page loaded: {page.url}"))
            page.on('domcontentloaded', lambda: 
                asyncio.create_task(self.event_handler.handle_navigation(page))
            )
            
            logger.info("Page recording setup complete")
            
        except Exception as e:
            logger.error(f"Error setting up page recording: {e}", exc_info=True)  # Added exc_info

    async def _handle_page_event(self, event_type: str, data: Dict, page: Page):
        """Handle events from the page"""
        if not self.recording:
            return
            
        try:
            logger.info(f"Handling {event_type} event: {data}")  # Changed to info for testing
            
            if event_type == 'click':
                await self.event_handler.handle_click(page, data)
            elif event_type == 'input':
                await self.event_handler.handle_input(page, data)
        except Exception as e:
            logger.error(f"Error handling {event_type} event: {e}") 