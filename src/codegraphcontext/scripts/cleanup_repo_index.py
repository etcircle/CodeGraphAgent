#!/usr/bin/env python3
"""
One-time cleanup script: removes single-file repository entries from the Neo4j graph.

These entries are typically created by ad-hoc `add_code_to_graph` calls on individual
files, and they pollute `list_indexed_repositories` with fragments.

Usage:
    python -m codegraphcontext.scripts.cleanup_repo_index [--dry-run]
"""
import sys

from codegraphcontext.core import get_database_manager
from codegraphcontext.utils.debug_log import info_logger


def find_single_file_repos(driver) -> list[dict]:
    """Find repos whose path looks like a single file (has a file extension)."""
    with driver.session() as session:
        result = session.run("""
            MATCH (r:Repository)
            WHERE r.path CONTAINS '.'
              AND NOT r.path ENDS WITH '/'
              AND r.path =~ '.*\\.[a-zA-Z]{1,10}$'
            OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
            WITH r, count(f) as file_count
            WHERE file_count <= 1
            RETURN r.name as name, r.path as path, file_count
            ORDER BY r.path
        """)
        return [dict(record) for record in result]


def cleanup(dry_run: bool = True):
    db_manager = get_database_manager()
    driver = db_manager.get_driver()

    single_file_repos = find_single_file_repos(driver)

    if not single_file_repos:
        print("No single-file repository entries found.")
        return

    print(f"Found {len(single_file_repos)} single-file repository entries:")
    for repo in single_file_repos:
        print(f"  - {repo['path']} (name: {repo['name']}, files: {repo['file_count']})")

    if dry_run:
        print("\n[DRY RUN] No changes made. Run without --dry-run to delete.")
        return

    for repo in single_file_repos:
        with driver.session() as session:
            session.run("""
                MATCH (r:Repository {path: $path})
                OPTIONAL MATCH (r)-[:CONTAINS*]->(e)
                DETACH DELETE r, e
            """, path=repo["path"])
        info_logger(f"Deleted single-file repo: {repo['path']}")

    print(f"\nDeleted {len(single_file_repos)} single-file repository entries.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv or len(sys.argv) == 1
    cleanup(dry_run=dry_run)
