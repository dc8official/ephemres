from __future__ import annotations

import ast
import html
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


class SafeString(str):
    """Marker string subclass indicating content is already HTML-safe."""
    pass


_TOKEN_RE = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.DOTALL)
_BLOCK_RE = re.compile(
    r"\{%\s*block\s+([a-zA-Z_]\w*)\s*%\}(.*?)\{%\s*endblock(?:\s+[a-zA-Z_]\w*)?\s*%\}",
    re.DOTALL,
)
_EXTENDS_RE = re.compile(r'^\s*\{%\s*extends\s+["\']([^"\']+)["\']\s*%\}')

# Matches `<atom> | <filter_name>(<args>)` and `<atom> | <filter_name>`
_FILTER_CALL_RE = re.compile(
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[a-zA-Z_][\w\.]*)\s*\|\s*([a-zA-Z_]\w*)\s*\(([^)]*)\)'
)
_FILTER_SIMPLE_RE = re.compile(
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[a-zA-Z_][\w\.]*)\s*\|\s*([a-zA-Z_]\w*)\b(?!\s*\()'
)


def _rewrite_filters(expr: str) -> str:
    """Rewrites Jinja pipe filter syntax `x | f(a)` and `x | f` into `_apply_filter(x, 'f', ...)`."""
    prev = None
    cur = expr.strip()
    while prev != cur:
        prev = cur
        cur = _FILTER_CALL_RE.sub(
            lambda m: f"_apply_filter({m.group(1)}, '{m.group(2)}', {m.group(3)})",
            cur,
        )
        cur = _FILTER_SIMPLE_RE.sub(
            lambda m: f"_apply_filter({m.group(1)}, '{m.group(2)}')",
            cur,
        )
    return cur


def _apply_filter(val: Any, filter_name: str, *args: Any) -> Any:
    if filter_name == "default":
        default_val = args[0] if args else ""
        boolean_check = bool(args[1]) if len(args) > 1 else False
        if boolean_check:
            return val if val else default_val
        return default_val if (val is None or val == "") else val
    if filter_name == "length" or filter_name == "count":
        try:
            return len(val) if val is not None else 0
        except TypeError:
            return 0
    if filter_name == "format":
        fmt_str = str(val)
        try:
            if len(args) == 1:
                arg0 = args[0] if args[0] is not None else 0
                return fmt_str % arg0
            return fmt_str % args
        except Exception:
            return fmt_str
    if filter_name == "safe":
        return SafeString("" if val is None else str(val))
    if filter_name in ("escape", "e"):
        return SafeString(html.escape("" if val is None else str(val), quote=True))
    if filter_name == "upper":
        return ("" if val is None else str(val)).upper()
    if filter_name == "lower":
        return ("" if val is None else str(val)).lower()
    if filter_name == "title":
        return ("" if val is None else str(val)).title()
    if filter_name == "strip" or filter_name == "trim":
        return ("" if val is None else str(val)).strip()
    if filter_name == "round":
        precision = int(args[0]) if args else 0
        try:
            return round(float(val or 0.0), precision)
        except Exception:
            return val
    return val


