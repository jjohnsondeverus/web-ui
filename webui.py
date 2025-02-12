from __future__ import annotations
import pdb
import logging
import json
import datetime

from dotenv import load_dotenv

load_dotenv()
import os
import glob
import asyncio
import argparse
import os

logger = logging.getLogger(__name__)

import gradio as gr

from browser_use.agent.service import Agent
from playwright.async_api import async_playwright
from browser_use.browser.browser import Browser, BrowserConfig
from browser_use.browser.context import (
    BrowserContext,
    BrowserContextConfig,
    BrowserContextWindowSize,
)
from langchain_ollama import ChatOllama
from src.utils.agent_state import AgentState

from src.utils import utils
from src.agent.custom_agent import CustomAgent
from src.browser.custom_browser import CustomBrowser
from src.browser.custom_context import CustomBrowserContext
from src.controller.custom_controller import CustomController
from gradio.themes import Citrus, Default, Glass, Monochrome, Ocean, Origin, Soft, Base
from src.utils.default_config_settings import default_config, load_config_from_file, save_config_to_file, save_current_config, update_ui_from_config
from src.utils.utils import update_model_dropdown, get_latest_files, capture_screenshot
from src.recording.task_recorder import TaskRecorder, BrowserEventHandler

from browser_use.browser.browser import BrowserConfig
from browser_use.browser.context import BrowserContextConfig

from src.agent.custom_prompts import CustomSystemPrompt, CustomAgentMessagePrompt

# Global variables for persistence
_global_browser = None
_global_browser_context = None
_global_task_recorder = None
_global_playwright = None

# Create the global agent state instance
_global_agent_state = AgentState()

async def stop_agent():
    """Request the agent to stop and update UI with enhanced feedback"""
    global _global_agent_state, _global_browser_context, _global_browser

    try:
        # Request stop
        _global_agent_state.request_stop()

        # Update UI immediately
        message = "Stop requested - the agent will halt at the next safe point"
        logger.info(f"🛑 {message}")

        # Return UI updates
        return (
            message,                                        # errors_output
            gr.update(value="Stopping...", interactive=False),  # stop_button
            gr.update(interactive=False),                      # run_button
        )
    except Exception as e:
        error_msg = f"Error during stop: {str(e)}"
        logger.error(error_msg)
        return (
            error_msg,
            gr.update(value="Stop", interactive=True),
            gr.update(interactive=True)
        )

async def run_browser_agent(
        agent_type,
        llm_provider,
        llm_model_name,
        llm_temperature,
        llm_base_url,
        llm_api_key,
        use_own_browser,
        keep_browser_open,
        headless,
        disable_security,
        window_w,
        window_h,
        save_recording_path,
        save_agent_history_path,
        save_trace_path,
        enable_recording,
        task,
        add_infos,
        max_steps,
        use_vision,
        max_actions_per_step,
        tool_calling_method,
        use_recorded_task,
        recorded_task_name
):
    try:
        global _global_browser, _global_browser_context, _global_playwright, _global_agent_state
        
        # Clear any previous stop request
        _global_agent_state.clear_stop()

        # Close any existing browser sessions
        await close_global_browser()
            
        # Initialize Playwright
        _global_playwright = await async_playwright().start()
        
        # Create browser instance with proper initialization
        _global_browser = CustomBrowser()
        browser_config = BrowserConfig(
            headless=headless,
            disable_security=disable_security,
            chrome_instance_path=os.getenv("CHROME_PATH") if use_own_browser else None
        )
        
        # Set config before launching
        _global_browser.config = browser_config
        
        # Launch browser based on settings
        if use_own_browser and browser_config.chrome_instance_path:
            # Use custom Chrome instance
            playwright_browser = await _global_browser._setup_browser_with_instance(_global_playwright)
        else:
            # Use standard browser launch
            playwright_browser = await _global_playwright.chromium.launch(
                headless=headless,
                args=['--disable-web-security'] if disable_security else None
            )
            
        # Set the browser instance
        _global_browser._browser = playwright_browser

        # Create context with proper window size configuration
        context_config = BrowserContextConfig()
        context_config.no_viewport = False
        context_config.browser_window_size = BrowserContextWindowSize(
            width=window_w,
            height=window_h
        )
        
        _global_browser_context = await _global_browser.new_context(config=context_config)

        # Create a page in the context
        await _global_browser_context.new_page()

        # Create agent with correct arguments
        llm = utils.get_llm_model(
            provider=llm_provider,
            model_name=llm_model_name,
            temperature=llm_temperature,
            base_url=llm_base_url,
            api_key=llm_api_key,
        )

        # Create agent based on type
        if agent_type == "custom":
            from src.agent.custom_prompts import CustomSystemPrompt, CustomAgentMessagePrompt
            from src.controller.custom_controller import CustomController
            
            controller = CustomController()
            
            # Load recorded task if specified
            if use_recorded_task and recorded_task_name:
                task_file = os.path.join("tmp", "record_videos", recorded_task_name)
                if os.path.exists(task_file):
                    with open(task_file, 'r') as f:
                        recorded_task_data = json.load(f)
                        task = f"Replay the following recorded task: {recorded_task_data.get('task_name', 'Unnamed Task')}"
                        add_infos = f"Follow these recorded steps: {json.dumps(recorded_task_data.get('steps', []))}"
            
            agent = CustomAgent(
                task=task,
                add_infos=add_infos,
                llm=llm,
                browser=_global_browser,
                browser_context=_global_browser_context,
                controller=controller,
                system_prompt_class=CustomSystemPrompt,
                agent_prompt_class=CustomAgentMessagePrompt,
                use_vision=use_vision,
                max_actions_per_step=max_actions_per_step
            )
        else:
            agent = Agent(
                task=task,
                llm=llm,
                browser_context=_global_browser_context,
                use_vision=use_vision,
                tool_calling_method=tool_calling_method
            )

        # Run the agent
        history = await agent.run(max_steps=max_steps)
        
        # Save agent history if path provided
        if save_agent_history_path:
            # Ensure the directory exists
            os.makedirs(os.path.dirname(save_agent_history_path), exist_ok=True)
            # Generate a unique filename using timestamp
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            history_file = os.path.join(save_agent_history_path, f"agent_history_{timestamp}.json")
            # Save history data
            history_data = {
                "final_result": history.final_result(),
                "errors": history.errors(),
                "model_actions": [action.model_dump() if hasattr(action, 'model_dump') else str(action) for action in history.model_actions()],  # type: ignore
                "model_thoughts": history.model_thoughts(),
                "agent_config": agent.to_dict() if hasattr(agent, 'to_dict') else None,  # type: ignore
            }
            with open(history_file, 'w') as f:
                json.dump(history_data, f, indent=2, default=str)

        # Handle cleanup based on persistence configuration
        if not keep_browser_open:
            await close_global_browser()

        return (
            history.final_result(),       # final_result
            history.errors(),             # errors
            history.model_actions(),      # model_actions
            history.model_thoughts(),     # model_thoughts
            None,                         # recording_display (none by default)
            None,                         # trace_file (none by default)
            None,                         # agent_history_file (none by default)
            gr.update(value="Stop", interactive=True),  # stop_button
            gr.update(interactive=True)                 # run_button
        )

    except Exception as e:
        logger.error(f"Error in run_browser_agent: {e}")
        import traceback
        errors = f"Error: {str(e)}\n{traceback.format_exc()}"
        return '', errors, '', '', None, None, None, None, None
    finally:
        # Handle cleanup based on persistence configuration
        if not keep_browser_open:
            if _global_browser_context:
                await _global_browser_context.close()
                _global_browser_context = None

            if _global_browser:
                await _global_browser.close()
                _global_browser = None

