from __future__ import annotations
import json
import logging
import pdb
import traceback
from typing import Optional, Type, List, Dict, Any, Callable, Union, TypeAlias, Sequence, cast
from PIL import Image, ImageDraw, ImageFont
from PIL.ImageFont import FreeTypeFont, ImageFont as PILImageFont
import os
import base64
import io
import platform
from browser_use.agent.prompts import SystemPrompt, AgentMessagePrompt
from browser_use.agent.service import Agent
from browser_use.agent.views import (
    ActionResult,
    ActionModel,
    AgentHistoryList,
    AgentOutput,
    AgentHistory,
    AgentBrain,
)
from browser_use.browser.browser import Browser
from browser_use.browser.context import BrowserContext
from browser_use.browser.views import BrowserStateHistory, BrowserState as BaseBrowserState
from browser_use.controller.service import Controller
from browser_use.telemetry.views import (
	AgentEndTelemetryEvent,
	AgentRunTelemetryEvent,
	AgentStepTelemetryEvent,
)
from browser_use.utils import time_execution_async
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
)
from json_repair import repair_json
from src.utils.agent_state import AgentState
from src.browser.custom_context import BrowserState as CustomBrowserState

from .custom_massage_manager import CustomMassageManager
from .custom_views import CustomAgentOutput, CustomAgentStepInfo, CustomAgentBrain
from .custom_prompts import CustomSystemPrompt, CustomAgentMessagePrompt

logger = logging.getLogger(__name__)

# Type alias to handle both browser state types
BrowserState: TypeAlias = Union[BaseBrowserState, CustomBrowserState]

# Type alias for font types that includes both FreeType and PIL fonts
Font = Union[FreeTypeFont, PILImageFont]

# Type alias for action types
ActionType = Union[Dict[str, Any], ActionModel]

