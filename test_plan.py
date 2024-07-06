import argparse
import glob
import json
import logging
import os
import random
import time
from pathlib import Path

import openai

from agent import (
    Agent,
    GlobalPlanner,
    LocalAgent,
    construct_agent,
)
from agent.prompts import *
from browser_env import (
    Action,
    ActionTypes,
    ScriptBrowserEnv,
    StateInfo,
    Trajectory,
    create_stop_action,
)
from browser_env.actions import is_equivalent
from browser_env.helper_functions import (
    RenderHelper,
    get_action_description,
)
from evaluation_harness import evaluator_router
os.environ['OPENAI_API_KEY'] = ""
LOG_FOLDER = "log_files"
Path(LOG_FOLDER).mkdir(parents=True, exist_ok=True)
LOG_FILE_NAME = f"{LOG_FOLDER}/log_{time.strftime('%Y%m%d%H%M%S', time.localtime())}_{random.randint(0, 10000)}.log"

logger = logging.getLogger("logger")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
logger.addHandler(console_handler)

file_handler = logging.FileHandler(LOG_FILE_NAME)
file_handler.setLevel(logging.DEBUG)
logger.addHandler(file_handler)

# Set the log format
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)


def config() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run end-to-end evaluation on the benchmark"
    )
    parser.add_argument(
        "--render", action="store_true", help="Render the browser"
    )
    parser.add_argument(
        "--slow_mo",
        type=int,
        default=0,
        help="Slow down the browser by the specified amount",
    )
    parser.add_argument(
        "--action_set_tag", default="id_accessibility_tree", help="Action type"
    )
    parser.add_argument(
        "--observation_type",
        choices=["accessibility_tree", "html", "image"],
        default="accessibility_tree",
        help="Observation type",
    )
    parser.add_argument(
        "--current_viewport_only",
        action="store_true",
        help="Only use the current viewport for the observation",
    )
    parser.add_argument("--viewport_width", type=int, default=1280)
    parser.add_argument("--viewport_height", type=int, default=720)
    parser.add_argument("--save_trace_enabled", action="store_true")
    parser.add_argument("--sleep_after_execution", type=float, default=0.0)

    parser.add_argument("--max_steps", type=int, default=30)

    # agent config
    parser.add_argument("--agent_type", type=str, default="prompt")
    parser.add_argument(
        "--local_instruction_path",
        type=str,
        default="agent/prompts/jsons/p_local_agent.json",
    )
    parser.add_argument(
        "--global_instruction_path",
        type=str,
        default="agent/prompts/jsons/p_global_planner.json",
    )
    parser.add_argument(
        "--parsing_failure_th",
        help="When concesecutive parsing failure exceeds this threshold, the agent will stop",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--repeating_action_failure_th",
        help="When concesecutive repeating action exceeds this threshold, the agent will stop",
        type=int,
        default=3,
    )

    # lm config
    parser.add_argument("--provider", type=str, default="openai")
    parser.add_argument("--model", type=str, default="gpt-3.5-turbo-0613")
    parser.add_argument("--mode", type=str, default="chat")
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top_p", type=float, default=0.9)
    parser.add_argument("--context_length", type=int, default=0)
    parser.add_argument("--max_tokens", type=int, default=384)
    parser.add_argument("--stop_token", type=str, default=None)
    parser.add_argument(
        "--max_obs_length",
        type=int,
        help="when not zero, will truncate the observation to this length before feeding to the model",
        default=1920,
    )

    # example config
    parser.add_argument("--test_start_idx", type=int, default=0)
    parser.add_argument("--test_end_idx", type=int, default=1000)

    # logging related
    parser.add_argument("--result_dir", type=str, default="")
    args = parser.parse_args()

    # check the whether the action space is compatible with the observation space
    if (
        args.action_set_tag == "id_accessibility_tree"
        and args.observation_type != "accessibility_tree"
    ):
        raise ValueError(
            f"Action type {args.action_set_tag} is incompatible with the observation type {args.observation_type}"
        )

    return args


