"""otc_explain — answer plain-English capability questions about otc.

A source-checkout twin of otc.py. Uses only keyword routing (no API
keys, no model, no browser) so it works offline for anyone who
couldn't be bothered to read the README:

    uv run otc_explain.py "what does this do?"
    uv run otc_explain.py "how are bot checks solved?" --full
    uv run otc_explain.py --list
    uv run otc_explain.py --keys

The same content is also reachable from the main CLI:

    uv run otc.py --explain "what's new?"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.explain import answer, key_status, list_topics, topic_names


def main() -> int:
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = [a for a in sys.argv[1:]]
    full = "--full" in args
    if "--list" in args:
        print("Capability topics:")
        print(list_topics())
        return 0
    if "--keys" in args:
        print("API keys in .env.local (values masked):")
        print(key_status())
        return 0
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print(__doc__.strip())
        print()
        print("Try: uv run otc_explain.py \"what does this do?\"")
        return 2
    q = " ".join(positional)
    print(f"Q: {q}\n")
    print(answer(q, full=full))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