class CustomAgent(Agent):
    def __init__(
            self,
            task: str,
            llm: BaseChatModel,
            add_infos: str = "",
            browser: Optional[Browser] = None,
            browser_context: Optional[BrowserContext] = None,
            controller: Controller = Controller(),
            use_vision: bool = True,
            save_conversation_path: Optional[str] = None,
            max_failures: int = 5,
            retry_delay: int = 10,
            system_prompt_class: Type[SystemPrompt] = SystemPrompt,
            agent_prompt_class: Type[AgentMessagePrompt] = AgentMessagePrompt,
            max_input_tokens: int = 128000,
            validate_output: bool = False,
            include_attributes: list[str] = [
                "title",
                "type",
                "name",
                "role",
                "tabindex",
                "aria-label",
                "placeholder",
                "value",
                "alt",
                "aria-expanded",
            ],
            max_error_length: int = 400,
            max_actions_per_step: int = 10,
            tool_call_in_content: bool = True,
            agent_state: Optional[AgentState] = None,
            initial_actions: Optional[List[Dict[str, Dict[str, Any]]]] = None,
            register_new_step_callback: Optional[Callable[[BrowserState, AgentOutput, int], None]] = None,
            register_done_callback: Optional[Callable[[AgentHistoryList], None]] = None,
            tool_calling_method: Optional[str] = 'auto',
    ):
        # Initialize base agent
        super().__init__(
            task=task,
            llm=llm,
            browser=browser,
            browser_context=browser_context,
            controller=controller,
            use_vision=use_vision,
            save_conversation_path=save_conversation_path,
            max_failures=max_failures,
            retry_delay=retry_delay,
            system_prompt_class=system_prompt_class,
            max_input_tokens=max_input_tokens,
            validate_output=validate_output,
            include_attributes=include_attributes,
            max_error_length=max_error_length,
            max_actions_per_step=max_actions_per_step,
            tool_call_in_content=tool_call_in_content,
            initial_actions=initial_actions,
            register_new_step_callback=register_new_step_callback,
            register_done_callback=register_done_callback,
            tool_calling_method=tool_calling_method
        )

        # Parse recorded task if provided
        self.recorded_steps: List[Dict[str, Any]] = []
        if add_infos:
            try:
                # Handle case where add_infos is already a string of a list
                if isinstance(add_infos, str) and add_infos.startswith("Follow these recorded steps: "):
                    # Extract the JSON list part
                    json_str = add_infos.replace("Follow these recorded steps: ", "").strip()
                    try:
                        self.recorded_steps = json.loads(json_str)
                        task = "Execute recorded browser steps"
                    except json.JSONDecodeError:
                        logger.warning("Failed to parse recorded steps from task string")
                        self.recorded_steps = []
                else:
                    # Try parsing as JSON object first
                    try:
                        task_data = json.loads(add_infos)
                        if isinstance(task_data, dict):
                            if "steps" in task_data:
                                self.recorded_steps = task_data["steps"]
                                # Update task with recorded task name if available
                                if "task_name" in task_data:
                                    task = f"Execute recorded task: {task_data['task_name']}"
                                else:
                                    task = "Execute recorded browser steps"
                        elif isinstance(task_data, list):
                            # Direct array of steps
                            self.recorded_steps = task_data
                            task = "Execute recorded browser steps"
                    except json.JSONDecodeError:
                        # If JSON parsing fails, treat as raw task description
                        logger.warning(f"Failed to parse recorded steps as JSON, using as raw task")
                        task = add_infos
                        self.recorded_steps = []
            except Exception as e:
                logger.warning(f"Error processing recorded steps: {e}")
                task = add_infos
                self.recorded_steps = []

        if self.model_name in ["deepseek-reasoner"] or "deepseek-r1" in self.model_name:
            self.use_deepseek_r1 = True
            self.max_input_tokens = 64000
        else:
            self.use_deepseek_r1 = False
        
        self._last_actions: Optional[Sequence[ActionModel]] = None
        self.add_infos = add_infos
        self.agent_state = agent_state
        self.agent_prompt_class = agent_prompt_class
        
        # Initialize message manager with task info
        self.message_manager = CustomMassageManager(
            llm=self.llm,
            task=self.task,
            action_descriptions=self.controller.registry.get_prompt_description(),
            system_prompt_class=self.system_prompt_class,
            agent_prompt_class=agent_prompt_class,
            max_input_tokens=self.max_input_tokens,
            include_attributes=self.include_attributes,
            max_error_length=self.max_error_length,
            max_actions_per_step=self.max_actions_per_step
        )

    def _setup_action_models(self) -> None:
        """Setup dynamic action models from controller's registry"""
        # Get the dynamic action model from controller's registry
        self.ActionModel = self.controller.registry.create_action_model()
        # Create output model with the dynamic actions
        self.AgentOutput = CustomAgentOutput.type_with_custom_actions(self.ActionModel)

    def _log_response(self, response: CustomAgentOutput) -> None:
        """Log the model's response"""
        if "Success" in response.current_state.prev_action_evaluation:
            emoji = "✅"
        elif "Failed" in response.current_state.prev_action_evaluation:
            emoji = "❌"
        else:
            emoji = "🤷"

        logger.info(f"{emoji} Eval: {response.current_state.prev_action_evaluation}")
        logger.info(f"🧠 New Memory: {response.current_state.important_contents}")
        logger.info(f"⏳ Task Progress: \n{response.current_state.task_progress}")
        logger.info(f"📋 Future Plans: \n{response.current_state.future_plans}")
        logger.info(f"🤔 Thought: {response.current_state.thought}")
        logger.info(f"🎯 Summary: {response.current_state.summary}")
        for i, action in enumerate(response.action):
            logger.info(
                f"🛠️  Action {i + 1}/{len(response.action)}: {action.model_dump_json(exclude_unset=True)}"
            )

    def update_step_info(
            self, 
            model_output: CustomAgentOutput, 
            step_info: Optional[CustomAgentStepInfo] = None
    ) -> None:
        """Update step info with model output"""
        if step_info is None:
            return

        step_info.step_number += 1
        important_contents = model_output.current_state.important_contents
        if (
                important_contents
                and "None" not in important_contents
                and important_contents not in step_info.memory
        ):
            step_info.memory += important_contents + "\n"

        task_progress = model_output.current_state.task_progress
        if task_progress and "None" not in task_progress:
            step_info.task_progress = task_progress

        future_plans = model_output.current_state.future_plans
        if future_plans and "None" not in future_plans:
            step_info.future_plans = future_plans

    def _create_task_frame(
        self,
        task: str,
        screenshot: Image.Image,
        title_font: Font,
        regular_font: Font,
        logo: Optional[Image.Image] = None,
        line_spacing: float = 1.5,
    ) -> Image.Image:
        """Create a frame showing the task description"""
        # Create new image with same width as screenshot and extra height for text
        extra_height = 200  # Adjust based on text length
        img = Image.new(
            'RGB',
            (screenshot.width, screenshot.height + extra_height),
            color='white'
        )
        
        # Paste screenshot at bottom
        img.paste(screenshot, (0, extra_height))
        
        # Add text
        draw = ImageDraw.Draw(img)
        
        # Add logo if provided
        if logo:
            # Calculate logo position (centered horizontally, near top)
            logo_x = (img.width - logo.width) // 2
            logo_y = 10
            img.paste(logo, (logo_x, logo_y), logo)
            
        # Add task text
        margin = 40
        y = extra_height - 100  # Position text above screenshot
        
        # Draw task text
        lines = []
        words = task.split()
        current_line = []
        
        for word in words:
            current_line.append(word)
            # Check if current line is too long
            line = ' '.join(current_line)
            if draw.textlength(line, font=regular_font) > img.width - 2 * margin:
                # Remove last word and add line
                current_line.pop()
                lines.append(' '.join(current_line))
                current_line = [word]
        
        # Add any remaining words
        if current_line:
            lines.append(' '.join(current_line))
            
        # Draw lines
        font_height = getattr(regular_font, "size", 40)  # Default to 40 if size not available
        for line in lines:
            draw.text((margin, y), line, font=regular_font, fill='black')
            y += int(font_height * line_spacing)
            
        return img

    def _add_overlay_to_image(
        self,
        image: Image.Image,
        step_number: int,
        goal_text: str,
        regular_font: Font,
        title_font: Font,
        margin: int = 40,
        logo: Optional[Image.Image] = None,
    ) -> Image.Image:
        """Add an overlay with step number and goal text to an image"""
        # Create a copy of the image
        img = image.copy()
        draw = ImageDraw.Draw(img)
        
        # Add logo if provided
        if logo:
            # Calculate logo position (top right corner)
            logo_x = img.width - logo.width - margin
            logo_y = margin
            img.paste(logo, (logo_x, logo_y), logo)
        
        # Add step number
        step_text = f"Step {step_number}"
        draw.text((margin, margin), step_text, font=title_font, fill='black')
        
        # Add goal text below step number
        title_height = getattr(title_font, "size", 56)  # Default to 56 if size not available
        y = margin + title_height + 10
        
        # Wrap text to fit width
        max_width = img.width - 2 * margin
        lines = []
        words = goal_text.split()
        current_line = []
        
        for word in words:
            current_line.append(word)
            # Check if current line is too long
            line = ' '.join(current_line)
            if draw.textlength(line, font=regular_font) > max_width:
                # Remove last word and add line
                current_line.pop()
                lines.append(' '.join(current_line))
                current_line = [word]
        
        # Add any remaining words
        if current_line:
            lines.append(' '.join(current_line))
            
        # Draw lines
        font_height = getattr(regular_font, "size", 40)  # Default to 40 if size not available
        for line in lines:
            draw.text((margin, y), line, font=regular_font, fill='black')
            y += int(font_height * 1.5)
            
        return img

    @time_execution_async("--get_next_action")
    async def get_next_action(self, input_messages: list[BaseMessage]) -> CustomAgentOutput:
        """Get next action from LLM based on current state"""
        if self.recorded_steps and self.n_steps <= len(self.recorded_steps):
            try:
                # Get the current recorded step (1-based indexing)
                current_step = self.recorded_steps[self.n_steps - 1]
                # Create an action based on the recorded step
                action = None
                if current_step["action"] == "navigate":
                    action = {"go_to_url": {"url": current_step["url"]}}
                elif current_step["action"] == "input":
                    action = {"input_text": {
                        "text": current_step["value"],
                        "selector": current_step.get("selector", ""),
                        "xpath": "", 
                        "index": 0
                    }}
                elif current_step["action"] == "click":
                    selector = current_step.get("selector", "")
                    text = current_step.get("text", "")
                    action = {"click_element": {
                        "selector": selector,
                        "text": text,
                        "xpath": "",
                        "index": 0
                    }}
                
                if action:
                    action_model = self.ActionModel(**action)
                    self.n_steps += 1
                    return CustomAgentOutput(
                        current_state=CustomAgentBrain(
                            prev_action_evaluation="Success - Using recorded steps",
                            important_contents=f"Step {self.n_steps} of {len(self.recorded_steps)}",
                            task_progress=f"Executing recorded step {self.n_steps}/{len(self.recorded_steps)}",
                            future_plans="Will continue executing recorded steps in sequence",
                            thought=f"Following recorded step: {current_step['action']}",
                            summary=f"Step {self.n_steps}: {current_step['action'].title()} action"
                        ),
                        action=[action_model]
                    )
            except Exception as e:
                logger.error(f"Error processing recorded step: {e}")
                # Fall through to LLM if there's an error processing the recorded step

        if self.recorded_steps and self.n_steps > len(self.recorded_steps):
            return CustomAgentOutput(
                current_state=CustomAgentBrain(
                    prev_action_evaluation="Task completed successfully using recorded steps",
                    important_contents=f"All recorded steps ({len(self.recorded_steps)}) completed",
                    task_progress="Task complete",
                    future_plans="",
                    thought="All steps executed",
                    summary="Task finished"
                ),
                action=[]
            )

        messages_to_process = (
            self.message_manager.merge_successive_human_messages(input_messages)
            if self.use_deepseek_r1
            else input_messages
        )

        ai_message = self.llm.invoke(messages_to_process)
        self.message_manager._add_message_with_tokens(ai_message)

        if self.use_deepseek_r1 and hasattr(ai_message, 'reasoning_content'):
            logger.info("🤯 Start Deep Thinking: ")
            logger.info(getattr(ai_message, 'reasoning_content', ''))
            logger.info("🤯 End Deep Thinking")

        content = ai_message.content
        if isinstance(content, list):
            content = content[0]
        if isinstance(content, str):
            content = content.replace("```json", "").replace("```", "")
            content = repair_json(content)
            parsed_json = json.loads(str(content))  # Ensure content is str
            parsed: CustomAgentOutput = self.AgentOutput(**parsed_json)
            
            if parsed is None:
                logger.debug(ai_message.content)
                raise ValueError('Could not parse response.')

            # Limit actions to maximum allowed per step
            actions = []
            for action in parsed.action[: self.max_actions_per_step]:
                if isinstance(action, dict):
                    # Ensure required fields are present with proper types
                    if "input_text" in action:
                        action["input_text"]["index"] = action["input_text"].get("index", 0)  # Default to first element
                        action["input_text"]["xpath"] = action["input_text"].get("xpath", "")  # Empty string instead of None
                    elif "click_element" in action:
                        action["click_element"]["index"] = action["click_element"].get("index", 0)  # Default to first element
                        action["click_element"]["xpath"] = action["click_element"].get("xpath", "")  # Empty string instead of None
                    actions.append(self.ActionModel(**action))
                else:
                    actions.append(action)
            parsed.action = actions
            
            self._log_response(parsed)
            self.n_steps += 1
            
            return parsed
        else:
            raise ValueError('Invalid message content format')

    @time_execution_async("--step")
    async def step(self, step_info: Optional[CustomAgentStepInfo] = None) -> None:
        """Execute one step of the task"""
        logger.info(f"\n📍 Step {self.n_steps}")
        state = None
        model_output = None
        result: list[ActionResult] = []

        try:
            state = await self.browser_context.get_state(use_vision=self.use_vision)
            self.message_manager.add_state_message(state, self._last_actions, self._last_result, step_info)
            input_messages = self.message_manager.get_messages()
            try:
                model_output = await self.get_next_action(input_messages)
                if self.register_new_step_callback:
                    self.register_new_step_callback(state, model_output, self.n_steps)
                self.update_step_info(model_output, step_info)
                logger.info(f"🧠 All Memory: \n{step_info.memory if step_info else ''}")
                self._save_conversation(input_messages, model_output)
                if self.model_name != "deepseek-reasoner":
                    # remove prev message
                    self.message_manager._remove_state_message_by_index(-1)
            except Exception as e:
                # model call failed, remove last state message from history
                self.message_manager._remove_state_message_by_index(-1)
                raise e

            actions = cast(List[ActionModel], model_output.action)
            result = []
            
            # Execute each action and collect results
            for action in actions:
                try:
                    action_dict = action.model_dump(exclude_unset=True)
                    success = await self.execute_action(action_dict, state)
                    if success:
                        # Check if this completes a recorded step
                        is_recorded_step_complete = False
                        if self.recorded_steps and self.n_steps <= len(self.recorded_steps):
                            current_step = self.recorded_steps[self.n_steps - 1]
                            if (
                                (current_step["action"] == "navigate" and "go_to_url" in action_dict) or
                                (current_step["action"] == "input" and "input_text" in action_dict) or
                                (current_step["action"] == "click" and "click_element" in action_dict)
                            ):
                                is_recorded_step_complete = True
                        
                        # Determine if this is the final action that completes the task
                        is_task_complete = False
                        if self.recorded_steps:
                            # For recorded tasks, check if all steps are complete AND we're on the last step
                            is_task_complete = (
                                self.n_steps >= len(self.recorded_steps) and
                                is_recorded_step_complete and
                                self.n_steps == len(self.recorded_steps)  # Ensure we stop exactly at the last step
                            )
                        else:
                            # For non-recorded tasks, check model's assessment
                            is_task_complete = (
                                hasattr(model_output.current_state, 'task_progress') and
                                model_output.current_state.task_progress and
                                any(completion_phrase in str(model_output.current_state.task_progress).lower()
                                    for completion_phrase in ["complete", "finished", "done", "accomplished"])
                            )
                        
                        result.append(ActionResult(
                            extracted_content=f"Successfully executed {list(action_dict.keys())[0]} action",
                            include_in_memory=True,
                            error=None,
                            is_done=bool(is_task_complete)
                        ))
                    else:
                        result.append(ActionResult(
                            extracted_content=None,
                            include_in_memory=True,
                            error=f"Failed to execute {list(action_dict.keys())[0]} action",
                            is_done=False
                        ))
                except Exception as e:
                    result.append(ActionResult(
                        extracted_content=None,
                        include_in_memory=True,
                        error=str(e),
                        is_done=False
                    ))

            if len(actions) == 0:
                # For empty actions, check if task is complete based on model's assessment
                is_task_complete = False
                if self.recorded_steps:
                    is_task_complete = (self.n_steps >= len(self.recorded_steps))
                else:
                    is_task_complete = (
                        hasattr(model_output.current_state, 'task_progress') and
                        model_output.current_state.task_progress and
                        any(completion_phrase in str(model_output.current_state.task_progress).lower()
                            for completion_phrase in ["complete", "finished", "done", "accomplished"])
                    )
                
                result = [ActionResult(
                    is_done=bool(is_task_complete),
                    extracted_content=step_info.memory if step_info else None,
                    include_in_memory=True
                )]
            
            self._last_result = result
            self._last_actions = actions
            
            if len(result) > 0 and result[-1].is_done:
                logger.info(f"📄 Result: {result[-1].extracted_content}")

            self.consecutive_failures = 0

        except Exception as e:
            result = await self._handle_step_error(e)
            self._last_result = result

        finally:
            actions_dump = [a.model_dump(exclude_unset=True) for a in (model_output.action if model_output else [])]
            self.telemetry.capture(
                AgentStepTelemetryEvent(
                    agent_id=self.agent_id,
                    step=self.n_steps,
                    actions=actions_dump,
                    consecutive_failures=self.consecutive_failures,
                    step_error=[r.error for r in result if r.error] if result else ['No result'],
                )
            )
            if not result:
                return

            if state:
                self._make_history_item(model_output, state, result)

    async def analyze_task(self) -> str:
        """Analyze the recorded task steps and generate a semantic understanding"""
        if not self.recorded_steps:
            return f"Task Analysis:\n1. Overall Goal: {self.task}\n\nNo recorded steps available - will attempt task using visual understanding and semantic matching."
            
        # Build a description of what we're trying to accomplish
        task_analysis = []
        task_analysis.append("Task Analysis:")
        task_analysis.append(f"1. Overall Goal: {self.task}")
        task_analysis.append("\n2. Step-by-Step Breakdown:")
        
        # Process each recorded step
        for i, step in enumerate(self.recorded_steps, 1):
            if not isinstance(step, dict):
                continue
                
            action = step.get("action", "")
            if action == "navigate":
                url = step.get("url", "")
                task_analysis.append(f"   {i}. Navigate to {url}")
                task_analysis.append(f"      - This will load a webpage where we expect to find interactive elements")
                
            elif action == "input":
                value = step.get("value", "")
                selector = step.get("selector", "")
                task_analysis.append(f"   {i}. Input text: '{value}'")
                task_analysis.append(f"      - Looking for an input field that accepts text")
                task_analysis.append(f"      - Will try recorded selector '{selector}' first")
                task_analysis.append(f"      - If that fails, will look for input fields visually based on:")
                task_analysis.append(f"        * Placeholder text or labels matching '{value}'")
                task_analysis.append(f"        * Search input fields")
                task_analysis.append(f"        * Visible text input fields")
                
            elif action == "click":
                text = step.get("text", "")
                selector = step.get("selector", "")
                task_analysis.append(f"   {i}. Click element" + (f" with text '{text}'" if text else ""))
                task_analysis.append(f"      - Looking for a clickable element")
                task_analysis.append(f"      - Will try recorded selector '{selector}' first")
                task_analysis.append(f"      - If that fails, will look for clickable elements visually based on:")
                task_analysis.append(f"        * Visible text matching '{text}' if provided")
                task_analysis.append(f"        * Buttons, links, or other clickable elements in the expected area")
        
        task_analysis.append("\n3. Visual Fallback Strategy:")
        task_analysis.append("   - If recorded selectors fail, we'll use visual understanding of the page")
        task_analysis.append("   - This includes analyzing visible text, element positions, and page structure")
        task_analysis.append("   - We'll look for elements that semantically match what we're trying to accomplish")
        
        analysis_text = "\n".join(task_analysis)
        
        # Create a step info object with the task analysis
        step_info = CustomAgentStepInfo(
            task=self.task,
            add_infos=analysis_text,
            step_number=1,
            max_steps=len(self.recorded_steps),
            memory="",
            task_progress="Starting execution of recorded steps",
            future_plans="Will execute each recorded step in sequence, using visual fallbacks if needed"
        )
        
        return analysis_text

    async def run(self, max_steps: int = 100) -> AgentHistoryList:
        """Execute the task with maximum number of steps"""
        try:
            self._log_agent_run()
            
            # First analyze and understand the task
            task_analysis = await self.analyze_task()
            logger.info("\n🔍 Task Analysis:\n" + task_analysis)

            # Execute initial actions if provided
            if self.initial_actions:
                result = await self.controller.multi_act(self.initial_actions, self.browser_context, check_for_new_elements=False)
                self._last_result = result

            step_info = CustomAgentStepInfo(
                task=self.task,
                add_infos=task_analysis,  # Include our task analysis in the step info
                step_number=1,
                max_steps=max_steps,
                memory="",
                task_progress="",
                future_plans=""
            )

            for step in range(max_steps):
                # 1) Check if stop requested
                if self.agent_state and self.agent_state.is_stop_requested():
                    logger.info("🛑 Stop requested by user")
                    self._create_stop_history_item()
                    break

                # 2) Store last valid state before step
                if self.browser_context and self.agent_state:
                    state = await self.browser_context.get_state(use_vision=self.use_vision)
                    self.agent_state.set_last_valid_state(state)

                if self._too_many_failures():
                    break

                # 3) Do the step
                await self.step(step_info)

                if self.history.is_done():
                    if (
                            self.validate_output and step < max_steps - 1
                    ):  # if last step, we dont need to validate
                        if not await self._validate_output():
                            continue

                    logger.info("✅ Task completed successfully")
                    break
            else:
                logger.info("❌ Failed to complete task in maximum steps")

            return self.history

        finally:
            self.telemetry.capture(
                AgentEndTelemetryEvent(
                    agent_id=self.agent_id,
                    success=self.history.is_done(),
                    steps=self.n_steps,
                    max_steps_reached=self.n_steps >= max_steps,
                    errors=self.history.errors(),
                )
            )

            if not self.injected_browser_context:
                await self.browser_context.close()

            if not self.injected_browser and self.browser:
                await self.browser.close()

            if self.generate_gif:
                output_path: str = 'agent_history.gif'
                if isinstance(self.generate_gif, str):
                    output_path = self.generate_gif

                self.create_history_gif(output_path=output_path)

    def _create_stop_history_item(self):
        """Create a history item for when the agent is stopped."""
        try:
            # Attempt to retrieve the last valid state from agent_state
            state = None
            if self.agent_state:
                last_state = self.agent_state.get_last_valid_state()
                if last_state:
                    # Convert to BrowserStateHistory
                    state = BrowserStateHistory(
                        url=getattr(last_state, 'url', ""),
                        title=getattr(last_state, 'title', ""),
                        tabs=getattr(last_state, 'tabs', []),
                        interacted_element=[None],
                        screenshot=getattr(last_state, 'screenshot', None)
                    )
                else:
                    state = self._create_empty_state()
            else:
                state = self._create_empty_state()

            # Create a final item in the agent history indicating done
            stop_history = AgentHistory(
                model_output=None,
                state=state,
                result=[ActionResult(extracted_content=None, error=None, is_done=True)]
            )
            self.history.history.append(stop_history)

        except Exception as e:
            logger.error(f"Error creating stop history item: {e}")
            # Create empty state as fallback
            state = self._create_empty_state()
            stop_history = AgentHistory(
                model_output=None,
                state=state,
                result=[ActionResult(extracted_content=None, error=None, is_done=True)]
            )
            self.history.history.append(stop_history)

    def _convert_to_browser_state_history(self, browser_state):
        return BrowserStateHistory(
            url=getattr(browser_state, 'url', ""),
            title=getattr(browser_state, 'title', ""),
            tabs=getattr(browser_state, 'tabs', []),
            interacted_element=[None],
            screenshot=getattr(browser_state, 'screenshot', None)
        )

    def _create_empty_state(self):
        return BrowserStateHistory(
            url="",
            title="",
            tabs=[],
            interacted_element=[None],
            screenshot=None
        )

    def create_history_gif(self, output_path: str = 'agent_history.gif', duration: int = 3000, show_goals: bool = True, show_task: bool = True, show_logo: bool = False, font_size: int = 40, title_font_size: int = 56, goal_font_size: int = 44, margin: int = 40, line_spacing: float = 1.5) -> None:
        """Create a GIF from the agent's history with overlaid task and goal text."""
        if not self.history.history:
            logger.warning('No history to create GIF from')
            return

        # Check for valid screenshots
        valid_history = [h for h in self.history.history if h.state and h.state.screenshot and isinstance(h.state.screenshot, str)]
        if not valid_history:
            logger.warning('No valid screenshots found in history')
            return

        images = []
        
        # Try to load nicer fonts
        try:
            # Try different font options in order of preference
            font_options = ['Helvetica', 'Arial', 'DejaVuSans', 'Verdana']
            font_loaded = False

            for font_name in font_options:
                try:
                    if platform.system() == 'Windows':
                        # Need to specify the abs font path on Windows
                        font_name = os.path.join(os.getenv('WIN_FONT_DIR', 'C:\\Windows\\Fonts'), font_name + '.ttf')
                    regular_font = ImageFont.truetype(font_name, font_size)
                    title_font = ImageFont.truetype(font_name, title_font_size)
                    goal_font = ImageFont.truetype(font_name, goal_font_size)
                    font_loaded = True
                    break
                except OSError:
                    continue

            if not font_loaded:
                # Fall back to default font if no TrueType fonts are available
                regular_font = ImageFont.load_default()
                title_font = regular_font
                goal_font = regular_font

        except OSError:
            # Fall back to default font
            regular_font = ImageFont.load_default()
            title_font = regular_font
            goal_font = regular_font

        # Load logo if requested
        logo = None
        if show_logo:
            try:
                logo = Image.open('./static/browser-use.png')
                # Resize logo to be small (e.g., 40px height)
                logo_height = 150
                aspect_ratio = logo.width / logo.height
                logo_width = int(logo_height * aspect_ratio)
                logo = logo.resize((logo_width, logo_height), Image.Resampling.LANCZOS)
            except Exception as e:
                logger.warning(f'Could not load logo: {e}')

        # Create task frame if requested
        if show_task and self.task and valid_history:
            first_screenshot_str = valid_history[0].state.screenshot
            if first_screenshot_str:  # Check if screenshot exists and is not None
                first_screenshot = Image.open(io.BytesIO(base64.b64decode(first_screenshot_str)))
                task_frame = self._create_task_frame(
                    self.task,
                    first_screenshot,
                    title_font,
                    regular_font,
                    logo,
                    line_spacing,
                )
                images.append(task_frame)

        # Process each history item
        for i, item in enumerate(valid_history, 1):
            try:
                # Convert base64 screenshot to PIL Image
                screenshot_str = item.state.screenshot
                if not screenshot_str:  # Skip if screenshot is None or empty
                    continue
                    
                img_data = base64.b64decode(screenshot_str)
                image = Image.open(io.BytesIO(img_data))

                if show_goals and item.model_output and hasattr(item.model_output.current_state, 'summary'):
                    # Use summary instead of thought since it's a known attribute
                    image = self._add_overlay_to_image(
                        image=image,
                        step_number=i,
                        goal_text=item.model_output.current_state.summary,
                        regular_font=regular_font,
                        title_font=title_font,
                        margin=margin,
                        logo=logo,
                    )

                images.append(image)
            except Exception as e:
                logger.warning(f'Error processing history item {i}: {e}')
                continue

        if images:
            try:
                # Save the GIF
                images[0].save(
                    output_path,
                    save_all=True,
                    append_images=images[1:],
                    duration=duration,
                    loop=0,
                    optimize=False,
                )
                logger.info(f'Created GIF at {output_path}')
            except Exception as e:
                logger.error(f'Error saving GIF: {e}')
        else:
            logger.warning('No images found in history to create GIF')

    async def execute_action(self, action, state):
        """Execute a browser action"""
        try:
            page = state.current_tab
            
            if "input_text" in action:
                params = action["input_text"]
                text = params.get("text", "")
                selector = params.get("selector", "")
                xpath = params.get("xpath", "")
                
                # Try selector first if provided
                if selector:
                    try:
                        # Wait for element to be visible
                        element = await page.wait_for_selector(selector, timeout=5000)
                        if element:
                            await element.fill(text)
                            return True
                    except Exception as e:
                        logger.warning(f"Failed to use selector {selector}: {e}")
                
                # Try xpath if provided
                if xpath:
                    try:
                        element = await page.wait_for_selector(f"xpath={xpath}", timeout=5000)
                        if element:
                            await element.fill(text)
                            return True
                    except Exception as e:
                        logger.warning(f"Failed to use xpath {xpath}: {e}")
                
                # Try visual search
                browser_state = await page.context.get_state(use_vision=True)
                if browser_state:
                    # Try finding by placeholder/label that might match the text
                    input_selector = None
                    if browser_state.find_input_field():
                        input_selector, _ = browser_state.find_input_field()
                    
                    # If no match, try finding any search/text input
                    if not input_selector and browser_state.find_input_field("search"):
                        input_selector, _ = browser_state.find_input_field("search")
                    
                    if input_selector:
                        try:
                            element = await page.wait_for_selector(input_selector, timeout=5000)
                            if element:
                                await element.fill(text)
                                return True
                        except Exception as e:
                            logger.warning(f"Failed to use visual selector: {e}")
                
                raise ValueError(f"Could not find input element for text: {text}")
                
            elif "click_element" in action:
                params = action["click_element"]
                selector = params.get("selector", "")
                text = params.get("text", "")
                xpath = params.get("xpath", "")
                
                # Try selector first if provided
                if selector:
                    try:
                        element = await page.wait_for_selector(selector, timeout=5000)
                        if element:
                            await element.click()
                            return True
                    except Exception as e:
                        logger.warning(f"Failed to use selector {selector}: {e}")
                
                # Try xpath if provided
                if xpath:
                    try:
                        element = await page.wait_for_selector(f"xpath={xpath}", timeout=5000)
                        if element:
                            await element.click()
                            return True
                    except Exception as e:
                        logger.warning(f"Failed to use xpath {xpath}: {e}")
                
                # Try finding by text if provided
                if text:
                    try:
                        # Try exact text match first
                        element = await page.wait_for_selector(f"text={text}", timeout=5000)
                        if element:
                            await element.click()
                            return True
                            
                        # Try contains text
                        element = await page.wait_for_selector(f"text='{text}'", timeout=5000)
                        if element:
                            await element.click()
                            return True
                    except Exception as e:
                        logger.warning(f"Failed to find element by text {text}: {e}")
                
                # Try visual search
                browser_state = await page.context.get_state(use_vision=True)
                if browser_state:
                    for clickable in browser_state.clickable_elements:
                        if clickable['selectors']:
                            try:
                                element = await page.wait_for_selector(clickable['selectors'][0], timeout=5000)
                                if element:
                                    await element.click()
                                    return True
                            except Exception:
                                continue
                
                raise ValueError(f"Could not find clickable element")
                
            elif "go_to_url" in action:
                url = action["go_to_url"]["url"]
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    return True
                except Exception as e:
                    logger.warning(f"Navigation error: {e}")
                    # Try again with default timeout and load state
                    await page.goto(url)
                    return True
                
            elif "send_keys" in action:
                keys = action["send_keys"]["keys"]
                await page.keyboard.press(keys)
                return True
                
            elif "scroll_down" in action:
                amount = action["scroll_down"]["amount"]
                await page.evaluate(f"window.scrollBy(0, {amount})")
                return True
                
            else:
                raise ValueError(f"Unknown action: {action}")
                
        except Exception as e:
            raise Exception(f"Error executing action {action}: {str(e)}")

    def _make_history_item(self, model_output: Optional[CustomAgentOutput], state: BrowserState, result: list[ActionResult]) -> None:
        """Create a history item from the current state"""
        if model_output is None:
            return
            
        # Convert actions to serializable format
        actions = []
        for action in model_output.action:
            if hasattr(action, 'model_dump'):
                actions.append(action.model_dump(exclude_unset=True))
            else:
                actions.append(dict(action))

        # Create history item
        history_item = AgentHistory(
            model_output=model_output,
            state=self._convert_to_browser_state_history(state),
            result=result
        )
        self.history.history.append(history_item)

    def to_dict(self) -> Dict[str, Any]:
        """Convert agent to JSON-serializable dictionary"""
        return {
            "task": self.task,
            "add_infos": self.add_infos,
            "use_vision": self.use_vision,
            "max_failures": self.max_failures,
            "retry_delay": self.retry_delay,
            "max_input_tokens": self.max_input_tokens,
            "validate_output": self.validate_output,
            "include_attributes": self.include_attributes,
            "max_error_length": self.max_error_length,
            "max_actions_per_step": self.max_actions_per_step,
            "tool_calling_method": self.tool_calling_method
        }