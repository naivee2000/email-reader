#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
cd "$project_dir"
exec .venv/bin/python -m email_reader --demo

