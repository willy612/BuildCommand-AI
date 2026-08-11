import json
from pathlib import Path
from brain.auto_dependencies import generate_dependencies, apply_generated_dependencies

BASE=Path(__file__).resolve().parent
state_path=BASE/"knowledge"/"master"/"project_state_template.json"
tasks_path=BASE/"knowledge"/"master"/"master_tasks.json"

state=json.loads(state_path.read_text(encoding="utf-8"))
tasks=json.loads(tasks_path.read_text(encoding="utf-8"))
generated=generate_dependencies(state,tasks)
apply_generated_dependencies(state,generated)
state_path.write_text(json.dumps(state,indent=2),encoding="utf-8")
print("Automatic dependency templates applied to project-state template.")