async def run_org_agent(
        llm,
        use_own_browser,
        keep_browser_open,
        headless,
        disable_security,
        window_w,
        window_h,
        save_recording_path,
        save_agent_history_path,
        save_trace_path,
        task,
        max_steps,
        use_vision,
        max_actions_per_step,
        tool_calling_method
):
    try:
        global _global_browser, _global_browser_context, _global_agent_state
        
        # Clear any previous stop request
        _global_agent_state.clear_stop()

        extra_chromium_args = [f"--window-size={window_w},{window_h}"]
        if use_own_browser:
            chrome_path = os.getenv("CHROME_PATH", None)
            if chrome_path == "":
                chrome_path = None
            chrome_user_data = os.getenv("CHROME_USER_DATA", None)
            if chrome_user_data:
                extra_chromium_args += [f"--user-data-dir={chrome_user_data}"]
        else:
            chrome_path = None
            
        if _global_browser is None:
            _global_browser = Browser(
                config=BrowserConfig(
                    headless=headless,
                    disable_security=disable_security,
                    chrome_instance_path=chrome_path,
                    extra_chromium_args=extra_chromium_args,
                )
            )

        if _global_browser_context is None:
            _global_browser_context = await _global_browser.new_context(
                config=BrowserContextConfig(
                    trace_path=save_trace_path if save_trace_path else None,
                    save_recording_path=save_recording_path if save_recording_path else None,
                    no_viewport=False,
                    browser_window_size=BrowserContextWindowSize(
                        width=window_w, height=window_h
                    ),
                )
            )
            
        agent = Agent(
            task=task,
            llm=llm,
            use_vision=use_vision,
            browser=_global_browser,
            browser_context=_global_browser_context,
            max_actions_per_step=max_actions_per_step,
            tool_calling_method=tool_calling_method
        )
        history = await agent.run(max_steps=max_steps)

        history_file = os.path.join(save_agent_history_path, f"{agent.agent_id}.json")
        agent.save_history(history_file)

        final_result = history.final_result()
        errors = history.errors()
        model_actions = history.model_actions()
        model_thoughts = history.model_thoughts()

        trace_file = get_latest_files(save_trace_path)

        return final_result, errors, model_actions, model_thoughts, trace_file.get('.zip'), history_file
    except Exception as e:
        import traceback
        traceback.print_exc()
        errors = str(e) + "\n" + traceback.format_exc()
        return '', errors, '', '', None, None
    finally:
        # Handle cleanup based on persistence configuration
        if not keep_browser_open:
            if _global_browser_context:
                await _global_browser_context.close()
                _global_browser_context = None

            if _global_browser:
                await _global_browser.close()
                _global_browser = None

