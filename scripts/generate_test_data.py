"""Replace the website placeholders with website domains from env_config
Generate the test data"""
import json
import os
import sys
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
file_path_raw = os.path.join(parent_dir, "config_files/test.raw.json")
file_path_test = os.path.join(parent_dir, "config_files/test.json")
files_dir = os.path.join(parent_dir, "config_files/")
os.chdir(parent_dir)
sys.path.append(parent_dir)

from browser_env.env_config import *

def main() -> None:
    with open(file_path_raw, "r") as f:
        raw = f.read()
    raw = raw.replace("__GITLAB__", GITLAB)
    raw = raw.replace("__REDDIT__", REDDIT)
    raw = raw.replace("__SHOPPING__", SHOPPING)
    raw = raw.replace("__SHOPPING_ADMIN__", SHOPPING_ADMIN)
    raw = raw.replace("__WIKIPEDIA__", WIKIPEDIA)
    raw = raw.replace("__MAP__", MAP)
    with open(file_path_test, "w") as f:
        f.write(raw)
    # split to multiple files
    data = json.loads(raw)

    # Filter data based on "sites" containing "shopping_admin"
    filted_data = data
    # shopping_admin_data = [item for item in data if "shopping" in item.get("sites", [])]

    for idx, item in enumerate(filted_data):
        with open(f"config_files/{idx}.json", "w") as f:
            json.dump(item, f, indent=2)

    # for idx, item in enumerate(data):
    #     with open(f"config_files/{idx}.json", "w") as f:
    #         json.dump(item, f, indent=2)


if __name__ == "__main__":
    main()
