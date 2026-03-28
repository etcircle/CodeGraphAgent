"""Context-oriented tool handlers: get_function_context, get_module_overview, explain_path."""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...utils.debug_log import debug_log, error_logger
from .search_handlers import _get_indexed_repo_paths, _detect_language


# ---------------------------------------------------------------------------
# Tool 1: get_function_context
# ---------------------------------------------------------------------------

def get_function_context(db_manager, **args) -> Dict[str, Any]:
    """
    Returns comprehensive context for a function: source code (from filesystem),
    class membership, callers, callees, imports, and sibling methods.
    Source code is ALWAYS read from the filesystem, never from graph properties.
    """
    function_name = args.get("function_name")
    if not function_name:
        return {"error": "function_name is required."}

    file_path = args.get("file_path")
    repo_path = args.get("repo_path")
    include_source = args.get("include_source", True)
    caller_depth = args.get("caller_depth", 1)
    callee_depth = args.get("callee_depth", 1)
    include_sibling_methods = args.get("include_sibling_methods", True)

    # Build query filters
    filters = "fn.name = $name"
    params: Dict[str, Any] = {"name": function_name}

    if file_path:
        resolved_fp = str(Path(file_path).resolve())
        filters += " AND f.path = $file_path"
        params["file_path"] = resolved_fp

    if repo_path:
        resolved_rp = str(Path(repo_path).resolve())
        filters += " AND f.path STARTS WITH $repo_path"
        params["repo_path"] = resolved_rp

    try:
        with db_manager.get_driver().session() as session:
            # Find the function(s)
            query = (
                f"MATCH (f:File)-[:CONTAINS]->(fn:Function) "
                f"WHERE {filters} "
                f"RETURN fn.name AS name, f.path AS path, "
                f"fn.line_number AS line_number, fn.end_line AS end_line, "
                f"fn.args AS args, fn.decorators AS decorators, "
                f"fn.context AS class_name, fn.cyclomatic_complexity AS complexity"
            )
            records = list(session.run(query, **params))

            if not records:
                return {
                    "success": True,
                    "function_name": function_name,
                    "found": False,
                    "message": f"No function named '{function_name}' found.",
                }

            functions = []
            for rec in records:
                fn_data: Dict[str, Any] = {
                    "name": rec["name"],
                    "file": rec["path"],
                    "line_number": rec["line_number"],
                    "end_line": rec["end_line"],
                    "args": rec["args"],
                    "decorators": rec["decorators"],
                    "class_name": rec["class_name"],
                    "complexity": rec["complexity"],
                    "language": _detect_language(rec["path"]),
                }

                # Source code: ALWAYS from filesystem
                if include_source and rec["path"] and rec["line_number"]:
                    source = _read_source_from_file(
                        rec["path"], rec["line_number"], rec["end_line"]
                    )
                    fn_data["source"] = source

                # Callers
                if caller_depth > 0:
                    fn_data["callers"] = _get_call_chain(
                        session, rec["name"], rec["path"],
                        direction="callers", depth=caller_depth
                    )

                # Callees
                if callee_depth > 0:
                    fn_data["callees"] = _get_call_chain(
                        session, rec["name"], rec["path"],
                        direction="callees", depth=callee_depth
                    )

                # Sibling methods (other methods on the same class)
                if include_sibling_methods and rec["class_name"]:
                    sibling_query = (
                        "MATCH (f:File {path: $path})-[:CONTAINS]->(sib:Function) "
                        "WHERE sib.context = $class_name AND sib.name <> $name "
                        "RETURN sib.name AS name, sib.args AS args, "
                        "sib.line_number AS line, sib.decorators AS decorators "
                        "ORDER BY sib.line_number"
                    )
                    siblings = []
                    for sib in session.run(sibling_query,
                                           path=rec["path"],
                                           class_name=rec["class_name"],
                                           name=rec["name"]):
                        siblings.append({
                            "name": sib["name"],
                            "args": sib["args"],
                            "line_number": sib["line"],
                            "decorators": sib["decorators"],
                        })
                    fn_data["sibling_methods"] = siblings

                functions.append(fn_data)

            return {
                "success": True,
                "function_name": function_name,
                "found": True,
                "match_count": len(functions),
                "functions": functions,
            }

    except Exception as e:
        error_logger(f"get_function_context error: {e}")
        return {"error": f"Failed to get function context: {str(e)}"}


