import argparse
import json
from typing import Any

import tiktoken
from beartype import beartype
import logging

from agent.prompts import *
from browser_env import Trajectory
from browser_env.actions import (
    Action,
    ActionParsingError,
    create_id_based_action,
    create_none_action,
    create_playwright_action,
)
from browser_env.utils import Observation, StateInfo
from llms import lm_config
from llms.providers.openai_utils import (
    generate_from_openai_chat_completion,
    generate_from_openai_completion,
)


class Agent:
    """Base class for the agent"""

    def __init__(self, *args: Any) -> None:
        pass

    def next_action(
        self, trajectory: Trajectory, intent: str, meta_data: Any
    ) -> Action:
        """Predict the next action given the observation"""
        raise NotImplementedError

    def reset(
        self,
        test_config_file: str,
    ) -> None:
        raise NotImplementedError

class GlobalPlanner(Agent):
    @beartype
    def __init__(
        self,
        action_set_tag: str,
        lm_config: lm_config.LMConfig,
        prompt_constructor: GlobalPromptConstructor,
    ) -> None:
        super().__init__()
        self.lm_config = lm_config
        self.prompt_constructor = prompt_constructor
        self.action_set_tag = action_set_tag

    def set_action_set_tag(self, tag: str) -> None:
        self.action_set_tag = tag

    def reset(self, test_config_file: str) -> None:
        pass

    def global_plan(self, logger: Any, trajectory: Trajectory, intent: str, prompt_type: str, meta_data: dict[str, Any]
    ) -> list:
        prompt = self.prompt_constructor.construct(
            trajectory, intent, prompt_type, meta_data
        )
        # logger.info(f"[Global plan Prompt]: {prompt}")

        lm_config = self.lm_config
        if lm_config.provider == "openai":
            if lm_config.mode == "chat":
                response = generate_from_openai_chat_completion(
                    messages=prompt,
                    model=lm_config.model,
                    temperature=lm_config.gen_config["temperature"],
                    top_p=lm_config.gen_config["top_p"],
                    context_length=lm_config.gen_config["context_length"],
                    max_tokens=lm_config.gen_config["max_tokens"],
                    stop_token=None,
                )
            elif lm_config.mode == "completion":
                response = generate_from_openai_completion(
                    prompt=prompt,
                    engine=lm_config.model,
                    temperature=lm_config.gen_config["temperature"],
                    max_tokens=lm_config.gen_config["max_tokens"],
                    top_p=lm_config.gen_config["top_p"],
                    stop_token=lm_config.gen_config["stop_token"],
                )
            else:
                raise ValueError(
                    f"OpenAI models do not support mode {lm_config.mode}"
                )
        else:
            raise NotImplementedError(
                f"Provider {lm_config.provider} not implemented"
            )

        # try:
        parsed_global_plan = self.prompt_constructor.parse_global_plan(response)
        return parsed_global_plan

class LocalAgent(Agent):
    def __init__(
        self,
        action_set_tag: str,
        lm_config: lm_config.LMConfig,
        prompt_constructor: LocalPromptConstructor,
    ) -> None:
        super().__init__()
        self.lm_config = lm_config
        self.prompt_constructor = prompt_constructor
        self.action_set_tag = action_set_tag

    @beartype
    def set_action_set_tag(self, tag: str) -> None:
        self.action_set_tag = tag

    @beartype
    def next_action(
        self, trajectory: Trajectory, action_description: str, meta_data: dict[str, Any]
    ) -> Action:
        try:
            parsed_action_description = self.prompt_constructor.extract_action(action_description)
            if self.action_set_tag == "id_accessibility_tree":
                action = create_id_based_action(parsed_action_description)
            elif self.action_set_tag == "playwright":
                action = create_playwright_action(parsed_action_description)
            else:
                raise ValueError(f"Unknown action type {self.action_set_tag}")

            action["raw_prediction"] = action_description

        except ActionParsingError as e:
            action = create_none_action()
            action["raw_prediction"] = action_description

        return action


    def local_plan(self, logger: Any, trajectory: Trajectory, intent: str, prompt_type: str, meta_data: dict[str, Any]
    ) -> list:
        prompt = self.prompt_constructor.construct(
            trajectory, intent, prompt_type, meta_data
        )

        lm_config = self.lm_config
        if lm_config.provider == "openai":
            if lm_config.mode == "chat":
                response = generate_from_openai_chat_completion(
                    messages=prompt,
                    model=lm_config.model,
                    temperature=lm_config.gen_config["temperature"],
                    top_p=lm_config.gen_config["top_p"],
                    context_length=lm_config.gen_config["context_length"],
                    max_tokens=lm_config.gen_config["max_tokens"],
                    stop_token=None,
                )
            elif lm_config.mode == "completion":
                response = generate_from_openai_completion(
                    prompt=prompt,
                    engine=lm_config.model,
                    temperature=lm_config.gen_config["temperature"],
                    max_tokens=lm_config.gen_config["max_tokens"],
                    top_p=lm_config.gen_config["top_p"],
                    stop_token=lm_config.gen_config["stop_token"],
                )
            else:
                raise ValueError(
                    f"OpenAI models do not support mode {lm_config.mode}"
                )
        else:
            raise NotImplementedError(
                f"Provider {lm_config.provider} not implemented"
            )

        # try:
        parsed_local_plan = self.prompt_constructor.parse_local_plan(response)
        return parsed_local_plan


    def reset(self, test_config_file: str) -> None:
        pass


def construct_llm_config(args: argparse.Namespace) -> lm_config.LMConfig:
    llm_config = lm_config.LMConfig(
        provider=args.provider, model=args.model, mode=args.mode
    )
    if args.provider == "openai":
        llm_config.gen_config["temperature"] = args.temperature
        llm_config.gen_config["top_p"] = args.top_p
        llm_config.gen_config["context_length"] = args.context_length
        llm_config.gen_config["max_tokens"] = args.max_tokens
        llm_config.gen_config["stop_token"] = args.stop_token
        llm_config.gen_config["max_obs_length"] = args.max_obs_length
    else:
        raise NotImplementedError(f"provider {args.provider} not implemented")
    return llm_config


def construct_agent(args: argparse.Namespace, agent_type) -> Any:
    llm_config = construct_llm_config(args)

    tokenizer = tiktoken.encoding_for_model(llm_config.model)

    if agent_type == "local_agent":
        prompt_constructor = LocalPromptConstructor(
            args.local_instruction_path, lm_config=llm_config, tokenizer=tokenizer
        )
        agent = LocalAgent(
            action_set_tag=args.action_set_tag,
            lm_config=llm_config,
            prompt_constructor=prompt_constructor,
        )
        return agent
    elif agent_type == "global_planner":
        prompt_constructor = GlobalPromptConstructor(
            args.global_instruction_path, lm_config=llm_config, tokenizer=tokenizer
        )
        agent = GlobalPlanner(
            action_set_tag=args.action_set_tag,
            lm_config=llm_config,
            prompt_constructor=prompt_constructor,
        )
    else:
        raise NotImplementedError(
            f"agent type {args.agent_type} not implemented"
        )
    return agent
