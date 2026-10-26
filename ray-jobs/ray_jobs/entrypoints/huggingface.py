"""Job type: a Hugging Face dataset, any task. Needs no upload; fetches data files at the commit it resolves.

    python -m ray_jobs.entrypoints.huggingface '<params json>'
"""
import sys

from ray_jobs.runner import main
from ray_jobs.sources.huggingface import extract

if __name__ == "__main__":
    sys.exit(main(sys.argv, extract))
