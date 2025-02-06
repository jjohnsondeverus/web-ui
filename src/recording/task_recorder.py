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
            # Skip empty input events
            if not event.get('value', '').strip():
                return
            
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

            # First expose the functions
            await page.expose_function("__recordClick", 
                lambda click_info: asyncio.create_task(self.event_handler.handle_click(page, click_info)))
            await page.expose_function("__recordInput", 
                lambda input_info: asyncio.create_task(self.event_handler.handle_input(page, input_info)))

            # Add navigation listener to reinject script after each navigation
            async def handle_navigation():
                logger.info("Reinjecting event listeners after navigation")
                await inject_listeners()

            # Define the injection function
            async def inject_listeners():
                await page.evaluate("""() => {
                    // Remove old listeners if they exist
                    window.__recorderInitialized = false;
                    
                    // Only inject once per page
                    if (!window.__recorderInitialized) {
                        window.__recorderInitialized = true;
                        console.log('Initializing event listeners');
                        
                        // Click listener
                        document.addEventListener('click', event => {
                            const element = event.target;
                            const clickInfo = {
                                element: {
                                    tagName: element.tagName.toLowerCase(),
                                    id: element.id || '',
                                    className: element.className || '',
                                    textContent: element.textContent?.trim() || '',
                                    href: element.href || '',
                                    type: element.type || '',
                                    name: element.name || ''
                                },
                                x: event.clientX,
                                y: event.clientY
                            };
                            console.log('Sending click event:', clickInfo);
                            window.__recordClick(clickInfo);
                        }, true);
                        
                        // Debounced input capture with focus on final values
                        let inputTimeout;
                        let lastRecordedValue = '';
                        let focusedElement = null;
                        
                        const captureInput = (event) => {
                            const element = event.target;
                            
                            // Clear any pending timeout
                            if (inputTimeout) {
                                clearTimeout(inputTimeout);
                            }
                            
                            // Set new timeout
                            inputTimeout = setTimeout(() => {
                                const value = element.type === 'password' ? '********' : element.value;
                                
                                // Skip empty or unchanged values
                                if (!value.trim() || value === lastRecordedValue) {
                                    return;
                                }
                                
                                // Only record if:
                                // 1. Element lost focus (complete input), or
                                // 2. User paused typing for 1 second
                                if (element !== focusedElement || element.value.length > 3) {
                                    lastRecordedValue = value;
                                    
                                    const inputInfo = {
                                        element: {
                                            tagName: element.tagName.toLowerCase(),
                                            id: element.id || '',
                                            type: element.type || '',
                                            name: element.name || ''
                                        },
                                        value: value
                                    };
                                    console.log('Sending input event:', inputInfo);
                                    window.__recordInput(inputInfo);
                                }
                            }, 1000); // Increased to 1 second for better completion detection
                        };

                        // Track focused element
                        document.addEventListener('focusin', (e) => {
                            focusedElement = e.target;
                        });
                        
                        document.addEventListener('focusout', (e) => {
                            focusedElement = null;
                            // Capture final value on blur
                            captureInput(e);
                        });

                        // Listen for input events
                        document.addEventListener('input', captureInput, true);
                        
                        console.log('Event listeners successfully initialized');
                    }
                }""")

            # Initial injection
            await inject_listeners()
            
            # Add page lifecycle events
            page.on('load', lambda: asyncio.create_task(handle_navigation()))
            page.on('domcontentloaded', lambda: asyncio.create_task(self.event_handler.handle_navigation(page)))
            page.on('console', lambda msg: logger.info(f"Browser console: {msg.text}"))
            page.on('pageerror', lambda err: logger.error(f"Browser error: {err}"))
            
            logger.info("Page recording setup complete")
            
        except Exception as e:
            logger.error(f"Error setting up page recording: {e}", exc_info=True)

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