import json
import re
from pathlib import Path
from typing import Any, TypedDict

import tiktoken

from browser_env import Action, ActionParsingError, Trajectory
from browser_env.env_config import URL_MAPPINGS
from browser_env.utils import StateInfo
from llms import lm_config

APIInput = str | list[Any] | dict[str, Any]


class Instruction(TypedDict):
    """Instruction for constructing prompt"""
    intro: str
    examples: dict[str, list[tuple[str, str]]]
    meta_prompts: dict[str, str]
    template: str
    meta_data: dict[str, Any]


class PromptConstructor(object):
    def __init__(
        self,
        instruction_path: str | Path,
        lm_config: lm_config.LMConfig,
        tokenizer: tiktoken.core.Encoding,
    ):
        self.instrction_path = Path(instruction_path)
        self.obs_modality = "text"
        self.lm_config = lm_config
        instruction = json.load(open(self.instrction_path))
        instruction["examples"] = [tuple(e) for e in instruction["examples"]]
        self.instruction: Instruction = instruction
        self.tokenizer = tokenizer

    def get_lm_api_input(
        self, intro: str, examples: list[tuple[str, str]], current: str
    ) -> APIInput:

        """Return the require format for an API"""
        message: list[dict[str, str]] | str
        if "openai" in self.lm_config.provider:
            if self.lm_config.mode == "chat":
                message = [{"role": "system", "content": intro}]
                for (x, y) in examples:
                    message.append(
                        {
                            "role": "system",
                            "name": "example_user",
                            "content": x,
                        }
                    )
                    message.append(
                        {
                            "role": "system",
                            "name": "example_assistant",
                            "content": y,
                        }
                    )
                message.append({"role": "user", "content": current})
                return message
            elif self.lm_config.mode == "completion":
                message = f"{intro}\n\n"
                message += "Here are a few examples:\n"
                for example in examples:
                    message += f"Observation\n:{example[0]}\n\n"
                    message += f"Action: {example[1]}\n\n"
                message += "Now make prediction given the observation\n\n"
                message += f"Observation\n:{current}\n\n"
                message += "Action:"
                return message
            else:
                raise ValueError(
                    f"OpenAI models do not support mode {self.lm_config.mode}"
                )
        else:
            raise NotImplementedError(
                f"Provider {self.lm_config.provider} not implemented"
            )

    def construct(
        self,
        trajectory: Trajectory,
        intent: str,
        # add a parameter referring type of prompt
        prompt_type: str,
        meta_data: dict[str, Any] = {},
    ) -> APIInput:
        raise NotImplementedError

    def map_url_to_real(self, url: str) -> str:
        """Map the urls to their real world counterparts"""
        for i, j in URL_MAPPINGS.items():
            if i in url:
                url = url.replace(i, j)
        return url

    def map_url_to_local(self, url: str) -> str:
        """Map the urls to their local counterparts"""
        for i, j in URL_MAPPINGS.items():
            if j in url:
                url = url.replace(j, i)
        return url

    def _extract_action(self, response: str) -> str:
        raise NotImplementedError

    def extract_action(self, response: str) -> str:
        response = self._extract_action(response)
        response = self.map_url_to_local(response)
        return response