def _read_source_from_file(
    file_path: str, start_line: int, end_line: Optional[int]
) -> Optional[str]:
    """Read source code from the filesystem. Never from graph properties."""
    try:
        with open(file_path, "r", errors="replace") as f:
            lines = f.readlines()
        start_idx = max(0, start_line - 1)
        end_idx = end_line if end_line else min(start_idx + 50, len(lines))
        source_lines = lines[start_idx:end_idx]
        return "".join(source_lines)
    except OSError as e:
        error_logger(f"Failed to read source from {file_path}: {e}")
        return None


def _get_call_chain(
    session, fn_name: str, fn_path: str,
    direction: str, depth: int
) -> List[Dict]:
    """Get callers or callees recursively up to depth levels."""
    if direction == "callers":
        query = (
            "MATCH (f:File)-[:CONTAINS]->(caller:Function)-[:CALLS]->(callee:Function) "
            "WHERE callee.name = $name AND f.path + callee.path IS NOT NULL "
            "RETURN caller.name AS name, f.path AS file, caller.line_number AS line"
        )
    else:
        query = (
            "MATCH (f_callee:File)-[:CONTAINS]->(callee:Function), "
            "(f_caller:File)-[:CONTAINS]->(caller:Function)-[:CALLS]->(callee) "
            "WHERE caller.name = $name "
            "RETURN callee.name AS name, f_callee.path AS file, callee.line_number AS line"
        )

    # For depth 1, just do a simple query
    if depth <= 1:
        if direction == "callers":
            q = (
                "MATCH (f:File)-[:CONTAINS]->(caller:Function)-[:CALLS]->(callee:Function {name: $name}) "
                "RETURN caller.name AS name, f.path AS file, caller.line_number AS line"
            )
        else:
            q = (
                "MATCH (f:File)-[:CONTAINS]->(fn:Function {name: $name})-[:CALLS]->(callee:Function), "
                "(f2:File)-[:CONTAINS]->(callee) "
                "RETURN callee.name AS name, f2.path AS file, callee.line_number AS line"
            )
        results = []
        for rec in session.run(q, name=fn_name):
            results.append({
                "name": rec["name"],
                "file": rec["file"],
                "line_number": rec["line"],
            })
        return results

    # For deeper levels, use variable-length path
    if direction == "callers":
        q = (
            f"MATCH path = (caller:Function)-[:CALLS*1..{depth}]->(target:Function {{name: $name}}) "
            f"WITH caller, length(path) AS hops "
            f"MATCH (f:File)-[:CONTAINS]->(caller) "
            f"RETURN DISTINCT caller.name AS name, f.path AS file, "
            f"caller.line_number AS line, hops "
            f"ORDER BY hops"
        )
    else:
        q = (
            f"MATCH path = (source:Function {{name: $name}})-[:CALLS*1..{depth}]->(callee:Function) "
            f"WITH callee, length(path) AS hops "
            f"MATCH (f:File)-[:CONTAINS]->(callee) "
            f"RETURN DISTINCT callee.name AS name, f.path AS file, "
            f"callee.line_number AS line, hops "
            f"ORDER BY hops"
        )
    results = []
    for rec in session.run(q, name=fn_name):
        results.append({
            "name": rec["name"],
            "file": rec["file"],
            "line_number": rec["line"],
            "depth": rec["hops"],
        })
    return results


# ---------------------------------------------------------------------------
# Tool 4: get_module_overview
# ---------------------------------------------------------------------------