async def run_custom_agent(
        llm,
        use_own_browser,
        keep_browser_open,
        headless,
        disable_security,
        window_w,
        window_h,
        save_recording_path,
        save_agent_history_path,
        save_trace_path,
        task,
        add_infos,
        max_steps,
        use_vision,
        max_actions_per_step,
        tool_calling_method
):
    try:
        global _global_browser, _global_browser_context, _global_agent_state

        # Clear any previous stop request
        _global_agent_state.clear_stop()

        extra_chromium_args = [f"--window-size={window_w},{window_h}"]
        if use_own_browser:
            chrome_path = os.getenv("CHROME_PATH", None)
            if chrome_path == "":
                chrome_path = None
            chrome_user_data = os.getenv("CHROME_USER_DATA", None)
            if chrome_user_data:
                extra_chromium_args += [f"--user-data-dir={chrome_user_data}"]
        else:
            chrome_path = None

        controller = CustomController()

        # Initialize global browser if needed
        if _global_browser is None:
            _global_browser = CustomBrowser(
                config=BrowserConfig(
                    headless=headless,
                    disable_security=disable_security,
                    chrome_instance_path=chrome_path,
                    extra_chromium_args=extra_chromium_args,
                )
            )

        if _global_browser_context is None:
            _global_browser_context = await _global_browser.new_context(
                config=BrowserContextConfig(
                    trace_path=save_trace_path if save_trace_path else None,
                    save_recording_path=save_recording_path if save_recording_path else None,
                    no_viewport=False,
                    browser_window_size=BrowserContextWindowSize(
                        width=window_w, height=window_h
                    ),
                )
            )
            
        # Create and run agent
        agent = CustomAgent(
            task=task,
            add_infos=add_infos,
            use_vision=use_vision,
            llm=llm,
            browser=_global_browser,
            browser_context=_global_browser_context,
            controller=controller,
            system_prompt_class=CustomSystemPrompt,
            agent_prompt_class=CustomAgentMessagePrompt,
            max_actions_per_step=max_actions_per_step,
            agent_state=_global_agent_state,
            tool_calling_method=tool_calling_method
        )
        history = await agent.run(max_steps=max_steps)

        history_file = os.path.join(save_agent_history_path, f"{agent.agent_id}.json")
        agent.save_history(history_file)

        final_result = history.final_result()
        errors = history.errors()
        model_actions = history.model_actions()
        model_thoughts = history.model_thoughts()

        trace_file = get_latest_files(save_trace_path)        

        return final_result, errors, model_actions, model_thoughts, trace_file.get('.zip'), history_file
    except Exception as e:
        import traceback
        traceback.print_exc()
        errors = str(e) + "\n" + traceback.format_exc()
        return '', errors, '', '', None, None
    finally:
        # Handle cleanup based on persistence configuration
        if not keep_browser_open:
            if _global_browser_context:
                await _global_browser_context.close()
                _global_browser_context = None

            if _global_browser:
                await _global_browser.close()
                _global_browser = None