def early_stop(
    trajectory: Trajectory, max_steps: int, thresholds: dict[str, int]
) -> tuple[bool, str]:
    """Check whether need to early stop"""

    # reach the max step
    num_steps = (len(trajectory) - 1) / 2
    if num_steps >= max_steps:
        return True, f"Reach max steps {max_steps}"

    last_k_actions: list[Action]
    action_seq: list[Action]

    # Case: parsing failure for k times
    k = thresholds["parsing_failure"]
    last_k_actions = trajectory[1::2][-k:]  # type: ignore[assignment]
    if len(last_k_actions) >= k:
        if all(
            [
                action["action_type"] == ActionTypes.NONE
                for action in last_k_actions
            ]
        ):
            return True, f"Failed to parse actions for {k} times"

    # Case: same action for k times
    k = thresholds["repeating_action"]
    last_k_actions = trajectory[1::2][-k:]  # type: ignore[assignment]
    action_seq = trajectory[1::2]  # type: ignore[assignment]

    if len(action_seq) == 0:
        return False, ""

    last_action: Action = action_seq[-1]

    if last_action["action_type"] != ActionTypes.TYPE:
        if len(last_k_actions) >= k:
            if all(
                [
                    is_equivalent(action, last_action)
                    for action in last_k_actions
                ]
            ):
                return True, f"Same action for {k} times"

    else:
        # check the action sequence
        if (
            sum([is_equivalent(action, last_action) for action in action_seq])
            >= k
        ):
            return True, f"Same typing action for {k} times"

    return False, ""


def test_plan(
    args: argparse.Namespace,
    local_agent: LocalAgent,
    global_planner: GlobalPlanner,
    config_file_list: list[str],
) -> None:
    scores = []
    max_steps = args.max_steps
    early_stop_thresholds = {
        "parsing_failure": args.parsing_failure_th,
        "repeating_action": args.repeating_action_failure_th,
    }

    env = ScriptBrowserEnv(
        headless=not args.render,
        slow_mo=args.slow_mo,
        observation_type=args.observation_type,
        current_viewport_only=args.current_viewport_only,
        viewport_size={
            "width": args.viewport_width,
            "height": args.viewport_height,
        },
        save_trace_enabled=args.save_trace_enabled,
        sleep_after_execution=args.sleep_after_execution,
    )

    def process_action(action, state_info, meta_data):
        trajectory.append(action)
        action_str = get_action_description(
            action,
            state_info["info"]["observation_metadata"],
            action_set_tag=args.action_set_tag,
            prompt_constructor=Agent.prompt_constructor
            if isinstance(Agent, PromptAgent)
            else None,
        )
        render_helper.render(action, state_info, meta_data, args.render_screenshot)
        meta_data["action_history"].append(action_str)
        return action_str

    def execute_local_plan(local_plan, cur_phase_task):
        action_num = 0
        while 1:
            if early_stop_flag:
                action = create_stop_action(f"Early stop: {stop_info}")
                decision = local_agent.check_action(action, trajectory, cur_phase_task)
            else:
                try:
                    action = local_plan[action_num]
                except IndexError as e:
                    action = create_stop_action(f"ERROR: {str(e)}")
                    decision = local_agent.check_action(action, trajectory, cur_phase_task)
                action_num += 1

            action_str = process_action(action, state_info, meta_data)
            is_aligned = local_agent.check_alignment(action, cur_phase_task)

            if not is_aligned:
                revised_local_plan = local_agent.revise_local_plan(local_plan, cur_phase_task)
                if revised_local_plan:
                    local_plan = revised_local_plan
                else:
                    replan_request = local_agent.request_replan(trajectory, cur_phase_task)
                    replan_decision = global_planner.decide_replan(replan_request, global_plan)
                    if replan_decision == "replan":
                        global_plan = global_planner.revise_global_plan(global_plan, replan_request)
                        logger.info(f"[Global Plan #{global_plan_num + 1}]: revised global plan")
                        global_plan_num += 1
                        render_helper.global_planner_render(global_plan, str(global_plan_num))
                        phase_num = 0
                        break
                    elif replan_decision == "overrule":
                        overrule_info = global_planner.overrule_local_plan(replan_request)
                        logger.info(f"[Global Planner]: Overrule local plan, reason: {overrule_info}")
                        local_plan = local_agent.revise_local_plan(local_plan, overrule_info)
                    obs, reward, done, info = env.step(action)
                    state_info = {"observation": obs, "info": info}
                    trajectory.append(state_info)

                    if done:
                        completion_report = local_agent.task_completion_report(trajectory, global_plan)
                        logger.info(f"[Local Agent]: Task Completion Report: {completion_report}")
                        final_result = global_planner.collate_result(completion_report, global_plan)
                        logger.info(f"[Global Planner]: Final Result: {final_result}")
                        break

                    if action["action_type"] == ActionTypes.STOP:
                        break

                return done

            for config_file in config_file_list:
                try:
                    render_helper = RenderHelper(config_file, args.result_dir, args.action_set_tag)
                    with open(config_file) as f:
                        _c = json.load(f)
                    intent = _c["intent"]
                    task_id = _c["task_id"]
                    logger.info(f"[Config file]: {config_file}")
                    logger.info(f"[Intent]: {intent}")

                    trajectory: Trajectory = []
                    obs, info = env.reset(options={"config_file": config_file})
                    state_info: StateInfo = {"observation": obs, "info": info}
                    trajectory.append(state_info)
                    meta_data = {"action_history": ["None"]}

                    initial_global_plan = global_planner.global_plan(logger, trajectory, intent, "global_plan",meta_data)
                    global_plan_len = len(initial_global_plan)
                    global_plan_num = 1
                    logger.info(f"[Global Plan #1]: created {global_plan_len} phases subtasks")
                    render_helper.global_planner_render(initial_global_plan, str(global_plan_num))
                    global_plan = initial_global_plan
                    phase_num = 0

                    while 1:
                        cur_phase_task = initial_global_plan[phase_num]
                        local_plan = local_agent.local_plan(logger, trajectory, intent, "global_plan", meta_data)
                        early_stop_flag, stop_info = early_stop(trajectory, max_steps, early_stop_thresholds)
                        done = execute_local_plan(local_plan, cur_phase_task)
                        if done:
                            break
                        phase_num += 1
                        if phase_num >= global_plan_len:
                            break

                    evaluator = evaluator_router(config_file)
                    score = evaluator(
                        trajectory=trajectory,
                        config_file=config_file,
                        page=env.page,
                        client=env.get_page_client(env.page),
                    )
                    scores.append(score)
                    if score == 1:
                        logger.info(f"[Result] (PASS) {config_file}")
                    else:
                        logger.info(f"[Result] (FAIL) {config_file}")

                    if args.save_trace_enabled:
                        env.save_trace(Path(args.result_dir) / "traces" / f"{task_id}.zip")

                except openai.error.OpenAIError as e:
                    logger.info(f"[OpenAI Error] {repr(e)}")
                except Exception as e:
                    logger.info(f"[Unhandled Error] {repr(e)}]")
                    import traceback
                    with open(Path(args.result_dir) / "error.txt", "a") as f:
                        f.write(f"[Config file]: {config_file}\n")
                        f.write(f"[Unhandled Error] {repr(e)}\n")
                        f.write(traceback.format_exc())

            render_helper.close()
            env.close()
            logger.info(f"Average score: {sum(scores) / len(scores)}")