def get_module_overview(db_manager, **args) -> Dict[str, Any]:
    """
    Returns a structured summary of a code module: endpoints, service classes,
    models, schemas, and key functions.
    """
    module_path = args.get("module_path")
    if not module_path:
        return {"error": "module_path is required."}

    repo_path = args.get("repo_path")

    # Resolve the module path
    if repo_path and not Path(module_path).is_absolute():
        resolved = str(Path(repo_path).resolve() / module_path)
    else:
        resolved = str(Path(module_path).resolve())

    # Ensure trailing separator for STARTS WITH matching
    if not resolved.endswith("/"):
        resolved += "/"

    try:
        with db_manager.get_driver().session() as session:
            result: Dict[str, Any] = {
                "success": True,
                "module_path": resolved,
                "endpoints": [],
                "services": [],
                "models": [],
                "schemas": [],
                "key_functions": [],
                "summary": {},
            }

            # 1. Endpoints: functions with router/app decorators
            endpoint_query = (
                "MATCH (f:File)-[:CONTAINS]->(fn:Function) "
                "WHERE f.path STARTS WITH $scope "
                "AND any(d IN fn.decorators WHERE "
                "  d CONTAINS 'router.' OR d CONTAINS 'app.' OR "
                "  d CONTAINS '.get' OR d CONTAINS '.post' OR "
                "  d CONTAINS '.put' OR d CONTAINS '.delete' OR "
                "  d CONTAINS '.patch') "
                "RETURN fn.name AS name, fn.decorators AS decorators, "
                "fn.line_number AS line, f.path AS file, fn.args AS args "
                "ORDER BY f.path, fn.line_number"
            )
            for rec in session.run(endpoint_query, scope=resolved):
                http_method, route = _parse_endpoint_decorator(rec["decorators"])
                result["endpoints"].append({
                    "name": rec["name"],
                    "method": http_method,
                    "route": route,
                    "file": rec["file"],
                    "line_number": rec["line"],
                    "args": rec["args"],
                })

            # 2. Services: classes in **/services/** files
            service_query = (
                "MATCH (f:File)-[:CONTAINS]->(c:Class) "
                "WHERE f.path STARTS WITH $scope AND f.path CONTAINS '/services/' "
                "OPTIONAL MATCH (f)-[:CONTAINS]->(m:Function) WHERE m.context = c.name "
                "RETURN c.name AS class_name, f.path AS file, c.line_number AS line, "
                "collect(m.name) AS methods "
                "ORDER BY f.path, c.line_number"
            )
            for rec in session.run(service_query, scope=resolved):
                result["services"].append({
                    "class_name": rec["class_name"],
                    "file": rec["file"],
                    "line_number": rec["line"],
                    "methods": rec["methods"],
                })

            # 3. Models: classes in **/models/** files
            model_query = (
                "MATCH (f:File)-[:CONTAINS]->(c:Class) "
                "WHERE f.path STARTS WITH $scope AND f.path CONTAINS '/models/' "
                "RETURN c.name AS class_name, f.path AS file, c.line_number AS line "
                "ORDER BY f.path, c.line_number"
            )
            for rec in session.run(model_query, scope=resolved):
                result["models"].append({
                    "class_name": rec["class_name"],
                    "file": rec["file"],
                    "line_number": rec["line"],
                })

            # 4. Schemas: classes in **/schemas/** files
            schema_query = (
                "MATCH (f:File)-[:CONTAINS]->(c:Class) "
                "WHERE f.path STARTS WITH $scope AND f.path CONTAINS '/schemas/' "
                "RETURN c.name AS class_name, f.path AS file, c.line_number AS line "
                "ORDER BY f.path, c.line_number"
            )
            for rec in session.run(schema_query, scope=resolved):
                result["schemas"].append({
                    "class_name": rec["class_name"],
                    "file": rec["file"],
                    "line_number": rec["line"],
                })

            # 5. Key functions: module-level functions (no class context) sorted by complexity
            fn_query = (
                "MATCH (f:File)-[:CONTAINS]->(fn:Function) "
                "WHERE f.path STARTS WITH $scope "
                "AND (fn.context IS NULL OR fn.context = '') "
                "RETURN fn.name AS name, f.path AS file, fn.line_number AS line, "
                "fn.args AS args, fn.cyclomatic_complexity AS complexity "
                "ORDER BY fn.cyclomatic_complexity DESC "
                "LIMIT 20"
            )
            for rec in session.run(fn_query, scope=resolved):
                result["key_functions"].append({
                    "name": rec["name"],
                    "file": rec["file"],
                    "line_number": rec["line"],
                    "args": rec["args"],
                    "complexity": rec["complexity"],
                })

            # 6. Summary stats
            stats_query = (
                "MATCH (f:File) WHERE f.path STARTS WITH $scope "
                "OPTIONAL MATCH (f)-[:CONTAINS]->(fn:Function) "
                "OPTIONAL MATCH (f)-[:CONTAINS]->(c:Class) "
                "RETURN count(DISTINCT f) AS files, "
                "count(DISTINCT fn) AS functions, "
                "count(DISTINCT c) AS classes"
            )
            stats = session.run(stats_query, scope=resolved).single()
            if stats:
                result["summary"] = {
                    "total_files": stats["files"],
                    "total_functions": stats["functions"],
                    "total_classes": stats["classes"],
                    "endpoints": len(result["endpoints"]),
                    "services": len(result["services"]),
                    "models": len(result["models"]),
                    "schemas": len(result["schemas"]),
                }

            return result

    except Exception as e:
        error_logger(f"get_module_overview error: {e}")
        return {"error": f"Failed to get module overview: {str(e)}"}


