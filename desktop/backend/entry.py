"""Entry point of the frozen desk backend that the desktop app starts."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()     # AI Scan workers are spawned processes; they re-enter here
    from desk.app import main

    main(["--desktop", *sys.argv[1:]])