class LocalPromptConstructor(PromptConstructor):
    def __init__(
        self,
        instruction_path: str | Path,
        lm_config: lm_config.LMConfig,
        tokenizer: tiktoken.core.Encoding,
    ):
        super().__init__(instruction_path, lm_config, tokenizer)
    def construct(
        self,
        trajectory: Trajectory,
        intent: str,
        prompt_type: str,
        meta_data: dict[str, Any] = {},
    ) -> APIInput:
        """Construct prompt given the trajectory"""
        instruction = json.load(open(self.instrction_path))
        instruction["examples"] = [tuple(e) for e in instruction["examples"]]
        intro = instruction["intro"]
        template = instruction["template"]
        keywords = instruction["meta_data"]["keywords"]

        state_info: StateInfo = trajectory[-1]  # type: ignore[assignment]
        obs = state_info["observation"][self.obs_modality]
        max_obs_length = self.lm_config.gen_config["max_obs_length"]
        if max_obs_length:
            obs = self.tokenizer.decode(self.tokenizer.encode(obs)[:max_obs_length])  # type: ignore[arg-type]
        page = state_info["info"]["page"]
        url = page.url
        previous_action_str = meta_data["action_history"][-1]
        # input x
        current = template.format(
            objective=intent,
            url=self.map_url_to_real(url),
            observation=obs,
            previous_action=previous_action_str,
        )
        # make sure all keywords are replaced
        assert all([f"{{k}}" not in current for k in keywords])


        examples = {
            "local_plan": self.instruction["examples"]["local_plan_examples"],
            "false_check": self.instruction["examples"]["false_check_examples"],
            "pass_check": self.instruction["examples"]["pass_check_examples"],
            "revise": self.instruction["examples"]["revise_examples"],
            "request": self.instruction["examples"]["request_examples"]
        }

        if prompt_type in examples:
            meta_prompt = self.instruction["meta_prompts"][prompt_type]
        else:
            raise ActionParsingError(
                f"The type of prompt isn't allowed!"
            )
        intro += meta_prompt
        prompt = self.get_lm_api_input(intro, examples[prompt_type], current)

        return prompt

    def _extract_action(self, response: str) -> str:
        action_splitter = self.instruction["meta_data"]["action_splitter"]
        pattern = rf"{action_splitter}(.*?){action_splitter}"
        match = re.search(pattern, response)
        if match:
            return match.group(1)
        else:
            raise ActionParsingError(
                f"Cannot parse action from response {response}"
            )

    def _extract_action_and_reasons(self, response: str) -> dict:
        action_pattern = r"Action: (.*?)\n"
        reasons_pattern = r"Reasons: (.*)"
        action_match = re.search(action_pattern, response)
        reasons_match = re.search(reasons_pattern, response, re.DOTALL)

        if action_match:
            action = action_match.group(1)
        else:
            action = None

        if reasons_match:
            reasons = reasons_match.group(1).strip()
        else:
            reasons = None

        return {
            "Action": action,
            "Reasons": reasons
        }

    def parse_local_plan(self, text):
        local_plan = []
        action_pattern = r'\*\*Action (\d+):\*\* (.+?)\n'
        matches = re.finditer(action_pattern, text)

        for match in matches:
            action_number = match.group(1)
            action_description = match.group(2).strip()

            action_info = {
                "Action Number": action_number,
                "Action Description": action_description
            }

            local_plan.append(action_info)

        return local_plan
    def parse_check_result(self, response: str) -> dict:
        result_pattern = r"Result: (.*?)\n"
        reasons_pattern = r"Reasons: (.*)"

        result_match = re.search(result_pattern, response)
        reasons_match = re.search(reasons_pattern, response, re.DOTALL)

        if result_match:
            result = result_match.group(1).strip().lower()
        else:
            result = None

        if reasons_match:
            reasons = reasons_match.group(1).strip()
        else:
            reasons = None

        return {
            "Result": result,
            "Reasons": reasons
        }

    def parse_revise_result(self, response: str) -> dict:
        revised_plan_pattern = r"Revised Plan: (.*)"

        revised_plan_match = re.search(revised_plan_pattern, response, re.DOTALL)

        if revised_plan_match:
            revised_plan = revised_plan_match.group(1).strip()
        else:
            revised_plan = None

        return {
            "Revised Plan": revised_plan
        }

    def parse_request_result(self, response: str) -> dict:
        request_type_pattern = r"Request Type: (.*?)\n"
        reasons_pattern = r"Reasons: (.*)"

        request_type_match = re.search(request_type_pattern, response)
        reasons_match = re.search(reasons_pattern, response, re.DOTALL)

        if request_type_match:
            request_type = request_type_match.group(1).strip().lower()
        else:
            request_type = None

        if reasons_match:
            reasons = reasons_match.group(1).strip()
        else:
            reasons = None

        return {
            "Request Type": request_type,
            "Reasons": reasons
        }

