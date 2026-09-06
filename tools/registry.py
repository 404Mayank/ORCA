"""Tool registry: name -> callable + Pydantic input/output schemas.

Two jobs, and the second one is the reason this file matters.

**Job one:** be the single place that knows what tools exist and what they
accept. ``orchestrator/validate_plan.py`` asks this module whether a planned
call is well-formed, before anything executes.

**Job two:** generate the tool list injected into the planner prompt. From
CLAUDE.md: *never hand-maintain a tool list inside a prompt file; it will drift
within a week.* :func:`planner_tool_block` derives that text from the
registered Pydantic models, so the prompt cannot disagree with the code. If a
tool gains an argument, the prompt gains it in the same commit.

**Unimplemented tools are not advertised.** A spec may be registered before its
function exists -- that is how the schema and the plan validator get built
first -- but :func:`planner_tool_block` only emits implemented tools by
default. The planner therefore cannot produce a plan we are unable to run,
which is a much better failure mode than a valid plan that dies in the
executor mid-demo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from pydantic import BaseModel, TypeAdapter, ValidationError

from core.schemas.plan import extract_references

__all__ = [
    "AgentGroup",
    "ToolSpec",
    "ArgError",
    "register",
    "get",
    "has",
    "all_specs",
    "implemented_specs",
    "planner_tool_block",
    "compact_tool_block",
    "validate_args",
]


class AgentGroup:
    """Which agent group owns a tool. Plain strings, matched to CLAUDE.md.

    Used to fan execution out across the four parallel groups. Not an enum,
    because it is a routing hint rather than a contract, and an over-typed
    routing hint is friction with no payoff.
    """

    OCEAN = "ocean"
    WEATHER = "weather"
    GEOSPATIAL = "geospatial"
    RISK = "risk"
    CATALOGUE = "catalogue"


@dataclass(frozen=True)
class ToolSpec:
    """One registered tool."""

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    agent: str
    fn: Callable[..., Any] | None = None
    safety_critical: bool = False
    """If True, a failure must degrade the verdict rather than be skipped.

    ``PlanStep.optional`` may never be True for a safety-critical tool. The
    alert check is the motivating case: not knowing whether a cyclone is
    active is not the same as there being no cyclone.
    """

    notes: str = ""

    @property
    def implemented(self) -> bool:
        return self.fn is not None


_REGISTRY: dict[str, ToolSpec] = {}


class ArgError(ValueError):
    """A planned call's arguments do not match the tool's input schema."""


def register(
    name: str,
    *,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    agent: str,
    fn: Callable[..., Any] | None = None,
    safety_critical: bool = False,
    notes: str = "",
) -> ToolSpec:
    """Register a tool. Re-registering the same name is an error, not an update.

    Silent overwrite would let two modules define the same tool and leave which
    one wins dependent on import order -- a bug that only shows up under a
    different entry point, which on a hackathon means during the demo.
    """
    if name in _REGISTRY:
        raise ValueError(f"Tool {name!r} is already registered.")
    spec = ToolSpec(
        name=name,
        description=description.strip(),
        input_model=input_model,
        output_model=output_model,
        agent=agent,
        fn=fn,
        safety_critical=safety_critical,
        notes=notes.strip(),
    )
    _REGISTRY[name] = spec
    return spec


def implement(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach an implementation to an already-registered spec.

    Decorator form, so a tool module reads::

        @implement("wave_forecast")
        def wave_forecast(args: WaveForecastIn) -> WaveForecastOut: ...

    Kept separate from :func:`register` so that specs can be declared and
    validated in Phase 0 while the functions are written in Phase 1.
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name not in _REGISTRY:
            raise KeyError(f"Cannot implement unregistered tool {name!r}.")
        spec = _REGISTRY[name]
        if spec.fn is not None:
            raise ValueError(f"Tool {name!r} already has an implementation.")
        _REGISTRY[name] = ToolSpec(**{**spec.__dict__, "fn": fn})
        return fn

    return deco


def get(name: str) -> ToolSpec:
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown tool {name!r}. Registered: {sorted(_REGISTRY)}. "
            "A planner that invents a tool name is caught here, before execution."
        )
    return _REGISTRY[name]


def has(name: str) -> bool:
    return name in _REGISTRY


def all_specs() -> list[ToolSpec]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def implemented_specs() -> list[ToolSpec]:
    return [s for s in all_specs() if s.implemented]


def _clear_for_tests() -> None:
    """Reset the registry. Tests only."""
    _REGISTRY.clear()


# --------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------


def validate_args(name: str, args: dict[str, Any]) -> list[str]:
    """Check a planned call's arguments against the tool's input schema.

    Returns a list of human-readable problems; empty means valid.

    The wrinkle this function exists to handle: a planned argument may be a
    ``$sN`` reference whose value does not exist yet. We cannot type-check a
    value we do not have, but we can check everything around it -- that the
    field exists on the model, and that every *concrete* argument is the right
    type. So references are checked for presence and skipped for type, and each
    concrete argument is validated individually against its own field
    annotation rather than by constructing the whole model, which would fail on
    the missing referenced fields.

    Errors are returned rather than raised because ``validate_plan()``
    collects every problem in one pass and feeds them all back to the planner
    for its single replan attempt. Raising on the first would waste that
    attempt fixing one of five errors.
    """
    spec = get(name)
    model = spec.input_model
    fields = model.model_fields
    problems: list[str] = []

    unknown = set(args) - set(fields)
    if unknown:
        problems.append(
            f"{name}: unexpected argument(s) {sorted(unknown)}; "
            f"accepts {sorted(fields)}"
        )

    for field_name, value in args.items():
        if field_name not in fields:
            continue
        if extract_references(value):
            # Holds a $sN reference somewhere -- possibly nested inside a list
            # or dict. Its type is checked when it resolves, in the executor.
            continue
        try:
            TypeAdapter(fields[field_name].annotation).validate_python(value)
        except ValidationError as exc:
            first = exc.errors()[0]
            problems.append(
                f"{name}.{field_name}: {first['msg']} (got {value!r})"
            )

    for field_name, info in fields.items():
        if info.is_required() and field_name not in args:
            problems.append(f"{name}: missing required argument {field_name!r}")

    return problems


# --------------------------------------------------------------------------
# Planner prompt generation
# --------------------------------------------------------------------------


def _type_name(ann: Any) -> str:
    """Render a type annotation the way a planner LLM can act on.

    ``__name__`` alone renders ``str | None`` as "Union" and ``list[str]`` as
    "list", which tells the planner nothing about what to put in the argument.
    Since this text goes straight into the prompt, an unhelpful rendering here
    is a planning error later.
    """
    import typing

    if ann is None or ann is type(None):
        return "null"
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)
    if origin is None:
        return getattr(ann, "__name__", str(ann).replace("typing.", ""))
    inner = [_type_name(a) for a in args]
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", ())):
        non_null = [i for i in inner if i != "null"]
        rendered = " | ".join(non_null)
        return rendered if len(non_null) == len(inner) else f"{rendered} or null"
    base = getattr(origin, "__name__", str(origin))
    return f"{base}[{', '.join(inner)}]" if inner else base


def _describe_field(name: str, info: Any) -> str:
    type_name = _type_name(info.annotation)
    required = "required" if info.is_required() else "optional"
    desc = (info.description or "").strip()
    tail = f" -- {desc}" if desc else ""
    return f"    - {name} ({type_name}, {required}){tail}"


def planner_tool_block(include_unimplemented: bool = False) -> str:
    """Render the tool catalogue for injection into the planner prompt.

    Generated from the registered Pydantic models, never hand-written. This is
    the mechanism that stops the prompt and the code drifting apart.

    By default only implemented tools are listed, so the planner cannot emit a
    plan we cannot execute.
    """
    specs = all_specs() if include_unimplemented else implemented_specs()
    if not specs:
        return (
            "(No tools are implemented yet. The planner must return a "
            "clarification or a refusal.)"
        )

    lines: list[str] = []
    for spec in specs:
        flag = " [SAFETY-CRITICAL]" if spec.safety_critical else ""
        lines.append(f"- **{spec.name}** ({spec.agent}){flag}")
        lines.append(f"  {spec.description}")
        if spec.input_model.model_fields:
            lines.append("  arguments:")
            for fname, finfo in spec.input_model.model_fields.items():
                lines.append(_describe_field(fname, finfo))
        else:
            lines.append("  arguments: none")
        lines.append(f"  returns: {spec.output_model.__name__}")
        if spec.notes:
            lines.append(f"  note: {spec.notes}")
        lines.append("")
    return "\n".join(lines).rstrip()


def compact_tool_block(exclude_agent: str | None = None) -> str:
    """A short tool catalogue, for a deliberating domain agent.

    :func:`planner_tool_block` renders every argument with its description,
    because the planner has to produce a complete, valid call from nothing.
    A domain agent is doing something narrower -- deciding whether to *ask*
    another agent for something -- and does not need the full contract to
    decide that.

    The size difference is the point. The full block is around 5 kB, and four
    agents deliberating per turn sent 20 kB of identical catalogue on every
    question, which exhausted the free-tier token budget within a handful of
    turns. This renders roughly a fifth of that.

    ``exclude_agent`` drops the caller's own tools. An agent has already run
    what it owns; listing those invites it to ask for a tool it just called.
    """
    lines: list[str] = []
    for spec in implemented_specs():
        if exclude_agent is not None and spec.agent == exclude_agent:
            continue
        required = [
            name for name, info in spec.input_model.model_fields.items() if info.is_required()
        ]
        optional = [
            name for name, info in spec.input_model.model_fields.items() if not info.is_required()
        ]
        args = ", ".join(required)
        if optional:
            args += f" [, {', '.join(optional[:4])}]" if args else f"[{', '.join(optional[:4])}]"
        flag = " [SAFETY-CRITICAL]" if spec.safety_critical else ""
        summary = spec.description.split(".")[0]
        lines.append(f"- {spec.name}({args}) -> {spec.agent}{flag}: {summary}")
    return "\n".join(lines) or "(no tools available)"
