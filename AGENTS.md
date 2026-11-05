1. When making changes to our task-template, run checks/static.py and rubric.py to ensure our task quality is not damaged.
2. When running a harbor job, do not set a name for the job, let it be the default date formatted name.
3. Export benchmark tasks under `datasets/nim/` only. Keep cached artifacts and logs under `.cache/`, never in custom dataset directories.
