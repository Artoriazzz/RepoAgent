from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, get_type_hints


MAX_RESULT_SIZE_CHARS = 50_000


class ToolStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"


class ToolSource(str, Enum):
    BUILTIN = "builtin"
    INTEGRATION = "integration"
    EXTERNAL = "external"


@dataclass
class ToolResponse:
    """所有工具共用的结构化 observation。

    PARTIAL 表示“调用完成但结果不完整或命令未成功”，不是 Python 异常。统一响应让
    Agent 可以把失败当作新证据继续决策，也让 QueryEngine 无需了解每个工具的异常类型。
    """

    status: ToolStatus
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, text: str, data: dict[str, Any] | None = None) -> "ToolResponse":
        return cls(status=ToolStatus.SUCCESS, text=text, data=data or {})

    @classmethod
    def partial(cls, text: str, data: dict[str, Any] | None = None) -> "ToolResponse":
        return cls(status=ToolStatus.PARTIAL, text=text, data=data or {})

    @classmethod
    def failure(cls, code: str, message: str) -> "ToolResponse":
        return cls(status=ToolStatus.ERROR, text=message, error={"code": code, "message": message})

    def to_dict(self) -> dict[str, Any]:
        payload = {"status": self.status.value, "text": self.text, "data": self.data}
        if self.error:
            payload["error"] = self.error
        if self.stats:
            payload["stats"] = self.stats
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass
class ToolParameter:
    name: str
    type: str
    description: str
    required: bool = True


@dataclass
class PermissionResult:
    allowed: bool
    message: str = ""
    parameters: dict[str, Any] | None = None

    @classmethod
    def allow(cls, parameters: dict[str, Any] | None = None) -> "PermissionResult":
        return cls(allowed=True, parameters=parameters)

    @classmethod
    def deny(cls, message: str) -> "PermissionResult":
        return cls(allowed=False, message=message)


ToolPredicate = bool | Callable[[dict[str, Any]], bool]
ToolPermissionCheck = Callable[[dict[str, Any]], PermissionResult]
ToolHandler = Callable[[dict[str, Any]], ToolResponse | str]


