#!/usr/bin/env python3
"""
extract_workload_from_dir.py

Extracts SQL queries (DQL, DML) and database schema metadata from any Python
application directory by statically analyzing Python ASTs and DDL files.
Generates:
  1. res.wg: Semicolon-terminated SQL queries (default 100 queries, matching AgentTune's res.wg).
  2. res.json: Schema metadata JSON file matching AgentTune's Workload Analyzer requirements.

Usage:
    python tools/extract_workload_from_dir.py \
        --input_dir "/path/to/python/app" \
        --output_wg "workload analyzer/workloads/smallbank.wg" \
        --output_json "workload analyzer/workloads/smallbank.json" \
        --workload_size 100
"""

import argparse
import ast
import glob
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class ASTQueryExtractor:
    """Statically parses Python ASTs to discover constants, f-strings, and SQL queries."""

    SQL_KEYWORDS = {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "WITH"}

    def __init__(self, input_dir: str):
        self.input_dir = os.path.abspath(input_dir)
        self.constants: Dict[str, str] = {}
        self.raw_queries: List[Dict[str, str]] = []
        self.ddl_statements: List[str] = []

    def scan(self) -> None:
        """Scan all Python files in the target directory."""
        py_files = sorted(glob.glob(os.path.join(self.input_dir, "**", "*.py"), recursive=True))
        if not py_files:
            print(f"Warning: No Python files found in {self.input_dir}")
            return

        # Pass 1: Collect module-level and class-level constants across all files
        for py_file in py_files:
            try:
                self._collect_constants(py_file)
            except Exception as e:
                print(f"Error reading constants from {py_file}: {e}")

        # Pass 2: Extract SQL statements from AST
        for py_file in py_files:
            try:
                self._extract_queries_from_file(py_file)
            except Exception as e:
                print(f"Error parsing queries from {py_file}: {e}")

    def _collect_constants(self, filepath: str) -> None:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        try:
            tree = ast.parse(content, filename=filepath)
        except Exception:
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        var_name = target.id
                        val = self._resolve_constant_val(node.value)
                        if val is not None and isinstance(val, str):
                            self.constants[var_name] = val
                            self.constants[var_name.lower()] = val
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.value:
                    var_name = node.target.id
                    val = self._resolve_constant_val(node.value)
                    if val is not None and isinstance(val, str):
                        self.constants[var_name] = val
                        self.constants[var_name.lower()] = val

    def _resolve_constant_val(self, node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        elif isinstance(node, ast.Str):
            return node.s
        return None

    def _extract_queries_from_file(self, filepath: str) -> None:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        try:
            tree = ast.parse(content, filename=filepath)
        except Exception:
            return

        # Custom recursive visitor to avoid descending into JoinedStr child constants
        self._visit_node(tree, filepath)

    def _visit_node(self, node: ast.AST, filepath: str) -> None:
        # f-strings: resolve completely and do NOT visit children
        if isinstance(node, ast.JoinedStr):
            resolved = self._resolve_fstring(node)
            if resolved:
                self._process_candidate_sql(resolved, filepath, "f-string")
            return

        # Regular string literals
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self._process_candidate_sql(node.value, filepath, "literal")
        elif isinstance(node, ast.Str):
            self._process_candidate_sql(node.s, filepath, "literal")

        # Recursively visit children
        for child in ast.iter_child_nodes(node):
            self._visit_node(child, filepath)

    def _resolve_fstring(self, node: ast.JoinedStr) -> str:
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(str(part.value))
            elif isinstance(part, ast.Str):
                parts.append(part.s)
            elif isinstance(part, ast.FormattedValue):
                if isinstance(part.value, ast.Name):
                    var_name = part.value.id
                    val = self.constants.get(var_name, self.constants.get(var_name.lower(), var_name))
                    parts.append(val)
                else:
                    parts.append("")
        return "".join(parts)

    def _process_candidate_sql(self, text: str, filepath: str, source_type: str) -> None:
        trimmed = text.strip()
        if not trimmed:
            return

        # Check if text starts with an SQL keyword
        first_word = trimmed.split()[0].upper().rstrip(";(")
        if first_word in self.SQL_KEYWORDS:
            stmts = [s.strip() for s in re.split(r";(?:\s*\n|\s*$)", trimmed) if s.strip()]
            for stmt in stmts:
                self._classify_and_add_sql(stmt, filepath, source_type)

    SYSTEM_TABLES = {"pg_database", "pg_tables", "pg_stat", "pg_class", "information_schema", "sqlite_master", "sqlite_sequence", "dual"}

    def _classify_and_add_sql(self, sql: str, filepath: str, source_type: str) -> None:
        cleaned = re.sub(r"\s+", " ", sql).strip().rstrip(";")
        first_word = cleaned.split()[0].upper()

        # Ignore system queries
        if any(st in cleaned.lower() for st in self.SYSTEM_TABLES):
            return

        # Validate query completeness to eliminate partial code fragments
        if first_word == "SELECT":
            # Must have FROM
            if re.search(r"\bFROM\s+[a-zA-Z0-9_]+", cleaned, re.IGNORECASE):
                self.raw_queries.append({
                    "type": "DQL",
                    "sql": cleaned,
                    "file": filepath,
                    "source": source_type
                })
        elif first_word == "UPDATE":
            # Must have SET
            if re.search(r"\bUPDATE\s+[a-zA-Z0-9_]+\s+SET\b", cleaned, re.IGNORECASE):
                self.raw_queries.append({
                    "type": "DML",
                    "sql": cleaned,
                    "file": filepath,
                    "source": source_type
                })
        elif first_word == "INSERT":
            # Must have INTO and (VALUES or SELECT)
            if re.search(r"\bINSERT\s+INTO\s+[a-zA-Z0-9_]+", cleaned, re.IGNORECASE):
                self.raw_queries.append({
                    "type": "DML",
                    "sql": cleaned,
                    "file": filepath,
                    "source": source_type
                })
        elif first_word == "DELETE":
            # Must have FROM
            if re.search(r"\bDELETE\s+FROM\s+[a-zA-Z0-9_]+", cleaned, re.IGNORECASE):
                self.raw_queries.append({
                    "type": "DML",
                    "sql": cleaned,
                    "file": filepath,
                    "source": source_type
                })
        elif first_word in ("CREATE", "DROP", "ALTER"):
            self.ddl_statements.append(cleaned)


class SQLConcretizer:
    """Substitutes parameter placeholders (%s, ?, $1) with valid, diverse concrete SQL literals."""

    def __init__(self, seed: int = 42):
        self.rand = random.Random(seed)

    def concretize(self, sql_template: str) -> str:
        sql = sql_template

        def replace_match(match):
            start = max(0, match.start() - 30)
            context = sql[start:match.start()].lower()

            if any(k in context for k in ("id", "custid", "w_id", "d_id", "c_id", "o_id", "count", "num")):
                return str(self.rand.randint(1, 1000000))
            elif any(k in context for k in ("bal", "balance", "amount", "price", "tax", "ytd", "rate")):
                return f"{self.rand.uniform(10.0, 5000.0):.2f}"
            elif any(k in context for k in ("name", "last", "first", "data", "info", "c", "pad", "street", "city")):
                rand_str = "".join(self.rand.choices("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=16))
                return f"'{rand_str}'"
            elif any(k in context for k in ("date", "time", "entry_d")):
                return "'2026-09-08 12:00:00'"
            else:
                return str(self.rand.randint(1, 1000000))

        # Handle printf style formatters like %02d or %%s
        sql = sql.replace("%%s", "%s")
        sql = re.sub(r"%0\d+d", lambda m: str(self.rand.randint(1, 99)), sql)

        # Handle %s
        sql = re.sub(r"%s", replace_match, sql)
        # Handle ?
        sql = re.sub(r"\?", replace_match, sql)
        # Handle $1, $2, etc.
        sql = re.sub(r"\$\d+", replace_match, sql)
        # Handle %(name)s
        sql = re.sub(r"%\([a-zA-Z0-9_]+\)s", replace_match, sql)

        sql = self._normalize_sql(sql)
        return sql

    def _normalize_sql(self, sql: str) -> str:
        sql = re.sub(r"\s+", " ", sql).strip().rstrip(";")

        # Convert SQL keywords to uppercase while preserving identifier case
        keywords = ["SELECT", "FROM", "WHERE", "INSERT", "INTO", "VALUES", "UPDATE", "SET",
                    "DELETE", "AND", "OR", "ORDER BY", "GROUP BY", "LIMIT", "FOR UPDATE",
                    "JOIN", "ON", "AS", "COUNT", "SUM", "AVG", "MIN", "MAX", "DESC", "ASC"]
        for kw in keywords:
            sql = re.sub(rf"\b{kw}\b", kw, sql, flags=re.IGNORECASE)

        # Normalize table and column identifiers to lowercase for pglast & Parserbase compatibility
        # We lowercase any identifier following FROM, INTO, UPDATE, TABLE, JOIN
        def lower_tbl_clause(m):
            prefix = m.group(1)
            tbl = m.group(2).lower()
            return f"{prefix} {tbl}"

        sql = re.sub(r"\b(FROM|INTO|UPDATE|JOIN)\s+([a-zA-Z0-9_]+)", lower_tbl_clause, sql, flags=re.IGNORECASE)

        # Lowercase column names in WHERE and SET clauses
        return sql + " ;"


class SchemaExtractor:
    """Extracts schema definitions from .sql files or DDL statements into AgentTune res.json format."""

    def __init__(self, input_dir: str, ddl_statements: List[str]):
        self.input_dir = os.path.abspath(input_dir)
        self.ddl_statements = ddl_statements

    def extract_schema(self) -> Dict:
        ddl_text = self._gather_ddl()
        tables = self._parse_ddl_to_tables(ddl_text)

        schema_json = {
            "Table Schema": "public",
            "Tables": tables
        }
        return schema_json

    def _gather_ddl(self) -> str:
        chunks = []
        sql_files = sorted(glob.glob(os.path.join(self.input_dir, "**", "*.sql"), recursive=True))
        for sql_file in sql_files:
            try:
                with open(sql_file, "r", encoding="utf-8", errors="ignore") as f:
                    chunks.append(f.read())
            except Exception as e:
                print(f"Error reading SQL file {sql_file}: {e}")

        chunks.extend(self.ddl_statements)
        return "\n\n".join(chunks)

    def _parse_ddl_to_tables(self, ddl_text: str) -> List[Dict]:
        tables: List[Dict] = []
        seen_tables: Set[str] = set()

        create_table_regex = re.compile(
            r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([a-zA-Z0-9_]+)\s*\((.*?)\)(?:\s*;|\s*$)",
            re.IGNORECASE | re.DOTALL
        )

        for match in create_table_regex.finditer(ddl_text):
            tbl_name = match.group(1).lower()
            body = match.group(2)

            if tbl_name in seen_tables:
                continue
            seen_tables.add(tbl_name)

            table_meta = self._parse_table_body(tbl_name, body)
            if table_meta["Table Columns"]:
                tables.append(table_meta)

        return tables

    def _parse_table_body(self, tbl_name: str, body: str) -> Dict:
        columns = []
        pk_info = {"Name": "", "Data Type": ""}
        fk_list = []

        lines = [l.strip().rstrip(",") for l in body.split("\n") if l.strip()]

        for line in lines:
            line_upper = line.upper()

            if "PRIMARY KEY" in line_upper and ("CONSTRAINT" in line_upper or line_upper.startswith("PRIMARY KEY")):
                pk_match = re.search(r"PRIMARY\s+KEY\s*\(\s*([a-zA-Z0-9_]+)\s*\)", line, re.IGNORECASE)
                if pk_match:
                    pk_info["Name"] = pk_match.group(1).lower()
                continue

            if "FOREIGN KEY" in line_upper and "REFERENCES" in line_upper:
                fk_match = re.search(
                    r"FOREIGN\s+KEY\s*\(\s*([a-zA-Z0-9_]+)\s*\)\s*REFERENCES\s+([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_]+)\s*\)",
                    line,
                    re.IGNORECASE
                )
                if fk_match:
                    fk_col = fk_match.group(1).lower()
                    ref_tbl = fk_match.group(2).lower()
                    ref_col = fk_match.group(3).lower()
                    fk_list.append({
                        "Foreign Key Name": fk_col,
                        "Foreign Key Type": "bigint",
                        "Referenced Table": ref_tbl,
                        "Referenced Primary Key": ref_col,
                        "Referenced Primary Key Type": "bigint"
                    })
                continue

            parts = line.split()
            if len(parts) >= 2:
                col_name = parts[0].lower()
                if col_name in ("constraint", "primary", "foreign", "unique", "check", "index"):
                    continue

                raw_type = parts[1].upper()
                type_mod = 0
                data_dist = [0, 1000000]
                std_type = "int"

                mod_match = re.search(r"\(([0-9]+)\)", raw_type)
                if mod_match:
                    type_mod = int(mod_match.group(1))

                if "INT" in raw_type or "SERIAL" in raw_type:
                    std_type = "int" if "BIG" not in raw_type else "bigint"
                    data_dist = [0, 1000000]
                elif "CHAR" in raw_type or "TEXT" in raw_type:
                    std_type = "varchar" if "VAR" in raw_type else "bpchar"
                    type_mod = type_mod if type_mod > 0 else 64
                    data_dist = [type_mod, type_mod]
                elif "FLOAT" in raw_type or "DOUBLE" in raw_type or "NUMERIC" in raw_type or "REAL" in raw_type:
                    std_type = "float"
                    data_dist = [10000, 50000]

                if "PRIMARY KEY" in line_upper:
                    pk_info["Name"] = col_name
                    pk_info["Data Type"] = std_type

                columns.append({
                    "Column Name": col_name,
                    "Data Type": std_type,
                    "Data Type Mod": type_mod,
                    "Data Distribution": data_dist
                })

        if pk_info["Name"]:
            for col in columns:
                if col["Column Name"] == pk_info["Name"]:
                    pk_info["Data Type"] = col["Data Type"]
                    break

        n_cols = max(1, len(columns))
        col_distribution = [round(1.0 / n_cols, 4)] * n_cols

        return {
            "Table Name": tbl_name,
            "Table Columns": columns,
            "Column Distribution": col_distribution,
            "Primary Key": pk_info,
            "Foreign Key": fk_list
        }


class WorkloadGenerator:
    """Synthesizes a 100-query .wg file from discovered DQL and DML templates."""

    def __init__(self, queries: List[Dict[str, str]], target_size: int = 100, seed: int = 42):
        self.queries = queries
        self.target_size = target_size
        self.concretizer = SQLConcretizer(seed=seed)
        self.rand = random.Random(seed)

    def generate(self) -> List[str]:
        if not self.queries:
            raise ValueError("No SQL queries discovered to generate workload.")

        unique_templates = list({q["sql"]: q for q in self.queries}.values())

        generated_queries = []
        for i in range(self.target_size):
            chosen = self.rand.choice(unique_templates)
            concretized = self.concretizer.concretize(chosen["sql"])
            generated_queries.append(concretized)

        return generated_queries


def validate_with_workload_parser(wg_path: str, json_path: str, repo_root: str) -> bool:
    """Invokes AgentTune's WorkloadParser.py to verify compatibility."""
    parser_script = os.path.join(repo_root, "workload analyzer", "WorkloadParser.py")
    if not os.path.exists(parser_script):
        print(f"[-] WorkloadParser script not found at {parser_script}")
        return False

    output_features = os.path.join(repo_root, "workload analyzer", "workload_features")
    cmd = [
        "/Users/dannykhant/.local/bin/uv", "run",
        "--with", "pandas", "--with", "pglast", "--with", "numpy",
        "python", parser_script,
        "--workload_file", os.path.abspath(wg_path),
        "--config_file", os.path.abspath(json_path),
        "--output", output_features
    ]
    print(f"[*] Validating with WorkloadParser.py...")
    res = subprocess.run(cmd, cwd=os.path.join(repo_root, "workload analyzer"), capture_output=True, text=True)
    if res.returncode == 0:
        print("[+] Validation successful! WorkloadParser extracted features cleanly.")
        if os.path.exists(output_features):
            with open(output_features, "r") as f:
                lines = f.readlines()
                print("[*] Generated workload features preview:")
                for l in lines[:15]:
                    print(f"    {l.rstrip()}")
        return True
    else:
        print(f"[-] Validation failed:\n{res.stderr}\n{res.stdout}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Extract SQL queries and schema metadata from a Python codebase for AgentTune."
    )
    parser.add_argument("--input_dir", type=str, required=True, help="Path to Python codebase (e.g. py-smallbank).")
    parser.add_argument("--output_wg", type=str, default="./workload analyzer/workloads/res.wg", help="Output .wg path.")
    parser.add_argument("--output_json", type=str, default="./workload analyzer/workloads/res.json", help="Output .json path.")
    parser.add_argument("--workload_size", type=int, default=100, help="Number of queries in .wg (default: 100).")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic generation.")
    parser.add_argument("--validate", action="store_true", default=True, help="Validate output using AgentTune's WorkloadParser.py.")

    args = parser.parse_args()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print(f"[*] Scanning directory: {args.input_dir}")
    extractor = ASTQueryExtractor(args.input_dir)
    extractor.scan()

    print(f"[*] Discovered {len(extractor.constants)} constants")
    print(f"[*] Discovered {len(extractor.raw_queries)} complete SQL query templates (DQL/DML)")
    print(f"[*] Discovered {len(extractor.ddl_statements)} DDL statements")

    if not extractor.raw_queries:
        print("[-] Error: No valid SQL queries found in Python files.")
        sys.exit(1)

    # 1. Generate Schema JSON
    print(f"[*] Extracting schema metadata...")
    schema_ext = SchemaExtractor(args.input_dir, extractor.ddl_statements)
    schema_data = schema_ext.extract_schema()

    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(schema_data, f, indent=4)
    print(f"[+] Saved schema JSON to: {args.output_json} ({len(schema_data['Tables'])} tables)")

    # 2. Generate Workload .wg
    print(f"[*] Generating {args.workload_size}-query workload .wg...")
    generator = WorkloadGenerator(extractor.raw_queries, target_size=args.workload_size, seed=args.seed)
    wg_queries = generator.generate()

    os.makedirs(os.path.dirname(os.path.abspath(args.output_wg)), exist_ok=True)
    with open(args.output_wg, "w", encoding="utf-8") as f:
        for q in wg_queries:
            f.write(q + "\n")
    print(f"[+] Saved workload trace to: {args.output_wg} ({len(wg_queries)} queries)")

    # 3. Validate
    if args.validate:
        validate_with_workload_parser(args.output_wg, args.output_json, repo_root)

    # Summary
    print("\n" + "=" * 60)
    print("Workload & Schema Extraction Summary")
    print("=" * 60)
    print(f"Target Directory : {args.input_dir}")
    print(f"Workload Trace   : {args.output_wg} ({len(wg_queries)} queries)")
    print(f"Schema JSON      : {args.output_json} ({len(schema_data['Tables'])} tables: {', '.join(t['Table Name'] for t in schema_data['Tables'])})")
    print("=" * 60)


if __name__ == "__main__":
    main()
