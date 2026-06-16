"""Entry point — verifies all sources via acquire.steps.VerifySourcesStep."""
import sys

from acquire.steps import main

if __name__ == "__main__":
    sys.exit(main())