def _eval_ast_node(node: ast.AST, context: Dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body, context)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "true" or node.id == "True":
            return True
        if node.id == "false" or node.id == "False":
            return False
        if node.id == "none" or node.id == "None":
            return None
        return context.get(node.id, "")
    if isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            return ""
        base = _eval_ast_node(node.value, context)
        if base is None:
            return ""
        if isinstance(base, dict):
            return base.get(node.attr, "")
        return getattr(base, node.attr, "")
    if isinstance(node, ast.Subscript):
        base = _eval_ast_node(node.value, context)
        idx = _eval_ast_node(node.slice, context)
        try:
            return base[idx]
        except Exception:
            return ""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "_apply_filter":
            eval_args = [_eval_ast_node(a, context) for a in node.args]
            return _apply_filter(*eval_args)
        raise ValueError(f"Unsupported function call in template expression: {ast.dump(node)}")
    if isinstance(node, ast.UnaryOp):
        operand = _eval_ast_node(node.operand, context)
        if isinstance(node.op, ast.Not):
            return not bool(operand)
        if isinstance(node.op, ast.USub):
            return -(operand or 0)
        if isinstance(node.op, ast.UAdd):
            return +(operand or 0)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            res: Any = True
            for val_node in node.values:
                res = _eval_ast_node(val_node, context)
                if not res:
                    return res
            return res
        if isinstance(node.op, ast.Or):
            res = False
            for val_node in node.values:
                res = _eval_ast_node(val_node, context)
                if res:
                    return res
            return res
    if isinstance(node, ast.BinOp):
        left = _eval_ast_node(node.left, context)
        right = _eval_ast_node(node.right, context)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return (left or 0) - (right or 0)
        if isinstance(node.op, ast.Mult):
            if isinstance(left, (str, list, tuple)) and isinstance(right, int):
                right = max(0, min(10000, right))
            elif isinstance(right, (str, list, tuple)) and isinstance(left, int):
                left = max(0, min(10000, left))
            return (left or 0) * (right or 0)
        if isinstance(node.op, ast.Div):
            return (left or 0) / (right or 1)
        if isinstance(node.op, ast.Mod):
            return left % right
    if isinstance(node, ast.Compare):
        left = _eval_ast_node(node.left, context)
        for op, comp_node in zip(node.ops, node.comparators):
            right = _eval_ast_node(comp_node, context)
            try:
                if isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                elif isinstance(op, ast.Gt):
                    ok = (left or 0) > (right or 0)
                elif isinstance(op, ast.GtE):
                    ok = (left or 0) >= (right or 0)
                elif isinstance(op, ast.Lt):
                    ok = (left or 0) < (right or 0)
                elif isinstance(op, ast.LtE):
                    ok = (left or 0) <= (right or 0)
                elif isinstance(op, ast.In):
                    ok = left in (right or ())
                elif isinstance(op, ast.NotIn):
                    ok = left not in (right or ())
                elif isinstance(op, ast.Is):
                    ok = left is right
                elif isinstance(op, ast.IsNot):
                    ok = left is not right
                else:
                    ok = False
            except TypeError:
                ok = False
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_ast_node(e, context) for e in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _eval_ast_node(k, context): _eval_ast_node(v, context)
            for k, v in zip(node.keys, node.values)
            if k is not None
        }
    return ""


def evaluate_expression(expr_str: str, context: Dict[str, Any]) -> Any:
    rewritten = _rewrite_filters(expr_str)
    if not rewritten:
        return ""
    try:
        tree = ast.parse(rewritten, mode="eval")
        return _eval_ast_node(tree, context)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Template AST Nodes
# ---------------------------------------------------------------------------
class _Node:
    def render(self, context: Dict[str, Any], autoescape: bool = True) -> str:
        raise NotImplementedError


class _TextNode(_Node):
    def __init__(self, text: str) -> None:
        self.text = text

    def render(self, context: Dict[str, Any], autoescape: bool = True) -> str:
        return self.text


class _VarNode(_Node):
    def __init__(self, expr: str) -> None:
        self.expr = expr.strip()

    def render(self, context: Dict[str, Any], autoescape: bool = True) -> str:
        val = evaluate_expression(self.expr, context)
        if val is None:
            return ""
        if isinstance(val, SafeString) or not autoescape:
            return str(val)
        return html.escape(str(val), quote=True)


class _SetNode(_Node):
    def __init__(self, var_name: str, expr: str) -> None:
        self.var_name = var_name.strip()
        self.expr = expr.strip()

    def render(self, context: Dict[str, Any], autoescape: bool = True) -> str:
        context[self.var_name] = evaluate_expression(self.expr, context)
        return ""


