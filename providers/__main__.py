"""
Provider self-check: ``python -m providers``

Loads every provider configured in obsidian.yaml and reports whether it could
be imported and constructed. Useful when writing your own provider. It does
not make any paid API calls (credentials are only checked when a provider is
actually used).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from anywhere: make the project root importable
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    from providers.registry import _BASE_CLASSES, get_provider, get_provider_name, is_builtin

    failures = 0
    print(f"{'type':<8} {'provider':<34} {'class':<44} status")
    print("-" * 100)
    for ptype in _BASE_CLASSES:
        try:
            name = get_provider_name(ptype)
        except Exception as e:  # pragma: no cover - config errors
            name = "?"
            print(f"{ptype:<8} {name:<34} {'':<44} ERROR: {e}")
            failures += 1
            continue
        kind = "built-in" if is_builtin(ptype) else "custom"
        try:
            inst = get_provider(ptype, fresh=True)
            cls = f"{type(inst).__module__}.{type(inst).__name__}"
            label = getattr(inst, "name", "")
            print(f"{ptype:<8} {name:<34} {cls:<44} ok ({kind}: {label})")
        except Exception as e:
            print(f"{ptype:<8} {name:<34} {'':<44} ERROR ({kind}): {e}")
            failures += 1

    print()
    if failures:
        print(f"{failures} provider(s) failed to load. See docs/PROVIDERS.md.")
        return 1
    print("All configured providers loaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
