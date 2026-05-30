---
name: python-scripting-automation
description: Write and automate production-grade Python scripts with structured logging, uv dependency declarations, and standard formatting. Use when generating Python tools or automation scripts.
---

# Python Scripting & Automation

## Instructions
1. **Logging**: Always use Python's built-in `logging` module. Configure standard formats with timestamps, level name, and logger name.
2. **Metadata**: Declare dependencies using PEP 723 inline script metadata:
   ```python
   # /// script
   # dependencies = [
   #   "requests",
   #   "pandas",
   # ]
   # ///
   ```
3. **Execution**: Use `uv run` to execute scripts to handle dependencies dynamically and cleanly.