def _parse_endpoint_decorator(decorators: list) -> tuple:
    """Parse HTTP method and route from decorator strings."""
    if not decorators:
        return ("UNKNOWN", "")

    http_methods = {"get", "post", "put", "delete", "patch", "head", "options"}

    for dec in decorators:
        dec_lower = dec.lower()
        for method in http_methods:
            if f".{method}" in dec_lower:
                # Try to extract route from decorator args
                route_match = re.search(r'["\']([^"\']+)["\']', dec)
                route = route_match.group(1) if route_match else ""
                return (method.upper(), route)

    return ("UNKNOWN", "")


# ---------------------------------------------------------------------------
# Tool 7: explain_path
# ---------------------------------------------------------------------------

def explain_path(db_manager, **args) -> Dict[str, Any]:
    """
    Find the shortest call chain between two functions using Neo4j shortestPath.
    """
    from_function = args.get("from_function")
    to_function = args.get("to_function")

    if not from_function:
        return {"error": "from_function is required."}
    if not to_function:
        return {"error": "to_function is required."}

    repo_path = args.get("repo_path")
    max_depth = args.get("max_depth", 6)

    params: Dict[str, Any] = {"from_name": from_function, "to_name": to_function}
    repo_filter = ""
    if repo_path:
        resolved = str(Path(repo_path).resolve())
        repo_filter = " AND start.path STARTS WITH $repo AND end.path STARTS WITH $repo"
        params["repo"] = resolved

    try:
        with db_manager.get_driver().session() as session:
            # Try forward direction
            query = (
                f"MATCH (start:Function {{name: $from_name}}), (end:Function {{name: $to_name}}) "
                f"WHERE true {repo_filter} "
                f"MATCH path = shortestPath((start)-[:CALLS*1..{max_depth}]->(end)) "
                f"WITH path, [n IN nodes(path) | n] AS ns "
                f"UNWIND range(0, size(ns)-1) AS idx "
                f"WITH path, ns[idx] AS n, idx "
                f"MATCH (f:File)-[:CONTAINS]->(n) "
                f"WITH path, collect({{name: n.name, path: f.path, line: n.line_number, idx: idx}}) AS chain "
                f"RETURN chain, length(path) AS hops "
                f"ORDER BY hops "
                f"LIMIT 3"
            )

            results = list(session.run(query, **params))

            if results:
                paths = []
                for rec in results:
                    chain = sorted(rec["chain"], key=lambda x: x["idx"])
                    paths.append({
                        "hops": rec["hops"],
                        "chain": [
                            {
                                "name": node["name"],
                                "file": node["path"],
                                "line_number": node["line"],
                            }
                            for node in chain
                        ],
                    })

                return {
                    "success": True,
                    "from": from_function,
                    "to": to_function,
                    "direction": "forward",
                    "paths_found": len(paths),
                    "paths": paths,
                }

            # Try reverse direction
            rev_params = {"from_name": to_function, "to_name": from_function}
            if repo_path:
                rev_params["repo"] = resolved

            rev_query = (
                f"MATCH (start:Function {{name: $from_name}}), (end:Function {{name: $to_name}}) "
                f"WHERE true {repo_filter} "
                f"MATCH path = shortestPath((start)-[:CALLS*1..{max_depth}]->(end)) "
                f"WITH path, [n IN nodes(path) | n] AS ns "
                f"UNWIND range(0, size(ns)-1) AS idx "
                f"WITH path, ns[idx] AS n, idx "
                f"MATCH (f:File)-[:CONTAINS]->(n) "
                f"WITH path, collect({{name: n.name, path: f.path, line: n.line_number, idx: idx}}) AS chain "
                f"RETURN chain, length(path) AS hops "
                f"ORDER BY hops "
                f"LIMIT 3"
            )

            rev_results = list(session.run(rev_query, **rev_params))

            if rev_results:
                paths = []
                for rec in rev_results:
                    chain = sorted(rec["chain"], key=lambda x: x["idx"])
                    paths.append({
                        "hops": rec["hops"],
                        "chain": [
                            {
                                "name": node["name"],
                                "file": node["path"],
                                "line_number": node["line"],
                            }
                            for node in chain
                        ],
                    })

                return {
                    "success": True,
                    "from": from_function,
                    "to": to_function,
                    "direction": "reverse (B calls A)",
                    "paths_found": len(paths),
                    "paths": paths,
                    "note": f"No path from {from_function} → {to_function}, but found {to_function} → {from_function}.",
                }

            return {
                "success": True,
                "from": from_function,
                "to": to_function,
                "paths_found": 0,
                "message": f"No call path found between '{from_function}' and '{to_function}' within {max_depth} hops.",
            }

    except Exception as e:
        error_logger(f"explain_path error: {e}")
        return {"error": f"Failed to find path: {str(e)}"}
