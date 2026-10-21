"""The skills/docs-to-CLI contract (issue #4).

Skills are injected into an agent with full trust, so every
`health-insights <subcommand>` they name and every `--flag` they mention must
exist in the shipped CLI, and every `options: {key: ...}` example they show
must be a key the matching module actually reads.

The commands a skill mentions are gathered from its inline code spans; the
docs (MODULES.md) are checked the same way through their fenced code blocks.
"""

from __future__ import annotations

import argparse
import pathlib
import re

import health_insights.cli as cli
import health_insights.modules as modules
from health_insights.concern_rules import glp1_rules  # noqa: F401  (registers the glp1 module)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS_DIR = (
    ROOT / "public" / "skills" if (ROOT / "public" / "skills").is_dir() else ROOT / "skills"
)
DOCS = ROOT / "docs" / "MODULES.md"

# Backtick spans that are real command mentions. Skill *names*
# (`health-insights-setup`) and prose paths (`~/.config/health-insights/...`)
# do not start with "health-insights " and never match.
CLI_SPAN = re.compile(r"`([^`]*health-insights\s[^`]*)`")
OPTIONS_BLOCK = re.compile(r"options:\s*\{([^}]*)\}")


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    options: set[str] = set()
    for action in parser._actions:
        options.update(action.option_strings)
    return options


def _resolve(parser: argparse.ArgumentParser, tokens: list[str]) -> list[argparse.ArgumentParser]:
    """Walk the subcommand chain; return every parser visited (root last to leaf)."""
    visited = [parser]
    current = parser
    index = 0
    while index < len(tokens):
        sub = _subparsers(current)
        if sub is None or tokens[index] not in sub.choices:
            break
        current = sub.choices[tokens[index]]
        visited.append(current)
        index += 1
    return visited


def _command_tokens(span: str) -> list[str] | None:
    """Tokens of one command span, or None when the span is not a command mention."""
    text = span.strip()
    if not (text == "health-insights" or text.startswith("health-insights ")):
        return None
    tokens = text.split()
    # Drop the leading program name, bracket markers and <placeholder> args.
    tokens = [t for t in tokens[1:] if t not in ("[", "]") and not (t.startswith("<") and t.endswith(">"))]
    return tokens


def _commands_in_skill(path: pathlib.Path) -> list[str]:
    return [m for m in CLI_SPAN.findall(path.read_text()) if _command_tokens(m) is not None]


def _commands_in_docs(path: pathlib.Path) -> list[str]:
    """Every fenced-code-block line that runs health-insights, comments stripped."""
    commands: list[str] = []
    in_fence = False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        stripped = line.split("#", 1)[0].strip()
        if stripped == "health-insights" or stripped.startswith("health-insights "):
            commands.append(stripped)
    return commands


def _assert_command_resolves(command: str) -> None:
    tokens = _command_tokens(command)
    assert tokens is not None, command
    visited = _resolve(cli.build_parser(), tokens)
    options = set().union(*(_option_strings(p) for p in visited))
    for token in tokens:
        assert not token.startswith("--") or token in options, (
            f"{command}: --flag {token} does not exist on the visited CLI parsers"
        )
    # Every non-flag token left after the subcommand chain is a positional value
    # (module id, file name, LOINC code); nothing more to assert.


def test_skills_dir_discovered():
    assert SKILLS_DIR.is_dir() and any(SKILLS_DIR.iterdir())


def test_every_skill_command_resolves_in_the_cli():
    total = 0
    for skill_path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        commands = _commands_in_skill(skill_path)
        total += len(commands)
        for command in commands:
            _assert_command_resolves(command)
    assert total, "no skill mentions a health-insights command at all"


def test_docs_module_command_examples_resolve_in_the_cli():
    commands = _commands_in_docs(DOCS)
    assert commands, f"{DOCS}: no health-insights command found"
    for command in commands:
        _assert_command_resolves(command)


def test_every_skill_options_example_is_a_key_the_module_reads():
    checked = 0
    for skill_path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        matches = OPTIONS_BLOCK.findall(skill_path.read_text())
        if not matches:
            continue  # core skills describe no module options
        owning = [m for m in modules.REGISTRY.values() if skill_path.parent.name in m.skills]
        assert owning, f"{skill_path.name}: no module lists this skill"
        # The options the skill shows must be keys of the owning module's DEFAULTS.
        for match in matches:
            for key_value in match.split(","):
                key = key_value.split(":", 1)[0].strip()
                if not key:
                    continue
                checked += 1
                assert any(key in _module_defaults(m) for m in owning), (
                    f"{skill_path.name}: options key {key!r} is not read by any module "
                    f"that lists this skill"
                )
    assert checked, "no skill ships an options example to check"


def _module_defaults(module) -> dict:
    """DEFAULTS of a module's rules module (registered via import side effects)."""
    import importlib

    name = module.id
    try:
        rules = importlib.import_module(f"health_insights.concern_rules.{name}_rules")
    except ModuleNotFoundError:
        return {}
    return dict(getattr(rules, "DEFAULTS", {}))


def test_glp1_skill_options_example_is_complete_and_read():
    """The one options example shipped today, pinned explicitly."""
    skill = (SKILLS_DIR / "health-insights-glp1-monitoring" / "SKILL.md").read_text()
    matches = OPTIONS_BLOCK.findall(skill)
    assert matches, "the glp1 monitoring skill lost its options example"
    keys = {kv.split(":", 1)[0].strip() for kv in matches[0].split(",")}
    assert keys == {"weight_pct_per_week", "ckd"}
    assert keys <= set(glp1_rules.DEFAULTS), (
        f"skill option keys {sorted(keys)} are not all read by glp1_rules.DEFAULTS"
    )
