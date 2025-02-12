from __future__ import annotations

import logging
from typing import List, Optional, Type, Dict, Any, Sequence

from browser_use.agent.message_manager.service import MessageManager
from browser_use.agent.message_manager.views import MessageHistory
from browser_use.agent.prompts import SystemPrompt, AgentMessagePrompt
from browser_use.agent.views import ActionResult, AgentStepInfo, ActionModel
from browser_use.browser.views import BrowserState
from langchain_core.language_models import BaseChatModel
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
	AIMessage,
	BaseMessage,
	HumanMessage,
    ToolMessage,
    SystemMessage
)
from langchain_openai import ChatOpenAI
from ..utils.llm import DeepSeekR1ChatOpenAI
from .custom_prompts import CustomAgentMessagePrompt
from .custom_views import CustomAgentStepInfo

logger = logging.getLogger(__name__)


class CustomMassageManager(MessageManager):
    def __init__(
            self,
            llm: BaseChatModel,
            task: str,
            action_descriptions: str,
            system_prompt_class: Type[SystemPrompt] = SystemPrompt,
            agent_prompt_class: Type[AgentMessagePrompt] = AgentMessagePrompt,
            max_input_tokens: int = 128000,
            estimated_characters_per_token: int = 3,
            image_tokens: int = 800,
            include_attributes: Optional[list[str]] = None,
            max_error_length: int = 400,
            max_actions_per_step: int = 10,
            message_context: Optional[str] = None
    ):
        self.llm = llm
        self.task = task
        self.action_descriptions = action_descriptions
        self.system_prompt_class = system_prompt_class
        self.agent_prompt_class = agent_prompt_class
        self.max_input_tokens = max_input_tokens
        self.estimated_characters_per_token = estimated_characters_per_token
        self.image_tokens = image_tokens
        self.include_attributes = include_attributes or []
        self.max_error_length = max_error_length
        self.max_actions_per_step = max_actions_per_step
        self.message_context = message_context
        
        # Initialize messages list
        self.messages: list[BaseMessage] = []
        
        # Add system prompt
        system_content = f"""Task: {self.task}
Action Descriptions: {self.action_descriptions}
Max Actions Per Step: {self.max_actions_per_step}"""
        self.messages.append(SystemMessage(content=system_content))
        
        # Add context message if provided
        if self.message_context:
            self.messages.append(HumanMessage(content=self.message_context))

    def cut_messages(self):
        """Get current message list, potentially trimmed to max tokens"""
        diff = self.history.total_tokens - self.max_input_tokens
        min_message_len = 2 if self.message_context is not None else 1
        
        while diff > 0 and len(self.history.messages) > min_message_len:
            self.history.remove_message(min_message_len) # alway remove the oldest message
            diff = self.history.total_tokens - self.max_input_tokens
        
    def add_state_message(
            self,
            state: BrowserState,
            actions: Optional[Sequence[ActionModel]] = None,
            result: Optional[Sequence[ActionResult]] = None,
            step_info: Optional[CustomAgentStepInfo] = None,
    ) -> None:
        """Add browser state as human message"""
        state_message = self.agent_prompt_class(
            state,
            actions,
            result,
            include_attributes=self.include_attributes,
            max_error_length=self.max_error_length,
            step_info=step_info,
        ).get_user_message()
        self._add_message_with_tokens(state_message)
    
    def _count_text_tokens(self, text: str) -> int:
        if isinstance(self.llm, (ChatOpenAI, ChatAnthropic, DeepSeekR1ChatOpenAI)):
            try:
                tokens = self.llm.get_num_tokens(text)
            except Exception:
                tokens = (
					len(text) // self.estimated_characters_per_token
				)  # Rough estimate if no tokenizer available
        else:
            tokens = (
				len(text) // self.estimated_characters_per_token
			)  # Rough estimate if no tokenizer available
        return tokens

    def _remove_state_message_by_index(self, remove_ind: int = -1) -> None:
        """Remove state message from history by index"""
        i = len(self.messages) - 1
        remove_cnt = 0
        while i >= 0:
            if isinstance(self.messages[i], HumanMessage): 
                remove_cnt += 1
            if remove_cnt == abs(remove_ind):
                self.messages.pop(i)
                break
            i -= 1

    def _setup_messages(self):
        """Setup initial messages"""
        self.messages = []
        system_prompt = self.system_prompt_class(
            task=self.task,
            action_descriptions=self.action_descriptions,
            max_actions_per_step=self.max_actions_per_step,
        )
        self.messages.append(system_prompt.to_message())

    def get_messages(self) -> list[BaseMessage]:
        """Get all messages"""
        return self.messages

    def merge_successive_human_messages(
        self, messages: list[BaseMessage]
    ) -> list[BaseMessage]:
        """Merge successive human messages into a single message"""
        if not messages:
            return messages

        merged_messages = []
        current_content = []
        current_type = None

        for message in messages:
            if current_type == type(message):
                current_content.append(str(message.content))
            else:
                if current_content:
                    merged_content = "\n".join(current_content)
                    if current_type == HumanMessage:
                        merged_messages.append(HumanMessage(content=merged_content))
                    else:
                        merged_messages.append(AIMessage(content=merged_content))
                current_content = [str(message.content)]
                current_type = type(message)

        if current_content:
            merged_content = "\n".join(current_content)
            if current_type == HumanMessage:
                merged_messages.append(HumanMessage(content=merged_content))
            else:
                merged_messages.append(AIMessage(content=merged_content))

        return merged_messages

    def _add_message_with_tokens(self, message: BaseMessage) -> None:
        """Add a message and track token count"""
        self.messages.append(message)