class GlobalPromptConstructor(PromptConstructor):
    """The agent will perform step-by-step reasoning before the answer"""

    def __init__(
        self,
        instruction_path: str | Path,
        lm_config: lm_config.LMConfig,
        tokenizer: tiktoken.core.Encoding,
    ):
        super().__init__(instruction_path, lm_config, tokenizer)
        self.answer_phrase = self.instruction["meta_data"]["answer_phrase"]
        self.instruction_path = Path(instruction_path)
        self.obs_modality = "text"
        self.lm_config = lm_config
        instruction = json.load(open(self.instruction_path))
        self.instruction: Instruction = instruction

    def construct(
        self,
        trajectory: Trajectory,
        intent: str,
        prompt_type: str,
        meta_data: dict[str, Any] = {},
    ) -> APIInput:
        """Construct prompt given the trajectory"""
        intro = self.instruction["intro"]
        template = self.instruction["template"]
        keywords = self.instruction["meta_data"]["keywords"]

        state_info: StateInxfo = trajectory[-1]  # type: ignore[assignment]
        obs = state_info["observation"][self.obs_modality]
        max_obs_length = self.lm_config.gen_config["max_obs_length"]
        if max_obs_length:
            obs = self.tokenizer.decode(self.tokenizer.encode(obs)[:max_obs_length])  # type: ignore[arg-type]
        page = state_info["info"]["page"]
        url = page.url
        previous_action_str = meta_data["action_history"][-1]
        # input x
        current = template.format(
            objective=intent,
            url=self.map_url_to_real(url),
            observation=obs,
            previous_action=previous_action_str,
        )
        # make sure all keywords are replaced
        assert all([f"{{k}}" not in current for k in keywords])

        examples = {
            "global_plan": self.instruction["examples"]["global_plan_examples"],
            "decide": self.instruction["examples"]["decide_examples"],
            "revise": self.instruction["examples"]["revise_examples"],
            "collation": self.instruction["examples"]["collation_examples"],
        }

        if prompt_type in examples:
            meta_prompt = self.instruction["meta_prompts"][prompt_type]
        else:
            raise ActionParsingError(
                f"The type of prompt isn't allowed!"
            )
        intro += meta_prompt
        prompt = self.get_lm_api_input(intro, examples[prompt_type], current)

        return prompt

    def _extract_action(self, response: str) -> str:
        action_splitter = self.instruction["meta_data"]["action_splitter"]
        pattern = rf"{action_splitter}(.*?){action_splitter}"
        match = re.search(pattern, response)
        if match:
            return match.group(1)
        else:
            raise ActionParsingError(
                f"Cannot parse action from response {response}"
            )

    def _extract_action_and_reasons(self, response: str) -> dict:
        action_pattern = r"Action: (.*?)\n"
        reasons_pattern = r"Reasons: (.*)"
        action_match = re.search(action_pattern, response)
        reasons_match = re.search(reasons_pattern, response, re.DOTALL)

        if action_match:
            action = action_match.group(1)
        else:
            action = None

        if reasons_match:
            reasons = reasons_match.group(1).strip()
        else:
            reasons = None

        return {
            "Action": action,
            "Reasons": reasons
        }

    def parse_global_plan(self, text):
        global_plan = []
        subtask_pattern = r'\*\*Subtask (\d+): (.+?)\*\*[\s\S]*?-\s+\*\*Subtask\*\*: (.+?)\n- \*\*Expected State\*\*: (.+?)\n\n'
        matches = re.finditer(subtask_pattern, text)

        for match in matches:
            subtask_number = match.group(1)
            subtask_description = match.group(2).strip()
            subtask_action = match.group(3).strip()
            expected_state = match.group(4).strip()

            subtask_info = {
                "Subtask Number": subtask_number,
                "Subtask Description": subtask_description,
                "Subtask Action": subtask_action,
                "Expected State": expected_state
            }

            global_plan.append(subtask_info)

        return global_plan
    def parse_decide_result(self, response: str) -> dict:
        decision_pattern = r"Decision: (.*?)\n"
        reasons_pattern = r"Reasons: (.*)"

        decision_match = re.search(decision_pattern, response)
        reasons_match = re.search(reasons_pattern, response, re.DOTALL)

        if decision_match:
            decision = decision_match.group(1).strip().lower()
        else:
            decision = None

        if reasons_match:
            reasons = reasons_match.group(1).strip()
        else:
            reasons = None

        return {
            "Decision": decision,
            "Reasons": reasons
        }

    def parse_revise_result(self, response: str) -> dict:
        revised_plan_pattern = r"Revised Plan: (.*)"

        revised_plan_match = re.search(revised_plan_pattern, response, re.DOTALL)

        if revised_plan_match:
            revised_plan = revised_plan_match.group(1).strip()
        else:
            revised_plan = None

        return {
            "Revised Plan": revised_plan
        }

    def parse_collation_result(self, response: str) -> dict:
        collation_pattern = r"Collation: (.*)"

        collation_match = re.search(collation_pattern, response, re.DOTALL)

        if collation_match:
            collation = collation_match.group(1).strip()
        else:
            collation = None

        return {
            "Collation": collation
        }