class Tool:
    """工具契约基类：schema、权限元数据、校验和执行生命周期。"""

    name: str
    description: str
    aliases: tuple[str, ...] = ()
    max_result_size_chars: int = MAX_RESULT_SIZE_CHARS
    deferred: bool = False
    source: ToolSource = ToolSource.BUILTIN
    sort_key: str = ""

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        raise NotImplementedError

    def parameters(self) -> list[ToolParameter]:
        raise NotImplementedError

    def prompt(self) -> str:
        return ""

    def is_concurrency_safe(self, parameters: dict[str, Any]) -> bool:
        return False

    def is_read_only(self, parameters: dict[str, Any]) -> bool:
        return False

    def is_destructive(self, parameters: dict[str, Any]) -> bool:
        return False

    def check_permissions(self, parameters: dict[str, Any]) -> PermissionResult:
        return PermissionResult.allow(parameters)

    def validate(self, parameters: dict[str, Any]) -> ToolResponse | None:
        # schema 会帮助模型生成参数，但模型输出始终是不可信输入，因此执行前必须再次
        # 做服务端校验。这里是浅层 JSON 类型校验，复杂业务约束应放进 permission/handler。
        if not isinstance(parameters, dict):
            return ToolResponse.failure("BAD_ARGUMENTS", "Tool arguments must be a JSON object.")
        for parameter in self.parameters():
            value = parameters.get(parameter.name)
            if value is None:
                if parameter.required:
                    return ToolResponse.failure("BAD_ARGUMENTS", f"Missing required argument: {parameter.name}")
                continue
            if not _matches_json_type(value, parameter.type):
                return ToolResponse.failure(
                    "BAD_ARGUMENTS",
                    f"Argument {parameter.name} must be {parameter.type}, got {type(value).__name__}",
                )
        return None

    def run_with_timing(self, parameters: dict[str, Any], truncate: bool = True) -> ToolResponse:
        """按 validate -> permission -> handler -> normalize 的固定管道执行工具。"""

        started = time.time()
        try:
            validation_error = self.validate(parameters)
            if validation_error:
                response = validation_error
            else:
                permission = self.check_permissions(parameters)
                if not permission.allowed:
                    response = ToolResponse.failure("PERMISSION_DENIED", permission.message)
                else:
                    response = self.run(permission.parameters or parameters)
        except Exception as exc:
            response = ToolResponse.failure("TOOL_ERROR", str(exc))
        response.stats["time_ms"] = int((time.time() - started) * 1000)
        if truncate:
            _truncate_response(response, self.max_result_size_chars)
        return response

    def to_openai_schema(self) -> dict[str, Any]:
        # schema 是给模型看的能力说明，不是授权凭证。即使模型能生成某个调用，最终仍要
        # 通过 check_permissions 和 handler 内部的路径/命令安全检查。
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in self.parameters():
            properties[parameter.name] = {
                "type": parameter.type,
                "description": parameter.description,
            }
            if parameter.required:
                required.append(parameter.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class FunctionTool(Tool):
    def __init__(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
        *,
        aliases: Iterable[str] = (),
        max_result_size_chars: int = MAX_RESULT_SIZE_CHARS,
        source: ToolSource | str = ToolSource.BUILTIN,
    ):
        self.func = func
        self.name = name or func.__name__
        self.description = description or (inspect.getdoc(func) or f"Run {self.name}").splitlines()[0]
        self.aliases = tuple(aliases)
        self.max_result_size_chars = max_result_size_chars
        self.source = _coerce_source(source)
        self.sort_key = self.name
        self._parameters = self._parse_parameters()

    def _parse_parameters(self) -> list[ToolParameter]:
        signature = inspect.signature(self.func)
        hints = get_type_hints(self.func)
        params: list[ToolParameter] = []
        for name, parameter in signature.parameters.items():
            py_type = hints.get(name, str)
            params.append(
                ToolParameter(
                    name=name,
                    type=_json_type(py_type),
                    description=f"Parameter {name}",
                    required=parameter.default == inspect.Parameter.empty,
                )
            )
        return params

    def parameters(self) -> list[ToolParameter]:
        return self._parameters

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        result = self.func(**parameters)
        if isinstance(result, ToolResponse):
            return result
        return ToolResponse.success(str(result), {"output": result})


class CallbackTool(Tool):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: list[ToolParameter],
        handler: ToolHandler,
        aliases: Iterable[str] = (),
        max_result_size_chars: int = MAX_RESULT_SIZE_CHARS,
        prompt: str | Callable[[], str] = "",
        is_concurrency_safe: ToolPredicate = False,
        is_read_only: ToolPredicate = False,
        is_destructive: ToolPredicate = False,
        check_permissions: ToolPermissionCheck | None = None,
        deferred: bool = False,
        source: ToolSource | str = ToolSource.BUILTIN,
        sort_key: str | None = None,
    ):
        self.name = name
        self.description = description
        self.aliases = tuple(aliases)
        self.max_result_size_chars = max_result_size_chars
        self._parameters = parameters
        self._handler = handler
        self._prompt = prompt
        self._is_concurrency_safe = is_concurrency_safe
        self._is_read_only = is_read_only
        self._is_destructive = is_destructive
        self._check_permissions = check_permissions
        self.deferred = deferred
        self.source = _coerce_source(source)
        self.sort_key = sort_key or name

    def parameters(self) -> list[ToolParameter]:
        return self._parameters

    def prompt(self) -> str:
        if callable(self._prompt):
            return self._prompt()
        return self._prompt

    def is_concurrency_safe(self, parameters: dict[str, Any]) -> bool:
        return _predicate_value(self._is_concurrency_safe, parameters)

    def is_read_only(self, parameters: dict[str, Any]) -> bool:
        return _predicate_value(self._is_read_only, parameters)

    def is_destructive(self, parameters: dict[str, Any]) -> bool:
        return _predicate_value(self._is_destructive, parameters)

    def check_permissions(self, parameters: dict[str, Any]) -> PermissionResult:
        if self._check_permissions is None:
            return PermissionResult.allow(parameters)
        return self._check_permissions(parameters)

    def run(self, parameters: dict[str, Any]) -> ToolResponse:
        result = self._handler(parameters)
        if isinstance(result, ToolResponse):
            return result
        return ToolResponse.success(str(result))