class _IfNode(_Node):
    def __init__(
        self,
        branches: List[Tuple[Optional[str], List[_Node]]],
    ) -> None:
        self.branches = branches

    def render(self, context: Dict[str, Any], autoescape: bool = True) -> str:
        for cond_expr, body_nodes in self.branches:
            if cond_expr is None or bool(evaluate_expression(cond_expr, context)):
                return "".join(n.render(context, autoescape=autoescape) for n in body_nodes)
        return ""


class _ForNode(_Node):
    def __init__(
        self,
        target_vars: List[str],
        iter_expr: str,
        body_nodes: List[_Node],
        else_nodes: Optional[List[_Node]] = None,
    ) -> None:
        self.target_vars = [v.strip() for v in target_vars]
        self.iter_expr = iter_expr.strip()
        self.body_nodes = body_nodes
        self.else_nodes = else_nodes or []

    def render(self, context: Dict[str, Any], autoescape: bool = True) -> str:
        iterable = evaluate_expression(self.iter_expr, context)
        if not iterable:
            return "".join(n.render(context, autoescape=autoescape) for n in self.else_nodes)

        try:
            items = list(iterable)
        except TypeError:
            return "".join(n.render(context, autoescape=autoescape) for n in self.else_nodes)

        if not items:
            return "".join(n.render(context, autoescape=autoescape) for n in self.else_nodes)

        out: List[str] = []
        total = len(items)
        local_ctx = dict(context)
        for idx, item in enumerate(items):
            local_ctx["loop"] = {
                "index": idx + 1,
                "index0": idx,
                "first": idx == 0,
                "last": idx == total - 1,
                "length": total,
            }
            if len(self.target_vars) == 1:
                local_ctx[self.target_vars[0]] = item
            else:
                try:
                    for var_name, sub_val in zip(self.target_vars, item):
                        local_ctx[var_name] = sub_val
                except TypeError:
                    local_ctx[self.target_vars[0]] = item
            for n in self.body_nodes:
                out.append(n.render(local_ctx, autoescape=autoescape))
        return "".join(out)


def _parse_nodes(tokens: List[str], pos: int = 0, stop_tags: Tuple[str, ...] = ()) -> Tuple[List[_Node], int, str]:
    nodes: List[_Node] = []
    n = len(tokens)

    while pos < n:
        tok = tokens[pos]
        if tok.startswith("{#") and tok.endswith("#}"):
            pos += 1
            continue
        if tok.startswith("{{") and tok.endswith("}}"):
            nodes.append(_VarNode(tok[2:-2]))
            pos += 1
            continue
        if tok.startswith("{%") and tok.endswith("%}"):
            tag_body = tok[2:-2].strip()
            if tag_body.startswith("-"):
                tag_body = tag_body[1:].strip()
            if tag_body.endswith("-"):
                tag_body = tag_body[:-1].strip()

            parts = tag_body.split(None, 1)
            kw = parts[0] if parts else ""
            rest = parts[1] if len(parts) > 1 else ""

            if kw in stop_tags:
                return nodes, pos + 1, tag_body

            if kw == "if":
                branches: List[Tuple[Optional[str], List[_Node]]] = []
                cur_cond: Optional[str] = rest
                while True:
                    branch_nodes, pos, end_tag = _parse_nodes(
                        tokens, pos + 1, stop_tags=("elif", "else", "endif")
                    )
                    branches.append((cur_cond, branch_nodes))
                    end_parts = end_tag.split(None, 1)
                    end_kw = end_parts[0]
                    if end_kw == "elif":
                        cur_cond = end_parts[1] if len(end_parts) > 1 else "False"
                        pos -= 1  # will increment at start of next _parse_nodes call
                    elif end_kw == "else":
                        else_nodes, pos, _ = _parse_nodes(tokens, pos, stop_tags=("endif",))
                        branches.append((None, else_nodes))
                        break
                    else:
                        break
                nodes.append(_IfNode(branches))
                continue

            if kw == "for":
                m_for = re.match(r"^(.+?)\s+in\s+(.+)$", rest, re.DOTALL)
                if m_for:
                    target_str, iter_expr = m_for.group(1), m_for.group(2)
                    target_vars = [v.strip() for v in target_str.split(",") if v.strip()]
                    body_nodes, pos, end_tag = _parse_nodes(
                        tokens, pos + 1, stop_tags=("else", "endfor")
                    )
                    else_nodes: List[_Node] = []
                    if end_tag.split(None, 1)[0] == "else":
                        else_nodes, pos, _ = _parse_nodes(tokens, pos, stop_tags=("endfor",))
                    nodes.append(_ForNode(target_vars, iter_expr, body_nodes, else_nodes))
                else:
                    pos += 1
                continue

            if kw == "set":
                if "=" in rest:
                    var_name, expr = rest.split("=", 1)
                    nodes.append(_SetNode(var_name, expr))
                pos += 1
                continue

            # Ignore standalone block/endblock/extends tags already resolved
            pos += 1
            continue

        if tok:
            nodes.append(_TextNode(tok))
        pos += 1

    return nodes, pos, ""