async def run_with_stream(
    agent_type,
    llm_provider,
    llm_model_name,
    llm_temperature,
    llm_base_url,
    llm_api_key,
    use_own_browser,
    keep_browser_open,
    headless,
    disable_security,
    window_w,
    window_h,
    save_recording_path,
    save_agent_history_path,
    save_trace_path,
    enable_recording,
    task,
    add_infos,
    max_steps,
    use_vision,
    max_actions_per_step,
    tool_calling_method,
    use_recorded_task,
    recorded_task_dropdown
):
    global _global_agent_state
    stream_vw = 80
    stream_vh = int(80 * window_h // window_w)
    if not headless:
        result = await run_browser_agent(
            agent_type=agent_type,
            llm_provider=llm_provider,
            llm_model_name=llm_model_name,
            llm_temperature=llm_temperature,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            use_own_browser=use_own_browser,
            keep_browser_open=keep_browser_open,
            headless=headless,
            disable_security=disable_security,
            window_w=window_w,
            window_h=window_h,
            save_recording_path=save_recording_path,
            save_agent_history_path=save_agent_history_path,
            save_trace_path=save_trace_path,
            enable_recording=enable_recording,
            task=task,
            add_infos=add_infos,
            max_steps=max_steps,
            use_vision=use_vision,
            max_actions_per_step=max_actions_per_step,
            tool_calling_method=tool_calling_method,
            use_recorded_task=use_recorded_task,
            recorded_task_name=recorded_task_dropdown
        )
        # Add HTML content at the start of the result array
        html_content = f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Using browser...</h1>"
        yield [
            html_content,                # browser_view
            result[0],                   # final_result_output
            result[1],                   # errors_output
            result[2],                   # model_actions_output
            result[3],                   # model_thoughts_output
            result[4],                   # recording_display
            result[5],                   # trace_file
            result[6],                   # agent_history_file
            result[7],                   # stop_button
            result[8]                    # run_button
        ]
    else:
        try:
            _global_agent_state.clear_stop()
            # Run the browser agent in the background
            agent_task = asyncio.create_task(
                run_browser_agent(
                    agent_type=agent_type,
                    llm_provider=llm_provider,
                    llm_model_name=llm_model_name,
                    llm_temperature=llm_temperature,
                    llm_base_url=llm_base_url,
                    llm_api_key=llm_api_key,
                    use_own_browser=use_own_browser,
                    keep_browser_open=keep_browser_open,
                    headless=headless,
                    disable_security=disable_security,
                    window_w=window_w,
                    window_h=window_h,
                    save_recording_path=save_recording_path,
                    save_agent_history_path=save_agent_history_path,
                    save_trace_path=save_trace_path,
                    enable_recording=enable_recording,
                    task=task,
                    add_infos=add_infos,
                    max_steps=max_steps,
                    use_vision=use_vision,
                    max_actions_per_step=max_actions_per_step,
                    tool_calling_method=tool_calling_method,
                    use_recorded_task=use_recorded_task,
                    recorded_task_name=recorded_task_dropdown
                )
            )

            # Initialize values for streaming
            html_content = f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Using browser...</h1>"
            final_result = errors = model_actions = model_thoughts = ""
            recording_display = trace_file = agent_history_file = None
            stop_button = gr.update(value="Stop", interactive=True)
            run_button = gr.update(interactive=True)

            # Periodically update the stream while the agent task is running
            while not agent_task.done():
                try:
                    encoded_screenshot = await capture_screenshot(_global_browser_context)
                    if encoded_screenshot is not None:
                        html_content = f'<img src="data:image/jpeg;base64,{encoded_screenshot}" style="width:{stream_vw}vw; height:{stream_vh}vh ; border:1px solid #ccc;">'
                    else:
                        html_content = f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Waiting for browser session...</h1>"
                except Exception as e:
                    html_content = f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Waiting for browser session...</h1>"

                if _global_agent_state and _global_agent_state.is_stop_requested():
                    yield [
                        html_content,
                        final_result,
                        errors,
                        model_actions,
                        model_thoughts,
                        recording_display,
                        trace_file,
                        agent_history_file,
                        gr.update(value="Stopping...", interactive=False),  # stop_button
                        gr.update(interactive=False),  # run_button
                    ]
                    break
                else:
                    yield [
                        html_content,
                        final_result,
                        errors,
                        model_actions,
                        model_thoughts,
                        recording_display,
                        trace_file,
                        agent_history_file,
                        stop_button,
                        run_button
                    ]
                await asyncio.sleep(0.05)

            # Once the agent task completes, get the results
            try:
                result = await agent_task
                final_result = result[0]
                errors = result[1]
                model_actions = result[2]
                model_thoughts = result[3]
                recording_display = result[4]
                trace_file = result[5]
                agent_history_file = result[6]
                stop_button = result[7]
                run_button = result[8]
            except Exception as e:
                errors = f"Agent error: {str(e)}"

            yield [
                html_content,
                final_result,
                errors,
                model_actions,
                model_thoughts,
                recording_display,
                trace_file,
                agent_history_file,
                stop_button,
                run_button
            ]

        except Exception as e:
            import traceback
            yield [
                f"<h1 style='width:{stream_vw}vw; height:{stream_vh}vh'>Waiting for browser session...</h1>",
                "",
                f"Error: {str(e)}\n{traceback.format_exc()}",
                "",
                "",
                None,
                None,
                None,
                gr.update(value="Stop", interactive=True),  # stop_button
                gr.update(interactive=True)    # run_button
            ]

# Define the theme map globally
theme_map = {
    "Default": Default(),
    "Soft": Soft(),
    "Monochrome": Monochrome(),
    "Glass": Glass(),
    "Origin": Origin(),
    "Citrus": Citrus(),
    "Ocean": Ocean(),
    "Base": Base()
}

async def close_global_browser():
    """Ensure proper cleanup of browser resources"""
    global _global_browser, _global_browser_context, _global_playwright
    
    try:
        if _global_browser_context:
            try:
                await _global_browser_context.close()
            except Exception as e:
                logger.error(f"Error closing browser context: {e}")
            _global_browser_context = None

        if _global_browser:
            try:
                await _global_browser.close()
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
            _global_browser = None
            
        if _global_playwright:
            try:
                await _global_playwright.stop()
            except Exception as e:
                logger.error(f"Error stopping playwright: {e}")
            _global_playwright = None
            
    except Exception as e:
        logger.error(f"Error in close_global_browser: {e}")

async def initialize_browser_for_recording(use_own_browser: bool) -> str:
    """Initialize browser for human task recording"""
    global _global_browser, _global_browser_context, _global_playwright
    
    try:
        # If browser exists but was created with different settings, close it
        if _global_browser:
            await close_global_browser()
            
        # Setup browser with recording-appropriate settings
        window_w = 1280  # Default width
        window_h = 720   # Default height
        
        # Setup browser config
        extra_chromium_args = []
        if use_own_browser:
            chrome_path = os.getenv("CHROME_PATH", None)
            if chrome_path == "":
                chrome_path = None
            chrome_user_data = os.getenv("CHROME_USER_DATA", None)
            if chrome_user_data:
                extra_chromium_args.append(f"--user-data-dir={chrome_user_data}")
            extra_chromium_args.append(f"--window-size={window_w},{window_h}")
        
        browser_config = BrowserConfig(
            headless=False,  # Always show browser for human interaction
            disable_security=False,  # Keep security enabled for human browsing
            chrome_instance_path=chrome_path if use_own_browser else None,
            extra_chromium_args=extra_chromium_args
        )

        # Initialize Playwright
        _global_playwright = await async_playwright().start()
        
        # Create browser instance
        _global_browser = CustomBrowser(config=browser_config)
        
        # Create context with proper window size configuration
        context_config = BrowserContextConfig()
        context_config.no_viewport = False
        context_config.browser_window_size = BrowserContextWindowSize(
            width=window_w,
            height=window_h
        )
        
        _global_browser_context = await _global_browser.new_context(config=context_config)

        # Create initial page
        await _global_browser_context.new_page()
        
        return "Browser initialized successfully"
        
    except Exception as e:
        logger.error(f"Error initializing browser: {e}")
        return f"Error initializing browser: {str(e)}"

# Modify the utility function to just return the list
def get_saved_tasks():
    """List all saved task recordings"""
    tasks_dir = os.path.join("tmp", "record_videos")
    if not os.path.exists(tasks_dir):
        return []
    
    tasks = []
    for filename in os.listdir(tasks_dir):
        if filename.endswith('.json'):
            tasks.append(filename)
    return tasks

# Add this near the top with other utility functions
def update_task_dropdowns():
    """Update both task dropdowns with current list of tasks"""
    tasks = get_saved_tasks()
    return {
        saved_tasks_dropdown: gr.update(choices=tasks),
        recorded_task_dropdown: gr.update(choices=tasks)
    }

def create_ui(config, theme_name="Ocean"):
    global _global_task_recorder, saved_tasks_dropdown, recorded_task_dropdown
    
    # Initialize the task recorder with the same path as other recordings
    _global_task_recorder = TaskRecorder(save_dir=config['save_recording_path'])

    css = """
    .gradio-container {
        max-width: 1200px !important;
        margin: auto !important;
        padding-top: 20px !important;
    }
    .header-text {
        text-align: center;
        margin-bottom: 30px;
    }
    .theme-section {
        margin-bottom: 20px;
        padding: 15px;
        border-radius: 10px;
    }
    """

    js = """
    function refresh() {
        const url = new URL(window.location);
        if (url.searchParams.get('__theme') !== 'dark') {
            url.searchParams.set('__theme', 'dark');
            window.location.href = url.href;
        }
    }
    """

    with gr.Blocks(
            title="Browser Use WebUI", theme=theme_map[theme_name], css=css, js=js
    ) as demo:
        with gr.Row():
            gr.Markdown(
                """
                # 🌐 Browser Use WebUI
                ### Control your browser with AI assistance
                """,
                elem_classes=["header-text"],
            )

        with gr.Tabs() as tabs:
            with gr.TabItem("⚙️ Agent Settings", id=1):
                with gr.Group():
                    agent_type = gr.Radio(
                        ["org", "custom"],
                        label="Agent Type",
                        value=config['agent_type'],
                        info="Select the type of agent to use",
                    )
                    with gr.Column():
                        max_steps = gr.Slider(
                            minimum=1,
                            maximum=200,
                            value=config['max_steps'],
                            step=1,
                            label="Max Run Steps",
                            info="Maximum number of steps the agent will take",
                        )
                        max_actions_per_step = gr.Slider(
                            minimum=1,
                            maximum=20,
                            value=config['max_actions_per_step'],
                            step=1,
                            label="Max Actions per Step",
                            info="Maximum number of actions the agent will take per step",
                        )
                    with gr.Column():
                        use_vision = gr.Checkbox(
                            label="Use Vision",
                            value=config['use_vision'],
                            info="Enable visual processing capabilities",
                        )
                        tool_calling_method = gr.Dropdown(
                            label="Tool Calling Method",
                            value=config['tool_calling_method'],
                            interactive=True,
                            allow_custom_value=True,  # Allow users to input custom model names
                            choices=["auto", "json_schema", "function_calling"],
                            info="Tool Calls Funtion Name",
                            visible=False
                        )

            with gr.TabItem("🔧 LLM Configuration", id=2):
                with gr.Group():
                    llm_provider = gr.Dropdown(
                        choices=[provider for provider,model in utils.model_names.items()],
                        label="LLM Provider",
                        value=config['llm_provider'],
                        info="Select your preferred language model provider"
                    )
                    llm_model_name = gr.Dropdown(
                        label="Model Name",
                        choices=utils.model_names['openai'],
                        value=config['llm_model_name'],
                        interactive=True,
                        allow_custom_value=True,  # Allow users to input custom model names
                        info="Select a model from the dropdown or type a custom model name"
                    )
                    llm_temperature = gr.Slider(
                        minimum=0.0,
                        maximum=2.0,
                        value=config['llm_temperature'],
                        step=0.1,
                        label="Temperature",
                        info="Controls randomness in model outputs"
                    )
                    with gr.Row():
                        llm_base_url = gr.Textbox(
                            label="Base URL",
                            value=config['llm_base_url'],
                            info="API endpoint URL (if required)"
                        )
                        llm_api_key = gr.Textbox(
                            label="API Key",
                            type="password",
                            value=config['llm_api_key'],
                            info="Your API key (leave blank to use .env)"
                        )

            with gr.TabItem("🌐 Browser Settings", id=3):
                with gr.Group():
                    with gr.Row():
                        use_own_browser = gr.Checkbox(
                            label="Use Own Browser",
                            value=config['use_own_browser'],
                            info="Use your existing browser instance",
                        )
                        keep_browser_open = gr.Checkbox(
                            label="Keep Browser Open",
                            value=config['keep_browser_open'],
                            info="Keep Browser Open between Tasks",
                        )
                        headless = gr.Checkbox(
                            label="Headless Mode",
                            value=config['headless'],
                            info="Run browser without GUI",
                        )
                        disable_security = gr.Checkbox(
                            label="Disable Security",
                            value=config['disable_security'],
                            info="Disable browser security features",
                        )
                        enable_recording = gr.Checkbox(
                            label="Enable Recording",
                            value=config['enable_recording'],
                            info="Enable saving browser recordings",
                        )

                    with gr.Row():
                        window_w = gr.Number(
                            label="Window Width",
                            value=config['window_w'],
                            info="Browser window width",
                        )
                        window_h = gr.Number(
                            label="Window Height",
                            value=config['window_h'],
                            info="Browser window height",
                        )

                    save_recording_path = gr.Textbox(
                        label="Recording Path",
                        placeholder="e.g. ./tmp/record_videos",
                        value=config['save_recording_path'],
                        info="Path to save browser recordings",
                        interactive=True,  # Allow editing only if recording is enabled
                    )

                    save_trace_path = gr.Textbox(
                        label="Trace Path",
                        placeholder="e.g. ./tmp/traces",
                        value=config['save_trace_path'],
                        info="Path to save Agent traces",
                        interactive=True,
                    )

                    save_agent_history_path = gr.Textbox(
                        label="Agent History Save Path",
                        placeholder="e.g., ./tmp/agent_history",
                        value=config['save_agent_history_path'],
                        info="Specify the directory where agent history should be saved.",
                        interactive=True,
                    )

            with gr.TabItem("🤖 Run Agent", id=4):
                with gr.Group():
                    with gr.Row():
                        use_recorded_task = gr.Checkbox(
                            label="Use Recorded Task",
                            value=False,
                            info="Execute a previously recorded task"
                        )
                        recorded_task_dropdown = gr.Dropdown(
                            label="Select Task",
                            choices=[],
                            interactive=True,
                            visible=False
                        )

                    task = gr.Textbox(
                        label="Task Description",
                        lines=4,
                        placeholder="Enter your task here...",
                        value=config['task'],
                        info="Describe what you want the agent to do",
                    )

                    def update_task_inputs(use_recorded: bool):
                        if use_recorded:
                            return {
                                recorded_task_dropdown: gr.update(visible=True),
                                task: gr.update(
                                    interactive=False,
                                    placeholder="Task will be loaded from recording...",
                                    value=""
                                )
                            }
                        else:
                            return {
                                recorded_task_dropdown: gr.update(visible=False),
                                task: gr.update(
                                    interactive=True,
                                    placeholder="Enter your task here...",
                                    value=config['task']
                                )
                            }

                    use_recorded_task.change(
                        fn=update_task_inputs,
                        inputs=[use_recorded_task],
                        outputs=[recorded_task_dropdown, task]
                    )

                add_infos = gr.Textbox(
                    label="Additional Information",
                    lines=3,
                    placeholder="Add any helpful context or instructions...",
                    info="Optional hints to help the LLM complete the task",
                )

                with gr.Row():
                    run_button = gr.Button("▶️ Run Agent", variant="primary", scale=2)
                    stop_button = gr.Button("⏹️ Stop", variant="stop", scale=1)
                    
                with gr.Row():
                    browser_view = gr.HTML(
                        value="<h1 style='width:80vw; height:50vh'>Waiting for browser session...</h1>",
                        label="Live Browser View",
                )

            with gr.TabItem("📹 Task Recording", id=8):
                with gr.Group():
                    gr.Markdown("""
                        ## Task Recording
                        Record browser interactions to create reusable task templates.
                        The agent can later replay these recorded tasks.
                    """)
                    
                    with gr.Row():
                        init_browser_btn = gr.Button("🌐 Initialize Browser", variant="secondary")
                        
                    with gr.Row():
                        task_name = gr.Textbox(
                            label="Task Name",
                            placeholder="Enter a name for this task...",
                            info="This name will be used to save and identify the recorded task"
                        )
                        
                    with gr.Row():
                        start_recording_btn = gr.Button("▶️ Start Recording", variant="primary")
                        stop_recording_btn = gr.Button("⏹️ Stop Recording", variant="stop")
                        stop_recording_btn.visible = False
                        
                    with gr.Row():
                        recording_status = gr.Textbox(
                            label="Status",
                            value="Not recording",
                            interactive=False
                        )
                        
                    with gr.Row():
                        recorded_steps = gr.JSON(
                            label="Recorded Steps",
                            value=[],
                            visible=True
                        )

                    with gr.Row():
                        saved_tasks_dropdown = gr.Dropdown(
                            label="Saved Tasks",
                            choices=[], # Will be populated on load
                            info="Select a previously recorded task",
                            interactive=True
                        )
                        refresh_tasks_btn = gr.Button("🔄 Refresh Tasks", variant="secondary")
                        
                    with gr.Row():
                        load_task_btn = gr.Button("📂 Load Task", variant="secondary")
                        delete_task_btn = gr.Button("🗑️ Delete Task", variant="secondary", visible=True)

                    async def on_init_browser():
                        # Get browser settings from config
                        use_own = config.get('use_own_browser', False)
                        result = await initialize_browser_for_recording(use_own)
                        return {
                            recording_status: result,
                            init_browser_btn: gr.update(interactive=True)
                        }

                    init_browser_btn.click(
                        fn=on_init_browser,
                        outputs=[recording_status, init_browser_btn]
                    )

                    async def on_start_recording(task_name):
                        global _global_browser_context, _global_task_recorder
                        assert _global_task_recorder is not None, "Task recorder is not initialized"
                        if not task_name.strip():
                            return {
                                recording_status: "Error: Please enter a task name first",
                                start_recording_btn: gr.update(interactive=True),
                                stop_recording_btn: gr.update(visible=False),
                                recorded_steps: []
                            }
                        if not _global_browser_context:
                            return {
                                recording_status: "Error: Browser not initialized. Please start the browser first.",
                                start_recording_btn: gr.update(interactive=True),
                                stop_recording_btn: gr.update(visible=False),
                                recorded_steps: []
                            }
                        try:
                            _global_task_recorder.start_recording(task_name)
                            asyncio.create_task(_global_task_recorder.attach_to_browser(_global_browser_context))  # type: ignore
                            return {
                                recording_status: "Recording started...",
                                start_recording_btn: gr.update(visible=False),
                                stop_recording_btn: gr.update(visible=True),
                                recorded_steps: []
                            }
                        except Exception as e:
                            return {
                                recording_status: f"Error starting recording: {str(e)}",
                                start_recording_btn: gr.update(interactive=True),
                                stop_recording_btn: gr.update(visible=False),
                                recorded_steps: []
                            }

                    async def on_stop_recording():
                        global _global_task_recorder
                        assert _global_task_recorder is not None, "Task recorder is not initialized"
                        try:
                            steps = _global_task_recorder.stop_recording()  # type: ignore
                            filepath = _global_task_recorder.save_recording()  # type: ignore
                            tasks = get_saved_tasks()
                            return {
                                recording_status: "Recording stopped and saved",
                                start_recording_btn: gr.update(visible=True),
                                stop_recording_btn: gr.update(visible=False),
                                recorded_steps: steps,
                                saved_tasks_dropdown: gr.update(choices=tasks),
                                recorded_task_dropdown: gr.update(choices=tasks)
                            }
                        except Exception as e:
                            return {
                                recording_status: f"Error saving recording: {str(e)}",
                                start_recording_btn: gr.update(visible=True),
                                stop_recording_btn: gr.update(visible=False),
                                recorded_steps: []
                            }

                    # Update the click handlers to include recorded_steps output
                    start_recording_btn.click(
                        fn=on_start_recording,
                        inputs=[task_name],
                        outputs=[recording_status, start_recording_btn, stop_recording_btn, recorded_steps]
                    )
                    
                    # Update the stop_recording_btn click handler to include both dropdowns in outputs
                    stop_recording_btn.click(
                        fn=on_stop_recording,
                        outputs=[
                            recording_status,
                            start_recording_btn,
                            stop_recording_btn,
                            recorded_steps,
                            saved_tasks_dropdown,     # Add these two
                            recorded_task_dropdown    # dropdown outputs
                        ]
                    )

                    # Wire up the refresh button click handler
                    refresh_tasks_btn.click(
                        fn=update_task_dropdowns,
                        outputs=[saved_tasks_dropdown, recorded_task_dropdown]
                    )

            with gr.TabItem("📁 Configuration", id=5):
                with gr.Group():
                    config_file_input = gr.File(
                        label="Load Config File",
                        file_types=[".pkl"],
                        interactive=True
                    )

                    load_config_button = gr.Button("Load Existing Config From File", variant="primary")
                    save_config_button = gr.Button("Save Current Config", variant="primary")

                    config_status = gr.Textbox(
                        label="Status",
                        lines=2,
                        interactive=False
                    )

                load_config_button.click(
                    fn=update_ui_from_config,
                    inputs=[config_file_input],
                    outputs=[
                        agent_type, max_steps, max_actions_per_step, use_vision, tool_calling_method,
                        llm_provider, llm_model_name, llm_temperature, llm_base_url, llm_api_key,
                        use_own_browser, keep_browser_open, headless, disable_security, enable_recording,
                        window_w, window_h, save_recording_path, save_trace_path, save_agent_history_path,
                        task, config_status
                    ]
                )

                save_config_button.click(
                    fn=save_current_config,
                    inputs=[
                        agent_type, max_steps, max_actions_per_step, use_vision, tool_calling_method,
                        llm_provider, llm_model_name, llm_temperature, llm_base_url, llm_api_key,
                        use_own_browser, keep_browser_open, headless, disable_security,
                        enable_recording, window_w, window_h, save_recording_path, save_trace_path,
                        save_agent_history_path, task,
                    ],  
                    outputs=[config_status]
                )

            with gr.TabItem("📊 Results", id=6):
                with gr.Group():

                    recording_display = gr.Video(label="Latest Recording")

                    gr.Markdown("### Results")
                    with gr.Row():
                        with gr.Column():
                            final_result_output = gr.Textbox(
                                label="Final Result", lines=3, show_label=True
                            )
                        with gr.Column():
                            errors_output = gr.Textbox(
                                label="Errors", lines=3, show_label=True
                            )
                    with gr.Row():
                        with gr.Column():
                            model_actions_output = gr.Textbox(
                                label="Model Actions", lines=3, show_label=True
                            )
                        with gr.Column():
                            model_thoughts_output = gr.Textbox(
                                label="Model Thoughts", lines=3, show_label=True
                            )

                    trace_file = gr.File(label="Trace File")

                    agent_history_file = gr.File(label="Agent History")

                # Bind the stop button click event after errors_output is defined
                stop_button.click(
                    fn=stop_agent,
                    inputs=[],
                    outputs=[errors_output, stop_button, run_button],
                )

                # Run button click handler
                run_button.click(
                    fn=run_with_stream,
                    inputs=[
                        agent_type, llm_provider, llm_model_name, llm_temperature, llm_base_url, llm_api_key,
                        use_own_browser, keep_browser_open, headless, disable_security, window_w, window_h,
                        save_recording_path, save_agent_history_path, save_trace_path,
                        enable_recording, task, add_infos, max_steps, use_vision, max_actions_per_step, tool_calling_method,
                        use_recorded_task, recorded_task_dropdown
                    ],
                    outputs=[
                        browser_view,           
                        final_result_output,    
                        errors_output,          
                        model_actions_output,   
                        model_thoughts_output,  
                        recording_display,      
                        trace_file,             
                        agent_history_file,     
                        stop_button,            
                        run_button              
                    ],
                )

            with gr.TabItem("🎥 Recordings", id=7):
                def list_recordings(save_recording_path):
                    if not os.path.exists(save_recording_path):
                        return []

                    # Get all video files
                    recordings = glob.glob(os.path.join(save_recording_path, "*.[mM][pP]4")) + glob.glob(os.path.join(save_recording_path, "*.[wW][eE][bB][mM]"))

                    # Sort recordings by creation time (oldest first)
                    recordings.sort(key=os.path.getctime)

                    # Add numbering to the recordings
                    numbered_recordings = []
                    for idx, recording in enumerate(recordings, start=1):
                        filename = os.path.basename(recording)
                        numbered_recordings.append((recording, f"{idx}. {filename}"))

                    return numbered_recordings

                recordings_gallery = gr.Gallery(
                    label="Recordings",
                    value=list_recordings(config['save_recording_path']),
                    columns=3,
                    height="auto",
                    object_fit="contain"
                )

                refresh_button = gr.Button("🔄 Refresh Recordings", variant="secondary")
                refresh_button.click(
                    fn=list_recordings,
                    inputs=save_recording_path,
                    outputs=recordings_gallery
                )

        # Attach the callback to the LLM provider dropdown
        llm_provider.change(
            lambda provider, api_key, base_url: update_model_dropdown(provider, api_key, base_url),
            inputs=[llm_provider, llm_api_key, llm_base_url],
            outputs=llm_model_name
        )

        # Add this after defining the components
        enable_recording.change(
            lambda enabled: gr.update(interactive=enabled),
            inputs=enable_recording,
            outputs=save_recording_path
        )

        use_own_browser.change(fn=close_global_browser)
        keep_browser_open.change(fn=close_global_browser)

    # Initial population of dropdowns
    initial_tasks = get_saved_tasks()
    saved_tasks_dropdown.choices = initial_tasks
    recorded_task_dropdown.choices = initial_tasks

    return demo

def main():
    global saved_tasks_dropdown, recorded_task_dropdown
    
    parser = argparse.ArgumentParser(description="Gradio UI for Browser Agent")
    parser.add_argument("--ip", type=str, default="127.0.0.1", help="IP address to bind to")
    parser.add_argument("--port", type=int, default=7788, help="Port to listen on")
    parser.add_argument("--theme", type=str, default="Ocean", choices=theme_map.keys(), help="Theme to use for the UI")
    parser.add_argument("--dark-mode", action="store_true", help="Enable dark mode")
    args = parser.parse_args()

    config_dict = default_config()

    demo = create_ui(config_dict, theme_name=args.theme)
    
    demo.launch(server_name=args.ip, server_port=args.port)

if __name__ == '__main__':
    main()