def build_tool(
    *,
    name: str,
    description: str,
    parameters: list[ToolParameter],
    handler: ToolHandler,
    aliases: Iterable[str] = (),
    max_result_size_chars: int = MAX_RESULT_SIZE_CHARS,
    prompt: str | Callable[[], str] = "",
    is_concurrency_safe: ToolPredicate = False,
    is_read_only: ToolPredicate = False,
    is_destructive: ToolPredicate = False,
    check_permissions: ToolPermissionCheck | None = None,
    deferred: bool = False,
    source: ToolSource | str = ToolSource.BUILTIN,
    sort_key: str | None = None,
) -> Tool:
    """创建完整工具契约，默认值按 fail-closed 方向收敛。"""
    return CallbackTool(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        aliases=aliases,
        max_result_size_chars=max_result_size_chars,
        prompt=prompt,
        is_concurrency_safe=is_concurrency_safe,
        is_read_only=is_read_only,
        is_destructive=is_destructive,
        check_permissions=check_permissions,
        deferred=deferred,
        source=source,
        sort_key=sort_key,
    )


class ToolRegistry:
    """工具名称空间与唯一执行入口。

    Worker 通过 subset/excluding 获得最小工具集；这比只在 prompt 中要求“不要调用”
    更可靠，因为被移除的工具不会出现在 schema 中，也无法通过该 Registry 执行。
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}
        self._activated_deferred: set[str] = set()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool
        for alias in tool.aliases:
            self._aliases[alias] = tool.name

    def register_many(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def register_function(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        self.register(FunctionTool(func, name=name, description=description))

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self.active_tools()]

    def active_tools(self) -> list[Tool]:
        return [
            tool
            for tool in self._tools.values()
            if not tool.deferred or tool.name in self._activated_deferred
        ]

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name) or self._tools.get(self._aliases.get(name, ""))

    def names(self) -> list[str]:
        return list(self._tools)

    def subset(self, names: Iterable[str]) -> "ToolRegistry":
        allowed = set(names)
        registry = ToolRegistry()
        registry.register_many(tool for tool in self._tools.values() if tool.name in allowed)
        return registry

    def excluding(self, names: Iterable[str]) -> "ToolRegistry":
        denied = set(names)
        registry = ToolRegistry()
        registry.register_many(tool for tool in self._tools.values() if tool.name not in denied)
        return registry

    def prompt(self) -> str:
        snippets = [tool.prompt().strip() for tool in self.active_tools() if tool.prompt().strip()]
        return "\n".join(snippets)

    def activate_deferred(self, names: Iterable[str]) -> None:
        for name in names:
            if name in self._tools and self._tools[name].deferred:
                self._activated_deferred.add(name)

    def execute(self, name: str, arguments: str | dict[str, Any], truncate: bool = True) -> ToolResponse:
        """解析模型参数并进入工具的统一安全执行管道。"""

        tool = self.get(name)
        if tool is None:
            return ToolResponse.failure("TOOL_NOT_FOUND", f"Unknown tool: {name}")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return ToolResponse.failure("BAD_ARGUMENTS", f"Invalid JSON arguments: {exc}")
        else:
            parsed = arguments
        return tool.run_with_timing(parsed, truncate=truncate)


def _json_type(py_type: Any) -> str:
    origin = getattr(py_type, "__origin__", None)
    if origin is list:
        return "array"
    if origin is dict:
        return "object"
    return {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }.get(py_type, "string")


def _matches_json_type(value: Any, json_type: str) -> bool:
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    return True


def _truncate_response(response: ToolResponse, max_chars: int) -> None:
    if max_chars <= 0 or len(response.text) <= max_chars:
        return
    original_len = len(response.text)
    response.text = _truncate_text(response.text, max_chars)
    response.data["truncated"] = True
    response.stats["truncated_chars"] = original_len - len(response.text)
    if response.status == ToolStatus.SUCCESS:
        response.status = ToolStatus.PARTIAL


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep_each = max(1, (max_chars - 60) // 2)
    omitted = len(text) - keep_each * 2
    return f"{text[:keep_each]}\n\n[... truncated {omitted} chars ...]\n\n{text[-keep_each:]}"


def _predicate_value(predicate: ToolPredicate, parameters: dict[str, Any]) -> bool:
    if callable(predicate):
        return bool(predicate(parameters))
    return bool(predicate)


def _coerce_source(source: ToolSource | str) -> ToolSource:
    if isinstance(source, ToolSource):
        return source
    return ToolSource(source)
