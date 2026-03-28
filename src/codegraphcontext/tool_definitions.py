
TOOLS = {
    "add_code_to_graph": {
        "name": "add_code_to_graph",
        "description": "Performs a one-time scan of a local folder to add its code to the graph. Ideal for indexing libraries, dependencies, or projects not being actively modified. Returns a job ID for background processing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the directory or file to add."},
                "is_dependency": {"type": "boolean", "description": "Whether this code is a dependency.", "default": False}
            },
            "required": ["path"]
        }
    },
    "check_job_status": {
        "name": "check_job_status",
        "description": "Check the status and progress of a background job.",
        "inputSchema": {
            "type": "object",
            "properties": { "job_id": {"type": "string", "description": "Job ID from a previous tool call"} },
            "required": ["job_id"]
        }
    },
    "list_jobs": {
        "name": "list_jobs",
        "description": "List all background jobs and their current status.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "find_code": {
        "name": "find_code",
        "description": "Find relevant code snippets related to a keyword (e.g., function name, class name, or content).",
        "inputSchema": {
            "type": "object",
            "properties": { "query": {"type": "string", "description": "Keyword or phrase to search for"}, "fuzzy_search": {"type": "boolean", "description": "Whether to use fuzzy search", "default": False}, "edit_distance": {"type": "number", "description": "Edit distance for fuzzy search (between 0-2)", "default": 2}, "repo_path": {"type": "string", "description": "Optional: Path to the repository to restrict the search to."}}, 
            "required": ["query"]
        }
    },
    "analyze_code_relationships": {
        "name": "analyze_code_relationships",
        "description": "Analyze code relationships like 'who calls this function' or 'class hierarchy'. Supported query types include: find_callers, find_callees, find_all_callers, find_all_callees, find_importers, who_modifies, class_hierarchy, overrides, dead_code, call_chain, module_deps, variable_scope, find_complexity, find_functions_by_argument, find_functions_by_decorator.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query_type": {"type": "string", "description": "Type of relationship query to run.", "enum": ["find_callers", "find_callees", "find_all_callers", "find_all_callees", "find_importers", "who_modifies", "class_hierarchy", "overrides", "dead_code", "call_chain", "module_deps", "variable_scope", "find_complexity", "find_functions_by_argument", "find_functions_by_decorator"]},
                "target": {"type": "string", "description": "The function, class, or module to analyze."},
                "context": {"type": "string", "description": "Optional: specific file path for precise results."},
                "repo_path": {"type": "string", "description": "Optional: Path to the repository to restrict the search to."}
            },
            "required": ["query_type", "target"]
        }
    },
    "watch_directory": {
        "name": "watch_directory",
        "description": "Performs an initial scan of a directory and then continuously monitors it for changes, automatically keeping the graph up-to-date. Ideal for projects under active development. Returns a job ID for the initial scan.",
        "inputSchema": {
            "type": "object",
            "properties": { "path": {"type": "string", "description": "Path to directory to watch"} },
            "required": ["path"]
        }
    },
    "execute_cypher_query": {
        "name": "execute_cypher_query",
        "description": "Fallback tool to run a direct, read-only Cypher query against the code graph. Use this for complex questions not covered by other tools. The graph contains nodes representing code structures and relationships between them. **Schema Overview:**\n- **Nodes:** `Repository`, `File`, `Module`, `Class`, `Function`.\n- **Properties:** Nodes have properties like `name`, `path`, `cyclomatic_complexity` (on Function nodes), and `source`.\n- **Relationships:** `CONTAINS` (e.g., File-[:CONTAINS]->Function), `CALLS` (Function-[:CALLS]->Function or File-[:CALLS]->Function), `IMPORTS` (File-[:IMPORTS]->Module), `INHERITS` (Class-[:INHERITS]->Class).",
        "inputSchema": {
            "type": "object",
            "properties": { "cypher_query": {"type": "string", "description": "The read-only Cypher query to execute."} },
            "required": ["cypher_query"]
        }
    },
    "add_package_to_graph": {
        "name": "add_package_to_graph",
        "description": "Add a package to the graph by discovering its location. Supports multiple languages. Returns immediately with a job ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "package_name": {"type": "string", "description": "Name of the package to add (e.g., 'requests', 'express', 'moment', 'lodash')."},
                "language": {"type": "string", "description": "The programming language of the package.", "enum": ["python", "javascript", "typescript", "java", "c", "go", "ruby", "php","cpp"]},
                "is_dependency": {"type": "boolean", "description": "Mark as a dependency.", "default": True}
            },
            "required": ["package_name", "language"]
        }
    },
    "find_dead_code": {
        "name": "find_dead_code",
        "description": "Find potentially unused functions (dead code) across the entire indexed codebase, optionally excluding functions with specific decorators.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "exclude_decorated_with": {"type": "array", "items": {"type": "string"}, "description": "Optional: A list of decorator names (e.g., '@app.route') to exclude from dead code detection.", "default": []},
                "repo_path": {"type": "string", "description": "Optional: Path to the repository to restrict the search to."}
            }
        }
    },
    "calculate_cyclomatic_complexity": {
        "name": "calculate_cyclomatic_complexity",
        "description": "Calculate the cyclomatic complexity of a specific function to measure its complexity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "The name of the function to analyze."},
                "path": {"type": "string", "description": "Optional: The full path to the file containing the function for a more specific query."},
                "repo_path": {"type": "string", "description": "Optional: Path to the repository to restrict the search to."}
            },
            "required": ["function_name"]
        }
    },
    "find_most_complex_functions": {
        "name": "find_most_complex_functions",
        "description": "Find the most complex functions in the codebase based on cyclomatic complexity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "The maximum number of complex functions to return.", "default": 10},
                "repo_path": {"type": "string", "description": "Optional: Path to the repository to restrict the search to."}
            }
        }
    },
    "list_indexed_repositories": {
        "name": "list_indexed_repositories",
        "description": "List all indexed repositories.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    "delete_repository": {
        "name": "delete_repository",
        "description": "Delete an indexed repository from the graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "The path of the repository to delete."} 
            },
            "required": ["repo_path"]
        }
    },
    "visualize_graph_query": {
        "name": "visualize_graph_query",
        "description": "Generates a URL to visualize the results of a Cypher query in the Neo4j Browser. The user can open this URL in their web browser to see the graph visualization.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cypher_query": {"type": "string", "description": "The Cypher query to visualize."}
            },
            "required": ["cypher_query"]
        }
    },
    "list_watched_paths": {
        "name": "list_watched_paths",
        "description": "Lists all directories currently being watched for live file changes.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    "unwatch_directory": {
        "name": "unwatch_directory",
        "description": "Stops watching a directory for live file changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The absolute path of the directory to stop watching."}
            },
            "required": ["path"]
        }
    },
    "load_bundle": {
        "name": "load_bundle",
        "description": "Load a pre-indexed .cgc bundle into the database. Can load from local file or automatically download from registry if not found locally. Bundles are portable snapshots of indexed code that load instantly without re-indexing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bundle_name": {"type": "string", "description": "Name of the bundle to load (e.g., 'flask', 'pandas', 'flask-main-2579ce9.cgc'). Can be a full filename or just the package name."},
                "clear_existing": {"type": "boolean", "description": "Whether to clear existing data before loading. Use with caution.", "default": False}
            },
            "required": ["bundle_name"]
        }
    },
    "search_registry_bundles": {
        "name": "search_registry_bundles",
        "description": "Search for available pre-indexed bundles in the registry. Returns bundles matching the search query with details like repository, version, size, and download information.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to find bundles (searches in name, repository, and description). Leave empty to list all bundles."},
                "unique_only": {"type": "boolean", "description": "If true, show only the most recent version of each package. If false, show all versions.", "default": False}
            }
        }
    },
    "get_repository_stats": {
        "name": "get_repository_stats",
        "description": "Get statistics about indexed repositories, including counts of files, functions, classes, and modules. Can show overall database statistics or stats for a specific repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Optional: Path to a specific repository. If not provided, returns overall database statistics."}
            }
        }
    },
    "get_watcher_health": {
        "name": "get_watcher_health",
        "description": "Returns health status for all active file watchers, including last batch time, error counts, cached files, and Neo4j connectivity. Use this to check if the code graph is stale.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "grep_code": {
        "name": "grep_code",
        "description": "Search for a text pattern or regex across indexed repositories. Returns matching lines with file paths, line numbers, and context. Use this for: string literals, error messages, API paths, config keys, TODO/FIXME comments. Use find_code for symbol name lookups. Use find_references for comprehensive 'who uses this symbol' queries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text or regex pattern to search for."},
                "is_regex": {"type": "boolean", "default": False},
                "file_pattern": {"type": "string", "description": "Glob filter (e.g. '*.py', 'test_*.py')."},
                "exclude_pattern": {"type": "string", "description": "Glob to exclude (e.g. 'test_*', '*.migration.py')."},
                "repo_path": {"type": "string", "description": "Optional: restrict to one repository."},
                "context_lines": {"type": "integer", "default": 2},
                "max_results": {"type": "integer", "description": "Max total matches (not per-file). Default 50.", "default": 50},
                "case_sensitive": {"type": "boolean", "default": True}
            },
            "required": ["pattern"]
        }
    },
    "get_file_content": {
        "name": "get_file_content",
        "description": "Read source code from a file in an indexed repository. Returns content with line numbers. Security-scoped: only reads files within indexed repos. Use after find_code or grep_code to read actual source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "start_line": {"type": "integer", "description": "First line (1-indexed). Omit for file start."},
                "end_line": {"type": "integer", "description": "Last line (1-indexed). Omit for file end."},
                "around_line": {"type": "integer", "description": "Center the view on this line number. Combine with context_lines."},
                "context_lines": {"type": "integer", "description": "Lines before/after around_line. Default 20.", "default": 20},
                "max_lines": {"type": "integer", "default": 500}
            },
            "required": ["path"]
        }
    },
    "get_function_context": {
        "name": "get_function_context",
        "description": "Returns comprehensive context for a function: source code (from filesystem), class membership, callers, callees, imports, and sibling methods. Use this when you need to understand a function before modifying it. Use find_code for simple name lookups. Use grep_code for text/pattern searches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function_name": {"type": "string", "description": "Name of the function to analyse."},
                "file_path": {"type": "string", "description": "Optional: file path to disambiguate same-named functions."},
                "repo_path": {"type": "string", "description": "Optional: restrict to a specific repository."},
                "include_source": {"type": "boolean", "description": "Include full source code (always from filesystem). Default true.", "default": True},
                "caller_depth": {"type": "integer", "description": "Levels of callers. Default 1.", "default": 1},
                "callee_depth": {"type": "integer", "description": "Levels of callees. Default 1.", "default": 1},
                "include_sibling_methods": {"type": "boolean", "description": "If method, include other methods on the same class (names + signatures only). Default true.", "default": True}
            },
            "required": ["function_name"]
        }
    },
    "get_module_overview": {
        "name": "get_module_overview",
        "description": "Returns a structured summary of a code module: endpoints (with HTTP methods), service classes and methods, models, schemas, and key functions. Use this to understand what a module does before diving into files. Works best with Python backend modules but supports TypeScript too.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "module_path": {"type": "string", "description": "Path to the module directory (absolute or relative to repo root)."},
                "repo_path": {"type": "string", "description": "Optional: repository root for resolving relative paths."}
            },
            "required": ["module_path"]
        }
    },
    "find_references": {
        "name": "find_references",
        "description": "Find all references to a symbol: callers, importers, inheritors, type annotations, and text mentions. Broader than find_callers. Use this for comprehensive 'who uses this?' queries. Use find_callers for just the call graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name (function, class, variable, type)."},
                "repo_path": {"type": "string", "description": "Optional: restrict to one repository."},
                "include_definitions": {"type": "boolean", "default": False}
            },
            "required": ["symbol"]
        }
    },
    "diff_since": {
        "name": "diff_since",
        "description": "Show files changed within a time window or since a commit. Uses git history. Useful for picking up another agent's work, reviewing recent changes, or morning standup context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Repository path."},
                "since": {"type": "string", "description": "Time ref: '1h', '4h', '1d', '3d', a commit SHA, or ISO date."},
                "extensions": {"type": "array", "items": {"type": "string"}, "description": "Filter by extensions."},
                "include_diff": {"type": "boolean", "description": "Include actual diff content. Default false.", "default": False},
                "include_stats": {"type": "boolean", "default": True},
                "include_uncommitted": {"type": "boolean", "description": "Include staged + unstaged changes. Default true.", "default": True}
            },
            "required": ["repo_path", "since"]
        }
    },
    "explain_path": {
        "name": "explain_path",
        "description": "Find the shortest call chain between two functions. Shows how control flows from A to B through the call graph. Use for bug tracing, understanding data flow, and planning refactors.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_function": {"type": "string", "description": "Starting function name."},
                "to_function": {"type": "string", "description": "Target function name."},
                "repo_path": {"type": "string", "description": "Optional: restrict to one repository."},
                "max_depth": {"type": "integer", "default": 6}
            },
            "required": ["from_function", "to_function"]
        }
    },
    "get_file_structure": {
        "name": "get_file_structure",
        "description": "Returns directory tree of an indexed repository with function/class counts per file. Use for understanding project layout. Use get_module_overview for deeper module analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Repository path."},
                "directory": {"type": "string", "description": "Optional: subdirectory scope."},
                "extensions": {"type": "array", "items": {"type": "string"}},
                "max_depth": {"type": "integer", "default": 4},
                "include_counts": {"type": "boolean", "description": "Function/class counts per file. Default true.", "default": True}
            },
            "required": ["repo_path"]
        }
    },
}
