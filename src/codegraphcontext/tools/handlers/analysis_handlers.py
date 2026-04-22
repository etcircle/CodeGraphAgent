from typing import Any, Dict
from ..code_finder import CodeFinder
from ...utils.debug_log import debug_log

def find_dead_code(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to find potentially dead code across the entire project."""
    exclude_decorated_with = args.get("exclude_decorated_with", [])
    repo_path = args.get("repo_path")
    try:
        debug_log(f"Finding dead code. repo_path={repo_path}")
        results = code_finder.find_dead_code(exclude_decorated_with=exclude_decorated_with, repo_path=repo_path)
        
        return {
            "success": True,
            "query_type": "dead_code",
            "results": results
        }
    except Exception as e:
        debug_log(f"Error finding dead code: {str(e)}")
        return {"error": f"Failed to find dead code: {str(e)}"}

def calculate_cyclomatic_complexity(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to calculate cyclomatic complexity for a given function."""
    function_name = args.get("function_name")
    path = args.get("path")
    repo_path = args.get("repo_path")

    try:
        debug_log(f"Calculating cyclomatic complexity for function: {function_name}, repo_path={repo_path}")
        results = code_finder.get_cyclomatic_complexity(function_name, path, repo_path=repo_path)
        
        response = {
            "success": True,
            "function_name": function_name,
            "results": results
        }
        if path:
            response["path"] = path
        
        return response
    except Exception as e:
        debug_log(f"Error calculating cyclomatic complexity: {str(e)}")
        return {"error": f"Failed to calculate cyclomatic complexity: {str(e)}"}

def find_most_complex_functions(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to find the most complex functions."""
    limit = args.get("limit", 10)
    repo_path = args.get("repo_path")
    try:
        debug_log(f"Finding the top {limit} most complex functions. repo_path={repo_path}")
        results = code_finder.find_most_complex_functions(limit, repo_path=repo_path)
        return {
            "success": True,
            "limit": limit,
            "results": results
        }
    except Exception as e:
        debug_log(f"Error finding most complex functions: {str(e)}")
        return {"error": f"Failed to find most complex functions: {str(e)}"}

def analyze_code_relationships(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to analyze code relationships"""
    query_type = args.get("query_type")
    target = args.get("target")
    context = args.get("context")
    repo_path = args.get("repo_path")

    if not query_type or not target:
        return {
            "error": "Both 'query_type' and 'target' are required",
            "supported_query_types": [
                "find_callers", "find_callees", "find_all_callers", "find_all_callees", "find_importers", "who_modifies",
                "class_hierarchy", "overrides", "dead_code", "call_chain",
                "module_deps", "variable_scope", "find_complexity", "find_functions_by_argument", "find_functions_by_decorator"
            ]
        }
    
    try:
        debug_log(f"Analyzing relationships: {query_type} for {target}, repo_path={repo_path}")
        results = code_finder.analyze_code_relationships(query_type, target, context, repo_path=repo_path)
        
        return {
            "success": True, "query_type": query_type, "target": target,
            "context": context, "results": results
        }
    
    except Exception as e:
        debug_log(f"Error analyzing relationships: {str(e)}")
        return {"error": f"Failed to analyze relationships: {str(e)}"}

def find_name_substring(code_finder: CodeFinder, **args) -> Dict[str, Any]:
    """Tool to find symbols whose names contain the given substring."""
    query = args.get("query")
    repo_path = args.get("repo_path")
    case_sensitive = args.get("case_sensitive", False)

    if not query:
        return {"error": "query is required"}

    try:
        debug_log(
            f"Finding symbols by name substring for query: {query} with case_sensitive={case_sensitive}, repo_path={repo_path}"
        )
        results = code_finder.find_name_substring(query, repo_path=repo_path, case_sensitive=case_sensitive)

        return {"success": True, "query": query, "total_matches": len(results), "results": results}
    
    except Exception as e:
        debug_log(f"Error finding symbols by name substring: {str(e)}")
        return {"error": f"Failed to find symbols by name substring: {str(e)}"}