class CompiledTemplate:
    """Compiled zero-dependency HTML template."""

    def __init__(self, source: str, autoescape: bool = True) -> None:
        self.source = source
        self.autoescape = autoescape
        tokens = _TOKEN_RE.split(source)
        self.nodes, _, _ = _parse_nodes(tokens, 0)

    def render(self, **context: Any) -> str:
        ctx = dict(context)
        return "".join(node.render(ctx, autoescape=self.autoescape) for node in self.nodes)


class TemplateEnvironment:
    """
    Drop-in zero-dependency replacement for jinja2.Environment + FileSystemLoader.
    Resolves {% extends "..." %} and {% block ... %} inheritance hierarchies
    and compiles templates into fast in-memory AST nodes.
    """

    def __init__(self, search_path: Union[str, Path], autoescape: bool = True) -> None:
        self.search_path = Path(search_path).resolve()
        self.autoescape = autoescape
        self._cache: Dict[str, CompiledTemplate] = {}

    def _read_template_file(self, name: str) -> str:
        candidate = (self.search_path / name).resolve()
        if not candidate.is_relative_to(self.search_path.resolve()) or not candidate.is_file():
            raise FileNotFoundError(f"Template '{name}' not found in {self.search_path}")
        return candidate.read_text(encoding="utf-8")

    def _resolve_inheritance(self, source: str) -> str:
        m_ext = _EXTENDS_RE.match(source)
        if not m_ext:
            # Strip any {% block name %}...{% endblock %} wrappers in base template
            return _BLOCK_RE.sub(lambda m: m.group(2), source)

        parent_name = m_ext.group(1)
        parent_source = self._read_template_file(parent_name)

        child_blocks: Dict[str, str] = {}
        for m_blk in _BLOCK_RE.finditer(source):
            child_blocks[m_blk.group(1)] = m_blk.group(2)

        def _replace_block(match: re.Match) -> str:
            blk_name = match.group(1)
            default_content = match.group(2)
            return child_blocks.get(blk_name, default_content)

        merged = _BLOCK_RE.sub(_replace_block, parent_source)
        return self._resolve_inheritance(merged)

    def get_template(self, name: str) -> CompiledTemplate:
        if name not in self._cache:
            raw_source = self._read_template_file(name)
            resolved_source = self._resolve_inheritance(raw_source)
            self._cache[name] = CompiledTemplate(resolved_source, autoescape=self.autoescape)
        return self._cache[name]

    def from_string(self, source: str) -> CompiledTemplate:
        resolved = self._resolve_inheritance(source)
        return CompiledTemplate(resolved, autoescape=self.autoescape)


Template = CompiledTemplate
