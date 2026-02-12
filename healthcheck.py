
import sys
import vapoursynth as vs
def main():
    core = vs.core
    try:
        if hasattr(core, "knlm") and hasattr(core, "fmtc"):
            sys.exit(0)
        sys.exit(1)
    except:
        sys.exit(1)
if __name__ == "__main__":
    main()