def prepare(args: argparse.Namespace) -> None:
    # convert prompt python files to json
    from agent.prompts import to_json

    to_json.run()

    # prepare result dir
    result_dir = args.result_dir
    if not result_dir:
        result_dir = (
            f"cache/results_{time.strftime('%Y%m%d%H%M%S', time.localtime())}"
        )
    if not Path(result_dir).exists():
        Path(result_dir).mkdir(parents=True, exist_ok=True)
        args.result_dir = result_dir
        logger.info(f"Create result dir: {result_dir}")

    if not (Path(result_dir) / "traces").exists():
        (Path(result_dir) / "traces").mkdir(parents=True)

    # log the log file
    with open(os.path.join(result_dir, "log_files.txt"), "a+") as f:
        f.write(f"{LOG_FILE_NAME}\n")


def get_unfinished(config_files: list[str], result_dir: str) -> list[str]:
    result_files = glob.glob(f"{result_dir}/*.html")
    task_ids = [
        os.path.basename(f).split(".")[0].split("_")[1] for f in result_files
    ]
    unfinished_configs = []
    for config_file in config_files:
        task_id = os.path.basename(config_file).split(".")[0]
        if task_id not in task_ids:
            unfinished_configs.append(config_file)
    return unfinished_configs


def dump_config(args: argparse.Namespace) -> None:
    config_file = Path(args.result_dir) / "config.json"
    if not config_file.exists():
        with open(config_file, "w") as f:
            json.dump(vars(args), f, indent=4)
            logger.info(f"Dump config to {config_file}")


if __name__ == "__main__":
    args = config()
    args.test_start_idx = 0
    args.test_end_idx = 30
    args.model = "gpt-3.5-turbo"
    args.result_dir = "______Results_______"
    args.sleep_after_execution = 2.5
    prepare(args)

    test_file_list = []
    st_idx = args.test_start_idx
    ed_idx = args.test_end_idx
    for i in range(st_idx, ed_idx):
        test_file_list.append(f"config_files/{i}.json")
    test_file_list = get_unfinished(test_file_list, args.result_dir)
    print(f"Total {len(test_file_list)} tasks left")
    args.render = True
    args.render_screenshot = True
    args.save_trace_enabled = True

    args.current_viewport_only = True
    dump_config(args)

    local_agent = construct_agent(args, "local_agent")
    global_planner = construct_agent(args, "global_planner")
    test_plan(args, local_agent, global_planner, test_file_list)